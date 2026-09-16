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

Only the last one is actually per-deployment. Standing up a second
telescope today means editing HTML by hand, writing a second
cloudflared config, and starting three processes with hand-written
flags — with nothing that says which instruments that telescope is
*supposed* to have.

Meanwhile the deployment facts already exist, in
[lco-ansible](https://github.com/carnegie-observatories/lco-ansible)'s
inventory: `inventory_lco.yaml` groups hosts by telescope, each group
carrying an `instruments:` list, and hosts carrying `gcam_guiders:`
entries with names, ini profiles and camera hosts. That repo deploys
the Cocoa apps and the guider GUIs — but not the web surface. Its own
SBS inventory says so:

> the guider-camera web interfaces this Mac is meant to exercise
> alongside PFS come from lco-instrument-web (SPA + gcam bridge),
> which this repo does not deploy yet.

This plan closes that gap: one configuration file per deployment, one
gateway service that reads it, and an ansible role that installs both
— **on SBS only**, until it is proven there.

## Scope: SBS first, and only SBS

The work lands and is validated on the SBS test deployment before any
telescope is touched. Concretely:

- Phase 0–6 produce exactly one deployment file, `deployments/sbs.yml`.
- `clay`, `baade` and `swope` are **not** written, not generated, not
  deployed. What each will need is recorded in an appendix at the end
  of this plan so the research is not lost, but nothing acts on it.
- The schema is designed for many deployments from day one — that is
  the point of the exercise — but it is exercised by one.
- After SBS is validated end to end (§ Validation gate), a follow-up
  plan covers the rollout, informed by whatever SBS taught us.

SBS already has a tunnel, a DNS record, an Access application and a
committed cloudflared config, so it is the cheapest possible place to
get this wrong.

## Goals & non-goals

**Goals:**

- One file per deployment, declaring every instrument, guider and
  viewer on it. Adding an instrument is one entry in that file.
- The SPA is data-driven — the landing page and the connect strings
  come from the deployment config at runtime, not from edited HTML.
- One **gateway service**, configured by that file, fronting the
  instruments, the quick-look viewers and the guiders.
- The gateway runs on **Linux in production and macOS for test**,
  from the same codebase, with the platform difference confined to
  service management.
- The tunnel ingress and the Access policy are *generated* from the
  same file, so they cannot drift from the SPA's idea of the
  deployment.
- The deployment keeps **its own Cloudflare subdomain**
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
- Rolling out to Clay, Baade or Swope. Separate plan, after the gate.
- Secrets. Tunnel credentials and the Access API token stay out of
  these files, as they are today.

## Gateway host: Linux in production, macOS for test

The production gateway is a **separate Linux machine** — not the
instrument Mac. That is a better place for it (it is not competing
with the observer's desktop, it can be rebuilt without touching an
instrument, and cloudflared is a first-class systemd service there),
but it changes three things that are currently implicit.

**1. The gateway is now across the network from the instruments.**
Today every target is `127.0.0.1`. On a separate host the gateway
reaches instrument WS ports over the LAN, so those ports must be
reachable from the gateway host, and `gateway.host ≠ instrument host`
becomes the normal case rather than an edge case. The deployment
schema already carries a `host:` per instrument, so this is a
network/firewall task, not a design change.

**2. cloudflared moves to the gateway host.** At SBS it runs on the
Mac today. On Linux it becomes a packaged systemd unit instead of a
`launchctl kickstart` of a root launchd job. The committed
`deploy/cloudflared/sbs/config.yml` still describes the ingress; only
`credentials-file:` and the install location change, and that file is
already documented as per-Mac by nature.

**3. Quick look breaks, and this is the one real blocker.**
[imageweb](../../imageweb/README.md) is a read-only client of the
instrument's control WS: on `exposure_complete` it takes the event's
`fits_path` and opens it — `pyfits.open(path)` in
[source.py:153](../../imageweb/imageweb/source.py#L153). That path is
"local and absolute by contract"
([image-viewer-plan.md](image-viewer-plan.md) § Decided). A gateway
on another machine has no such file.

Three ways out, in order of preference:

- **(a) Make the path resolve on the gateway.** Export the
  instrument's data directory (NFS or SMB) and mount it on the
  gateway, with a `fits_roots:` mapping in the deployment file
  translating the instrument-local prefix to the gateway mount point.
  No Cocoa change, no protocol change, one mount per instrument host.
  **Recommended.**
- **(b) Run a per-instrument imageweb sidecar on the instrument Mac**
  and have the gateway proxy `/image/<app>/` to it. Preserves the
  local-disk contract exactly, at the cost of re-introducing the
  per-instrument processes this plan is consolidating away — and of
  needing the ansible role on the Macs too.
- **(c) Ship the bytes over the WS.** Cleanest conceptually, but it
  is a Cocoa change and the migration plan explicitly moved image
  transfer *out* of the apps and into the gateway. Rejected for this
  plan.

Option (a) is assumed below. **It is untested** — it is the one part
of this plan with no working precedent anywhere in the stack, and
Phase 5 exists to find out whether it holds before anything depends
on it.

### The SBS gateway, concretely

The host exists: **`sbs-instruments-gw`**, a freshly installed KVM VM.

| | |
|---|---|
| OS | Ubuntu 24.04.2 LTS, x86_64, kernel 6.8.0 |
| Address | `172.16.10.120/24` on `ens18`, reached via jump host `nuc` (`172.16.10.100`) |
| Admin | `william`, passwordless sudo |
| Installed | Python 3.12.3 |
| **Not** installed | `uv`, `cloudflared`, `cifs-utils` |

**Data comes over SMB.** The share is
`//wschoenell@obshome/wschoenell`, where `obshome` is
`userdata.obs.carnegiescience.edu` (`10.7.80.3`). It is already
mounted on the instrument Mac at `/Volumes/wschoenell` — by `obs1`,
the account PFS runs as — so the instrument-side path is real today
and the gateway side is a mount away. Port 445 is **reachable from
the gateway** (verified), which settles option (a)'s data half:

```yaml
fits_root:
  instrument: /Volumes/wschoenell/DATA
  gateway:    /mnt/obshome/wschoenell/DATA
```

**But PFS is not writing there yet.** `/Volumes/wschoenell/DATA`
exists and is **empty**, created at the same moment as the mount, and
PFS's preferences (`edu.carnegiescience.obs.PFS.plist`) carry no
data-path key at all — only CCD and window settings. So where the
FITS lands is a provisioning decision still open, which makes both
shapes of option (a) genuinely available:

| | **(a1)** mount `obshome` on the gateway | **(a2)** share the data dir *from the Mac* |
|---|---|---|
| Gateway mounts | `//obshome/wschoenell` → `/mnt/obshome/...` | `//sbs-inst1/DATA` → `/mnt/sbs-inst1/...` |
| Network needed | gateway→`10.7.80.3:445` — **already works** | gateway→`10.7.129.58:445` — **blocked today** |
| PFS change | must be pointed at `/Volumes/wschoenell/DATA` | none; it writes where it already writes |
| Machines in the path | three (Mac, obshome, gateway) | two (Mac, gateway) |
| Mac-side cost | none — the share is already mounted | enable File Sharing, a sharing account, SMB load during readout |

(a2)'s blocked-network row looks decisive but is not: the gateway
needs `10.7.129.58:51603` for the control WebSocket regardless, so
that path has to be opened for *any* version of this plan. Once it
is, (a2) costs nothing extra and removes a machine from the
dependency chain.

**Recommendation: (a1).** It works for the data half today,
independent of the network fix, so the file pipeline can be proven
before the routing question is settled; a central data server is
where observatory data should land anyway (backups, access from
elsewhere); and it asks nothing of the observer's Mac during
readout. (a2) stays documented as the fallback if pointing PFS at
the share turns out to be unwelcome — it becomes free the moment the
network is fixed.

Either way the **`fits_root:` mapping is the only thing that
changes** in the deployment file, which is the point of having it.

Two consequences for the ansible role. The Mac's mount is *per-user*
(`mounted by obs1`, and unreadable by anyone else — `ls` as another
user is `Permission denied`), so the gateway's must be a **system**
mount — `/etc/fstab` or a systemd `.mount` unit — with `uid`/`gid`
pinned to the gateway service account rather than inherited. And SMB
needs **credentials**, which must not go anywhere near
`deployments/sbs.yml`: they belong in the ansible vault, rendered to
a root-owned `credentials=` file, exactly as the repo already handles
`raspberry_password` and the instrument admin passwords.

### ⚠ Blocker: the gateway cannot reach the instruments

Verified in both directions, and it stops Phase 5 dead:

| From | To | Result |
|---|---|---|
| `sbs-instruments-gw` (172.16.10.120) | `sbs-inst1` (10.7.129.58) ports 22, 51603, 8080 | **unreachable**, ICMP 100% loss |
| `sbs-inst1` | `sbs-instruments-gw` ports 22, 8080 | **unreachable** |
| `sbs-instruments-gw` | `obshome` (10.7.80.3) port 445 | **open** |

The gateway has a route to `10.7.129.58` via `172.16.10.1` and the
Mac has one back via `10.7.128.1`, but nothing passes — the two sit on
networks with no path between them. So the gateway can read the FITS
bytes but cannot open the control WebSocket that tells it a frame
exists, cannot proxy `/<app>/ws`, and cannot health-check anything.

Three ways forward, in order of preference:

- **Add a second interface to the VM on the instrument network.** It
  is a KVM guest; a bridged NIC on the instrument VLAN is the
  smallest change, needs no firewall policy, and matches what a
  production gateway wants anyway — one foot on the instrument LAN,
  one where the tunnel terminates.
- **Route and firewall between the two subnets.** The correct fix if
  the gateway should stay single-homed, but it is a network-team
  change with a wider blast radius than this plan.
- **Do nothing yet.** Phases 0–4 all run on the Mac and are entirely
  unaffected. This only has to be solved before Phase 5.

Nothing in this plan should be scheduled past Phase 4 until one of
the first two lands.

### What has to be portable

| Concern | macOS (test) | Linux (production) |
|---|---|---|
| Service manager | launchd plist + `launchctl kickstart` | systemd unit + `systemctl` |
| Install prefix | `/opt/homebrew` (arm64) | `/usr/local` (Ubuntu 24.04) |
| Runtime | uv-managed venv | same |
| Service account | `obs1` | dedicated `gateway` user (owns the mount uid) |
| cloudflared | root launchd job | packaged systemd unit |
| FITS access | `/Volumes/wschoenell` (obs1's SMB mount) | `/mnt/obshome/...` (system SMB mount) |

Everything above the service manager is already portable: the gateway
is Python, `uv` runs on both, and the SPA is static files. The
platform split is genuinely confined to "how does this process get
started and kept alive", which is exactly where ansible should absorb
it.

## The deployment file

`deployments/<name>.yml` in this repo. One per deployment; the name
is the Cloudflare subdomain label. Day one, there is one of them:

```yaml
# deployments/sbs.yml
name: sbs
title: SBS test deployment
domain: sbs.chimera.observer

gateway:
  host: sbs-gateway           # ansible inventory hostname
  port: 8080                  # the one local port the tunnel targets

instruments:
  - app: pfs                  # instruments/<app>/ supplies the UI
    title: Planet Finder Spectrograph
    host: sbs-inst1
    # port omitted → derived from the PROJECT_ID table (pfs → 51603)
    quicklook: true           # FITS producer; gets /image/pfs/
    fits_root:                # § Gateway host, option (a)
      instrument: /Users/obs1/data
      gateway: /mnt/sbs-inst1/data

guiders:
  - name: pfs-sv
    title: PFS slit viewer
    ini: pfs
    gnum: 3
    host: sbs-inst1
    tcs_mode: 0               # no TCS at SBS

access:
  - "*@carnegiescience.edu"
  - "hreggiani@gmail.com"
  - "ph.silva@gmail.com"
```

Notes on the shape:

- **`port` is derived, not written.** The WS port is
  `50001 + 100×PROJECT_ID + 2`, and the mapping is already tabulated
  in [ws-migration-plan.md](ws-migration-plan.md) (ldss3 50603,
  fourstar 51103, mike 50803, pfs 51603, swope 51203, mage 51503,
  dcu 51703, ifum/m2fs 51803, adc 52403, henrietta 52803). That table
  becomes a checked-in `instruments/ports.yml` that both the gateway
  and the generators read. An explicit `port:` overrides it, for a
  machine running a non-standard build.
- **Guider ports are derived the same way** — gcam compiles
  `PROJECT_ID 22`, so commands are `52200 + gnum` and the image
  server `52300 + gnum`. `gnum` is the one number that must be
  written, and it already lives in lco-ansible's
  `gcam_ini_settings[ini].gnum` (overridable per guider).
- **`quicklook: true` replaces the hardcoded embed.** The Quick Look
  sub-tab is added to that instrument's window list at runtime
  instead of being frozen into `instruments/pfs/manifest.json`, which
  is shared by every telescope that runs PFS.
- **`fits_root:` is omitted when the gateway is the instrument host**
  — which is how the Mac test target will run at first, keeping
  Phases 1–4 free of the mount question entirely.
- **`access:` subsumes `deploy/access-policies.yml`.** That file's
  per-telescope allowed-lists move into the matching deployment file,
  and `sync-access-policies.py` reads `deployments/*.yml` instead.
  One less place a deployment is half-declared. The other three
  telescopes' entries move across unchanged when their files are
  written; until then `access-policies.yml` keeps them.

## Where the config lives, and how it stays true

The deployment file lives **in this repo**, not in lco-ansible.
It describes web topology (subdomain, paths, which instruments have a
browser surface), the SPA fetches it at runtime, and it must be
reviewable alongside the code that consumes it.

But lco-ansible remains the authority on *which machines exist and
what runs on them*, so the two must be checked against each other.
The check is mechanical and belongs in CI:

```
deployments/sbs.yml             inventory_sbs.yaml (group: sbs)
  instruments[].app        ⊆    hosts[].instruments[].name  (lowercased)
  instruments[].host       ∈    group hosts
  guiders[].name           ≡    hosts[].gcam_guiders[].name
  gateway.host             ∈    group hosts
```

Subset, not equality, for instruments — an app without a WS surface
is legitimately absent from the web config. A *superset* is always an
error: a deployment file naming an app the host does not install is a
typo or a stale entry.

This is the one genuinely contestable decision in the plan. The
alternative — generate `deployments/*.yml` from the inventory — was
rejected because the mapping needs human judgment (which apps have a
browser surface, `quicklook:`, guider titles, the FITS mount
mapping), and a generator with that many exception lists is a worse
artifact than a hand-written file with an automated check.

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
lco-gateway --deployment deployments/sbs.yml

  /                     SPA static files (this repo)
  /config.json          the deployment, as the SPA consumes it
  /<app>/ws             → ws://<instrument host>:<derived port>/
  /image/<app>/         quick-look viewer  (imageweb, in-process)
  /guider/<name>/       web guider         (gcam bridge, in-process)
  /healthz              per-target reachability, for the runbook
```

What this buys:

- **The tunnel ingress collapses to one rule** — everything on the
  hostname goes to the gateway. Adding an instrument stops touching
  cloudflared entirely, which removes the most error-prone step in
  the current runbook.
- **One service to install, start and check** per platform, instead
  of three with an undocumented start order.
- **The SPA stops guessing.** `/config.json` is served by the process
  that knows the deployment.

The costs, stated plainly:

- **An extra hop on the instrument WebSocket**, and on a separate
  gateway host that hop is now LAN rather than loopback. It also
  makes the gateway a single point of failure for every instrument on
  the telescope, where today a crashed static-file server leaves the
  WS endpoints up.
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

Both generators are pure functions of `deployments/sbs.yml` and write
files that are committed and reviewable — the existing convention for
the cloudflared config, which is versioned precisely so "which path →
which port" stays reviewable.

- `deploy/cloudflared/sbs/config.yml` — one catch-all rule once
  Phase 3 lands; until then, the same per-instrument rules written
  today, generated instead of typed. The `tunnel:` UUID and
  `credentials-file:` are per-host and are preserved from the
  existing file rather than generated.
- Cloudflare Access — `sync-access-policies.py` reads
  `deployments/*.yml` for the deployments that have files and
  `access-policies.yml` for the rest, keeping its current behaviour
  including the refusal to push an empty allowed-list without
  `--allow-lockout`.

## Ansible integration

Following the conventions already in lco-ansible, with one addition.

**The platform split.** Today that repo splits platforms *by
playbook*: `boot_server.yml`, `zwo_server_*.yml` and
`poe_tool_deploy.yml` are Linux and use `systemd`; `instrument_mac.yml`,
`gcam_mac.yml` and `henrietta_mac.yml` are macOS and use launchd. No
role does both. The gateway is the first thing that must, so the
split moves inside the role:

```
roles/gateway/
  defaults/main.yml           versions, prefixes, ports
  tasks/main.yml              platform-independent: fetch, venv, config
  tasks/service-launchd.yml   included when ansible_system == 'Darwin'
  tasks/service-systemd.yml   included when ansible_system == 'Linux'
  templates/
    deployment.yml.j2
    com.carnegie.lco-gateway.plist.j2
    lco-gateway.service.j2
```

`tasks/main.yml` ends with

```yaml
- ansible.builtin.include_tasks: "service-{{ 'launchd' if ansible_system == 'Darwin' else 'systemd' }}.yml"
```

so everything above that line is written once and the divergence is a
single template plus a start/enable task. Both service files take the
same variables (exec path, deployment file, service user, log path),
which keeps the two templates comparable at review time.

The rest follows existing practice:

- **Inventory group** `gateway_computers`, with per-deployment leaf
  groups (`sbs_gateway_computers`, …), the same shape as
  `gcam_computers` / `instrumentation_computers`.
- **Playbook `playbooks/gateway.yml`** — deliberately *not*
  `gateway_mac.yml`, since it targets both platforms. Post-task
  assertions on `/healthz` and a debug task reporting the deployment
  URL, matching `gcam_mac.yml`'s "report how to start each guider"
  ending.
- **Which deployment file a host gets** comes from a
  `gateway_deployment: sbs` group var, defaulting to the leaf group's
  deployment name.
- **Delivery** follows the instrument-app model: `instrument-updater.sh`
  pulls **release assets**, not git checkouts, so the gateway ships as
  a versioned release artifact and the role pins a version + SHA256
  exactly as `gcam` does with `gcam_bin_url`.

## Phases

Each phase is one commit on the plan branch; each is independently
useful and independently revertible. Every phase targets SBS.

**Phase 0 — schema and the SBS file.** `deployments/sbs.yml`;
`instruments/ports.yml`; a JSON Schema; a validator. Nothing consumes
them yet. *Verification:* the validator passes, and the inventory
cross-check passes against a current `lco-ansible` checkout.

**Phase 1 — the SPA reads the config.** `index.html` renders its
cards from `/config.json` (falling back to the static file when
served by `server.py`); `ws.js` resolves host/port/path from it;
`quicklook:` drives the Quick Look sub-tab instead of the frozen
`manifest.json` entry. *Verification:* SBS looks identical to today,
with nothing instrument-specific left in `index.html`.

**Phase 2 — the gateway service.** `lco-gateway` subsumes
`server.py`, `imageweb` and the gcam bridge in one process, serving
static files, `/config.json`, `/image/*` and `/guider/*`. WS still
routed by cloudflared. Still on the Mac, still all-loopback.
*Verification:* SBS runs on one process and one port; quick look and
the guider work through the tunnel.

**Phase 3 — WS proxy (flagged).** `/<app>/ws` proxied in-process;
cloudflared reduced to a single rule. *Verification:* a full PFS
exposure through the proxy, the `logs` topic under load, and a
deliberate instrument restart to confirm reconnect still works.

**Phase 4 — generators.** cloudflared config and the SBS Access
policy generated from the deployment file. *Verification:* the
generated SBS config is byte-identical to the committed one, modulo
the Phase-3 collapse.

**Phase 5 — split the host.** Move the gateway off `sbs-inst1` onto a
Linux machine: mount the instrument's data directory, set
`fits_root:`, move cloudflared, prove quick look still works across
the network. This is the phase that tests § Gateway host option (a),
and the phase most likely to send the plan back for revision.
*Verification:* PFS exposure → quick look renders on a gateway that
has never had the FITS file on its own disk.

**Phase 6 — ansible role and playbook.** Role with the launchd /
systemd split, playbook, inventory group; deployed to both the Mac
and the Linux gateway from the same role. *Verification:* a
from-scratch run on a clean host of each platform brings up
`sbs.chimera.observer` with no manual steps.

**Phase 7 — drift checks in CI.** The inventory cross-check and the
generator diff run on every PR.

## Validation gate

Before any other deployment is written, SBS must have run a real
observing-shaped session on the Linux gateway:

- every instrument in `sbs.yml` connects, commands ack, state updates
- a full exposure completes and renders in quick look from the
  mounted path
- the guider streams
- the gateway survives an instrument app restart, a gateway service
  restart, and a tunnel reconnect
- `/healthz` correctly reports a deliberately-stopped instrument

Only then does the rollout plan get written.

## Risks

- **Option (a) may not hold.** If mounting the instrument's data
  directory on the gateway is unacceptable — export policy, latency
  on large FITS, an instrument that writes to a path it will not
  share — Phase 5 fails and quick look falls back to option (b), a
  sidecar per instrument Mac. That is a material change to the
  "one service" story, which is why it is phased before the ansible
  work rather than after.
- **A gateway outage takes every instrument's browser surface with
  it** once Phase 3 lands, and a separate gateway host adds a machine
  that can fail independently. Mitigation: the direct
  `?host=…&port=…` connect string keeps working from the
  observatory VPN and is the documented fallback; `/healthz` plus the
  service manager's restart policy cover the common case.
- **The Linux path is unexercised until Phase 5.** Phases 1–4 all run
  on the Mac, so the first four phases prove nothing about the
  production platform. Mitigation is to keep the platform difference
  to the service manager and nothing else — if Phase 5 needs code
  changes beyond mounts and a unit file, the abstraction was wrong.
- **The deployment file drifts from reality.** Mitigation: Phase 7's
  CI check, plus `/healthz` reporting per-target reachability so a
  wrong host shows up as a red card on the landing page rather than a
  silent failure.
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
  belongs in a repo about to grow per-site configuration — worth
  clearing before Phase 0 regardless of this plan.

## Open questions

1. **How does the gateway reach the instrument network** — a second
   NIC on the VM, or routing between the subnets? Blocks Phase 5 and
   nothing earlier. (§ Blocker.)
2. **Where should PFS write its FITS?** Nowhere today. Pointing it at
   `/Volumes/wschoenell/DATA` chooses option (a1); leaving it local
   chooses (a2). (§ The SBS gateway, concretely.)
3. **Which SMB account does the gateway mount as?** The Mac mounts as
   `wschoenell`, a person. A service account is the right answer for
   an unattended mount, and its credentials go in the ansible vault.
4. **Does the gateway need the observatory VPN, or does it sit
   outside?** It terminates the tunnel and reaches instrument LAN
   ports; where it sits relative to the VPN boundary is a security
   decision, and
   [security-models.md](https://github.com/carnegie-observatories/lco-ansible/blob/main/docs/plans/security-models.md)
   is the right place to settle it.
5. **What happens to `--instrument NAME@HOST:PORT` and `--guider`?**
   Phase 2 folds them into the deployment file. Keeping them as
   development overrides is useful; keeping them as the *only*
   interface for a one-off is what this plan is trying to end.

## Appendix — the other deployments, for later

Not in scope. Recorded so the research survives to the rollout plan.

| Deployment | Instruments (from inventory) | Guiders | Notes |
|---|---|---|---|
| `clay` | ADC, DCU, PFS, MIKE, MagE, LDSS3C, IFUM, M2FS | `clay-gcam11` (NASE SH), `clay-gcam12` (NASE PG) | group `clay` lists nine apps including GuidePaddle |
| `baade` | *none declared* | `baade-gcam01` (IMACS PG), `baade-gcam02` (IMACS SH) | |
| `swope` | Swope; Henrietta on `hen-drp` | none | Henrietta is a host-level `instruments:` override |

Two findings worth carrying forward:

- **Baade has no `instruments:` list in the inventory at all** — the
  group defines boot servers and the two IMACS guider cameras and
  nothing else. Its instruments are IMACS and FourStar, both outside
  the WS migration (IMACS is C end-to-end; FourStar is a C daemon,
  though [ws-migration-fourstar-plan.md](ws-migration-fourstar-plan.md)
  now exists). `baade.yml` would be a deployment of guiders and
  nothing else — a good test that the schema does not assume
  instruments exist.
- **GuidePaddle is in Clay's inventory list but is a TCS client, not
  a server.** It has no WS surface and must not appear in a
  deployment file. The inventory list is "apps the auto-updater
  installs", which is a superset of "apps with a browser surface" —
  related but not equal, which is why the deployment file is authored
  rather than derived.
