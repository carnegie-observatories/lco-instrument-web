# Per-deployment configuration and the gateway service

## Context

Every telescope runs a different set of instruments, guiders and
viewers, but the browser surface is currently written for exactly one
of them. The SBS test deployment is hardcoded in five unrelated
places:

| What | Where | Holds |
|---|---|---|
| Landing-page cards | [index.html](../../index.html) | ADC/DCU/PFS + `gcam03`/`gcam41` + PFS quick look, at `localhost` dev URLs |
| WS port fallback | [ws.js:32](../../ws.js#L32) | `52403` (ADC) |
| Quick-look embed | [instruments/pfs/manifest.json](../../instruments/pfs/manifest.json) | `"embed": "/image/pfs/"` |
| Tunnel ingress | [deploy/cloudflared/sbs/config.yml](../../deploy/cloudflared/sbs/config.yml) | one rule per instrument, per guider, per viewer |
| Access allow-list | [deploy/access-policies.yml](../../deploy/access-policies.yml) | `clay` / `baade` / `swope` / `sbs` |

Only the last one is actually per-deployment. Standing up Clay today
means editing HTML by hand, writing a second cloudflared config, and
starting three processes with hand-written flags — with nothing that
says which instruments Clay is *supposed* to have.

Meanwhile the deployment facts already exist, in
[lco-ansible](https://github.com/carnegie-observatories/lco-ansible)'s
inventory: `inventory_lco.yaml` groups hosts by telescope (`baade`,
`clay`, `swope`), each group carrying an `instruments:` list, and
hosts carrying `gcam_guiders:` entries with names, ini profiles and
camera hosts. That repo deploys the Cocoa apps and the guider GUIs —
but not the web surface. Its own SBS inventory says so:

> the guider-camera web interfaces this Mac is meant to exercise
> alongside PFS come from lco-instrument-web (SPA + gcam bridge),
> which this repo does not deploy yet.

This plan closes that gap: one configuration file per deployment,
one gateway service that reads it, and an ansible role that installs
both.

## Goals & non-goals

**Goals:**

- One file per deployment, declaring every instrument, guider and
  viewer on it. Adding a telescope is adding a file; adding an
  instrument to a telescope is one entry in that file.
- The SPA is data-driven — the landing page and the connect strings
  come from the deployment config at runtime, not from edited HTML.
- One **gateway service** per deployment, configured by that file,
  fronting the instruments, the quick-look viewers and the guiders.
- The tunnel ingress and the Access policy are *generated* from the
  same file, so they cannot drift from the SPA's idea of the
  deployment.
- Each deployment keeps **its own Cloudflare subdomain**
  (`<name>.chimera.observer`), one Access application per subdomain.
- Deployed by lco-ansible, following that repo's existing role /
  playbook / inventory-group conventions.

**Non-goals:**

- Changing the WS protocol or any Cocoa app. The gateway is a client
  and a proxy; nothing new is asked of the instruments.
- Replacing the per-instrument `manifest.json` / `bindings.yml` /
  generated layouts. Those describe *an instrument*, are identical
  across telescopes, and stay where they are. Deployment config
  describes *which instruments are present and where*.
- Per-instrument subdomains. Universal SSL covers
  `*.chimera.observer` one level deep only, so instruments stay
  paths on the telescope hostname
  ([deploy-cloudflare.md § Domain layout](../deploy-cloudflare.md)).
- Secrets. Tunnel credentials and the Access API token stay out of
  these files, as they are today.

## The deployment file

`deployments/<name>.yml` in this repo. One per deployment; the name
is the Cloudflare subdomain label.

```yaml
# deployments/clay.yml
name: clay
title: Magellan Clay
domain: clay.chimera.observer

gateway:
  host: clay-inst1            # ansible inventory hostname
  port: 8080                  # the one local port the tunnel targets

instruments:
  - app: pfs                  # instruments/<app>/ supplies the UI
    title: Planet Finder Spectrograph
    host: clay-inst1
    # port omitted → derived from the PROJECT_ID table (pfs → 51603)
    quicklook: true           # FITS producer; gets /image/pfs/
  - app: adc
    host: clay-inst1
  - app: dcu
    host: clay-inst1
  - app: mike
    host: clay-inst1
    quicklook: true

guiders:
  - name: clay-nase-pg
    title: NASE principal guider
    ini: nase
    gnum: 12
    host: clay-gcam12
  - name: clay-nase-sh
    title: NASE Shack-Hartmann
    ini: nase
    gnum: 11
    host: clay-gcam11

access:
  - "*@carnegiescience.edu"
```

Notes on the shape:

- **`port` is derived, not written.** The WS port is
  `50001 + 100×PROJECT_ID + 2`, and the mapping is already tabulated
  in [ws-migration-plan.md](ws-migration-plan.md) (ldss3 50603,
  fourstar 51103, mike 50803, pfs 51603, swope 51203, mage 51503,
  dcu 51703, ifum/m2fs 51803, adc 52403, henrietta 52803). That table
  becomes a checked-in `instruments/ports.yml` that both the gateway
  and the generators read. An explicit `port:` in a deployment file
  overrides it, for a machine running a non-standard build.
- **Guider ports are derived the same way** — gcam compiles
  `PROJECT_ID 22`, so commands are `52200 + gnum` and the image
  server `52300 + gnum`. `gnum` is the one number that must be
  written, and it already lives in lco-ansible's
  `gcam_ini_settings[ini].gnum` (overridable per guider).
- **`quicklook: true` replaces the hardcoded embed.** The Quick Look
  sub-tab is added to that instrument's window list at runtime
  instead of being frozen into `instruments/pfs/manifest.json`, which
  is shared by every telescope that runs PFS.
- **`access:` subsumes `deploy/access-policies.yml`.** That file's
  per-telescope allowed-lists move into the matching deployment file,
  and `sync-access-policies.py` reads `deployments/*.yml` instead.
  One less place a telescope is half-declared.

### Which deployments exist on day one

| File | Instruments | Guiders | Notes |
|---|---|---|---|
| `sbs.yml` | PFS | `pfs-sv` (simulator, `tcs_mode: 0`) | test deployment; already has a tunnel + Access app |
| `clay.yml` | ADC, DCU, PFS, MIKE, MagE, LDSS3C, IFUM, M2FS | `clay-nase-pg`, `clay-nase-sh` | inventory group `clay` lists nine apps incl. GuidePaddle |
| `baade.yml` | — see below | `baade-gcam01` (IMACS PG), `baade-gcam02` (IMACS SH) | |
| `swope.yml` | Swope, Henrietta (`hen-drp`) | none | Henrietta is a host-level override in the inventory |

Two facts to settle while writing these, not after:

- **Baade has no `instruments:` list in the inventory at all** — the
  group defines boot servers and the two IMACS guider cameras and
  nothing else. Its instruments are IMACS and FourStar, both
  explicitly out of scope for the WS migration (IMACS is C
  end-to-end; FourStar is a C daemon, though
  [ws-migration-fourstar-plan.md](ws-migration-fourstar-plan.md) now
  exists). So `baade.yml` ships with `instruments: []` and its two
  guiders — a real, useful deployment consisting of guiders and
  nothing else, which is a good forcing function for not assuming
  every deployment has instruments.
- **GuidePaddle is in Clay's inventory list but is a TCS client, not
  a server.** It has no WS surface and must not appear in
  `clay.yml`. The inventory list is "apps the auto-updater installs",
  which is a superset of "apps with a browser surface" — the two
  lists are related but not equal, which is exactly why the
  deployment file is authored rather than derived.

## Where the config lives, and how it stays true

The deployment file lives **in this repo**, not in lco-ansible.
It describes web topology (subdomain, paths, which instruments have a
browser surface), the SPA fetches it at runtime, and it must be
reviewable alongside the code that consumes it.

But lco-ansible remains the authority on *which machines exist and
what runs on them*, so the two must be checked against each other.
The check is mechanical and belongs in CI:

```
deployments/clay.yml            inventory_lco.yaml (group: clay)
  instruments[].app        ⊆    vars.instruments[].name    (lowercased)
  instruments[].host       ∈    group hosts
  guiders[].name           ≡    hosts[].gcam_guiders[].name
  gateway.host             ∈    group hosts
```

Subset, not equality, for instruments — GuidePaddle and any app
without a WS surface is legitimately absent from the web config. A
*superset* is always an error: a deployment file naming an app the
telescope does not install is a typo or a stale entry.

This is the one genuinely contestable decision in the plan. The
alternative — generate `deployments/*.yml` from the inventory — was
rejected because the mapping needs human judgment at three points
(GuidePaddle, `quicklook:`, and guider titles), and a generator with
three hand-maintained exception lists is a worse artifact than a
hand-written file with an automated check.

## The gateway service

Today three processes serve one deployment:

| Process | Port | Serves |
|---|---|---|
| `server.py` | 8080 | SPA static files |
| `imageweb` | 8766 | `/image/<app>/` quick-look viewers |
| gcam bridge | 8765 | `/guider/<name>/` web guiders |

…and cloudflared needs an ingress rule for each, plus one per
instrument WS. Adding an instrument touches the tunnel config, the
landing page, and a process's command line.

**Proposal: one `lco-gateway` process, configured by the deployment
file, owning every path on the hostname.**

```
lco-gateway --deployment deployments/clay.yml

  /                     SPA static files (this repo)
  /config.json          the deployment, as the SPA consumes it
  /<app>/ws             → ws://<instrument host>:<derived port>/
  /image/<app>/         quick-look viewer  (imageweb, in-process)
  /guider/<name>/       web guider         (gcam bridge, in-process)
  /healthz              per-target reachability, for the runbook
```

What this buys:

- **The tunnel ingress collapses to one rule per deployment** —
  everything on the hostname goes to `127.0.0.1:8080`. Adding an
  instrument stops touching cloudflared entirely, which removes the
  single most error-prone step in the current runbook.
- **One launchd service** to install, start and check, instead of
  three with an undocumented start order.
- **The SPA stops guessing.** `/config.json` is served by the process
  that knows the deployment, so the landing page, the sub-tab strip
  and the connect strings all come from one source.

The costs, stated plainly:

- **An extra hop on the instrument WebSocket.** Frames are small and
  the hop is loopback-to-LAN on the same site, but it is a real
  addition to a path that currently goes browser → cloudflared →
  instrument. It also makes the gateway a single point of failure for
  every instrument on the telescope, where today a crashed
  static-file server leaves the WS endpoints up.
- **WS proxying has sharp edges** — subprotocol negotiation, ping /
  pong passthrough, half-close, and backpressure on the `logs` topic,
  which is the highest-volume thing on the wire.

Because of that second point, the WS proxy is deliberately **Phase 3,
behind a flag**, and Phases 1–2 leave cloudflared routing
`/<app>/ws` exactly as it does now. If the proxy proves troublesome
the plan stops at Phase 2 and still delivers per-deployment config —
the flag stays off and cloudflared keeps its per-instrument rules,
generated rather than hand-written.

## Generated artifacts

Both generators are pure functions of `deployments/<name>.yml` and
write files that are committed and reviewable — the existing
convention for the cloudflared config, which is versioned precisely
so "which path → which port" stays reviewable.

- `deploy/cloudflared/<name>/config.yml` — one catch-all rule once
  Phase 3 lands; until then, the same per-instrument rules written
  today, generated instead of typed. The `tunnel:` UUID and
  `credentials-file:` are per-Mac and are preserved from the existing
  file rather than generated.
- Cloudflare Access — `sync-access-policies.py` reads
  `deployments/*.yml` and keeps its current behaviour, including the
  refusal to push an empty allowed-list without `--allow-lockout`.

## Ansible integration

Following the conventions already in lco-ansible:

- **Inventory group.** A `gateway_computers` aggregator group,
  with per-telescope leaf groups (`clay_gateway_computers`, …), the
  same shape as `gcam_computers` / `instrumentation_computers`. At
  SBS and Clay the gateway Mac is the instrument Mac; the group
  exists so it need not be.
- **Role `gateway`.** Installs the runtime (uv + this repo at a
  pinned ref), templates the deployment file and a launchd plist,
  installs the cloudflared config, starts the service. Modelled on
  the `gcam` role: `defaults/main.yml` carries versions and prefixes,
  a template per config file, and post-tasks that assert the thing is
  actually running rather than trusting the play.
- **Playbook `playbooks/gateway_mac.yml`**, `hosts: gateway_computers`,
  with post-task assertions on `/healthz` and a debug task reporting
  the deployment URL — matching `gcam_mac.yml`'s "report how to start
  each guider" ending.
- **Which deployment file a host gets** comes from a
  `gateway_deployment: clay` group var, defaulting to the telescope
  group name.

Delivery of the code follows the existing instrument-app model:
`instrument-updater.sh` pulls **release assets**, not git checkouts,
so the gateway ships as a versioned release artifact and the role
pins a version + SHA256 exactly as `gcam` does with `gcam_bin_url`.

## Phases

Each phase is one commit on the plan branch; each is independently
useful and independently revertible.

**Phase 0 — schema and the four files.** `deployments/*.yml` for sbs,
clay, baade, swope; `instruments/ports.yml`; a JSON Schema; a
validator. Nothing consumes them yet. Verification: the validator
passes, and the inventory cross-check passes against a current
`lco-ansible` checkout.

**Phase 1 — the SPA reads the config.** `index.html` renders its
cards from `/config.json` (falling back to the static file when
served by `server.py`); `ws.js` resolves host/port/path from it;
`quicklook:` drives the Quick Look sub-tab instead of the frozen
`manifest.json` entry. Verification: SBS looks identical to today,
with nothing instrument-specific left in `index.html`.

**Phase 2 — the gateway service.** `lco-gateway` subsumes
`server.py`, `imageweb` and the gcam bridge in one process, serving
static files, `/config.json`, `/image/*` and `/guider/*`. WS still
routed by cloudflared. Verification: SBS runs on one process and one
port; quick look and both test guiders work through the tunnel.

**Phase 3 — WS proxy (flagged).** `/<app>/ws` proxied in-process;
cloudflared reduced to a single rule. Verification: a full PFS
exposure through the proxy, the `logs` topic under load, and a
deliberate instrument restart to confirm reconnect still works.

**Phase 4 — generators.** cloudflared config and Access policies
generated from the deployment files; `access-policies.yml` retired.
Verification: the generated SBS config is byte-identical to the
committed one, modulo the Phase-3 collapse.

**Phase 5 — ansible role and SBS rollout.** Role, playbook, inventory
group; deployed to `sbs-inst1`. Verification: a from-scratch run on a
clean Mac brings up `sbs.chimera.observer` with no manual steps.

**Phase 6 — Clay, Baade, Swope.** One tunnel, one DNS record, one
Access app and one playbook run each. Verification: each subdomain
serves its own instruments and nobody else's.

**Phase 7 — drift checks in CI.** The inventory cross-check and the
generator diff run on every PR, so a deployment file that stops
matching the inventory fails review rather than a night.

## Risks

- **A gateway outage takes every instrument's browser surface with
  it** once Phase 3 lands. Mitigation: the direct
  `?host=…&port=…` connect string keeps working from the
  observatory VPN and is the documented fallback; `/healthz` and the
  launchd `KeepAlive` cover the common case.
- **The deployment file drifts from reality** — an instrument moves
  host, a guider is renumbered. Mitigation: Phase 7's CI check, plus
  `/healthz` reporting per-target reachability so a wrong host shows
  up as a red card on the landing page rather than a silent failure.
- **Two copies of the shared plans.** `ws-migration-plan.md`,
  `ws-ui-conversion-plan.md` and the step-0 audits are currently
  tracked in **both** this repo and lco-ansible, with independent
  histories. This plan links them relative to this repo; before
  Phase 0, one copy should win and the other become a pointer.
  Otherwise the port table this plan depends on has two editable
  homes.
- **Secrets near the config.** `deploy/cloudflared/**/*.json` and
  `cert.pem` are gitignored, and the deployment files add no secrets.
  Separately and urgently: this working tree contains `.secrets` and
  a `.history/` directory holding timestamped copies of it. Neither
  belongs in a repo that is about to grow per-site configuration —
  worth clearing before Phase 0 regardless of this plan.

## Open questions

1. **Is the gateway Mac the instrument Mac?** At SBS and Clay today,
   yes. If Baade's gateway should be a separate machine — it has no
   instrument Mac in the inventory, only boot and camera servers —
   that changes the inventory group's membership, not the design.
2. **Does Swope's Henrietta get its own deployment or share Swope's?**
   `hen-drp` overrides `instruments:` at host level for the updater.
   A single `swope.yml` listing both apps is simpler; two subdomains
   is more faithful to "one deployment, one Access policy" if the
   allowed-lists should differ.
3. **What happens to `--instrument NAME@HOST:PORT` and `--guider`?**
   Phase 2 folds them into the deployment file. Keeping them as
   overrides is useful for development; keeping them as the *only*
   interface for a one-off is what the plan is trying to end.
