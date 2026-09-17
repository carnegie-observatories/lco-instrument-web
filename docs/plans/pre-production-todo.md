# Pre-production: what has to merge or change so nothing is rsynced

State on 2026-09-17. Today `playbooks/gateway.yml` installs the gateway
from git, cloudflared from brew/apt with vaulted credentials, and gcam from
a pinned zwo release — and then rsyncs two working trees from the control
laptop: the astro-ph checkout (viewer packages, built by hand; chz1 as a
path dependency) and zwo's `src/web` (gcamweb, from an unmerged PR). Both
are marked TEMPORARY in the role. A pre-production host must come up from
an ansible run with nothing copied from anyone's laptop: every input is a
git ref, a release asset with a checksum, or a package from a registry,
pinned in the inventory.

Items are grouped by repo; **blockers** are the ones without which the
rsync cannot go. Everything else is polish that the same run should carry.

## astro-ph (astro-ph-labs/astro-ph, private) — the root of both rsyncs

- [ ] **Merge `chz1-stream` (PR #1).** `chz1.stream` is the server both
      imageweb and gcamweb embed and it exists only on that branch; the
      heartbeat fix is on it too. Blocker.
- [ ] **Publish chz1 as a Python package, or make it git-installable.**
      imageweb and gcamweb both carry `chz1 = { path = "../../…/astro-ph/packages/chz1" }`
      — a relative path into a checkout that must sit beside the consumer.
      Two ways out; pick one:
      - a wheel attached to a chz1 tag/release (private repo → the host
        needs a token to download it), or
      - `chz1 = { git = "https://github.com/astro-ph-labs/astro-ph", subdirectory = "packages/chz1", tag = "chz1-v0.1.0" }`
        with a read-only deploy token on the host.
      Either way, tag it (`chz1-v0.1.0`) so the consumers pin a version.
      Blocker.
- [ ] **Ship the viewer packages as artifacts, not a checkout.** The
      gateway serves `/pkg/{chz1,core,viewer}/` straight from the
      monorepo: `chz1/ts/src` (source, fine), but `core/dist` and
      `viewer/dist` are `tsc -b` outputs that are git-ignored and built on
      the laptop. `publish.yml` already pushes `@astro-ph-labs/{core,viewer}`
      to GitHub Packages (npm.pkg.github.com), so: publish `core 0.1.0`,
      `viewer 0.1.1`, `chz1 0.1.0`, and have the gateway role `npm pack` /
      `npm install --no-save` those three at pinned versions into
      `gateway_root/pkg/` (GitHub Packages needs a token, same one as
      above). The import maps in `viewer/*.html` then point at
      `node_modules/@astro-ph-labs/…` paths instead of `pkg/<name>/dist`.
      Blocker.
- [ ] Keep `[tool.uv] cache-keys` (local commit `fe1002c`) or drop it once
      chz1 is a versioned package — a git/registry source reinstalls on
      version change and does not need it.
- [ ] Decide private vs public. Everything above is easier if the repo (or
      just chz1/core/viewer) is public: no tokens on the host, plain
      `git+https` and `npm install` work. The host today cannot reach
      GitHub over SSH at all.

## zwo (carnegie-observatories/zwo, public) — gcamweb

- [ ] **Merge PR #35** (`feature/gcam-web-viewer`, mergeable, no review
      yet). It carries `src/web/gcamweb` and `docs/plans/gcam-web-viewer.md`.
      Blocker.
- [ ] **Cut a release that includes it** (v1.1 is a pre-release with only
      the gcam/zwoserver `.run` assets). Then gcamweb installs like gcam
      does: either
      - `uv tool install "gcamweb @ git+https://github.com/carnegie-observatories/zwo@v1.2#subdirectory=src/web"`
        (public repo, no token), or
      - a `gcamweb-<ver>.whl` asset built by the zwo CI and fetched with
        `get_url` + sha256, the gcam role's pattern.
      Blocker.
- [ ] Repoint gcamweb's `chz1` source from the path to the tag chosen
      above (its pyproject says so itself: "Pin to a git ref when it
      publishes").

## lco-instrument-web (carnegie-observatories/lco-instrument-web, public)

- [ ] **Merge `plan/deployment-config` into `main`** — 34 commits: the
      deployment config, the gateway, the viewer assembly, the guider
      pages, the night-test recorder, the gateway heartbeat. The SBS
      inventory pins `gateway_version: plan/deployment-config`; after the
      merge pin a tag (`v0.2.0`) instead of a branch. Blocker for "pinned",
      not for "no rsync".
- [ ] Push the three commits still local (ssh agent was down): report,
      plan, close-code summary.
- [ ] Repoint imageweb's `chz1` source from the relative path to the tag,
      and regenerate `uv.lock` (`uv lock`), so `uv sync --frozen` on the
      host resolves without an astro-ph checkout beside it.
- [ ] Repoint the import maps (`viewer/quicklook.html`, `viewer/guider.html`)
      and `mount_packages()` in `gateway.py` at the installed npm packages
      (one directory, three packages) instead of the monorepo layout.
- [ ] Deploy and verify the two heartbeats: `playbooks/gateway.yml`, then
      one more night with `tools/nighttest.py`; close classes 1 and 2 must
      read zero (`docs/reports/night-test-2026-09-17.md`).
- [ ] Tidy `proxy_ws`'s `ws_connect(timeout=ClientTimeout(total=10))` —
      deprecated argument of the wrong type; use `ClientWSTimeout` or the
      session default.
- [ ] Tag a release and note it in `docs/deploy-cloudflare.md`.
- [ ] Commit or drop the three modified/untracked plan files sitting in
      the working tree (`image-viewer-plan.md`, `ws-migration-plan.md`,
      `ws-migration-fourstar-plan.md`).

## lco-ansible (carnegie-observatories/lco-ansible, private)

- [ ] **Merge `feature/gateway-role` into `main`** — 5 commits: gateway
      role, cloudflared role, gcamweb tasks, `/healthz` reporting, tunnel
      post-checks. Also commit or drop the working-tree edits to
      `inventory_lco.yaml`, `playbooks/zwo_server_deploy.yml`,
      `instrument-updater.sh`, `uv.lock`.
- [ ] **Replace the astro-ph synchronize** (`roles/gateway/tasks/main.yml`)
      with the package installs from the astro-ph section: `npm install`
      of the three JS packages at pinned versions into `gateway_root/pkg`,
      and nothing else — chz1's Python side comes in through imageweb's
      `uv sync`. Delete `gateway_astro_ph_src`, the "check the source on
      the control machine" tasks and the "own the tree as the connecting
      user" workaround with it. Blocker.
- [ ] **Replace the gcamweb synchronize** (`roles/gateway/tasks/gcamweb.yml`)
      with the release install from the zwo section; delete
      `gateway_gcamweb_src`. Blocker.
- [ ] Add the token, if any repo stays private: one read-only token in
      the host's vault (`host_vars/<host>/secrets.yaml`, same pattern as
      `cloudflared_tunnel_credentials`), used for GitHub Packages and/or
      the private git source. Never on the command line; a `.netrc` or
      `UV_INDEX`/`NODE_AUTH_TOKEN` file owned by the gateway user, 0400.
- [ ] Pin every version in the inventory next to `gateway_version`:
      `gateway_viewer_packages: {core: 0.1.0, viewer: 0.1.1, chz1: 0.1.0}`,
      `gateway_gcamweb_version`, `gateway_chz1_ref`, `cloudflared_version`.
      A host's whole web stack should be readable from `inventory_sbs.yaml`.
- [ ] Idempotence check: two consecutive runs, the second `changed=0`.
      The rsync tasks were the one place that could never quite get there
      (mode fights, ownership handovers); their removal should make it
      trivial, and the check belongs in the playbook's docs.
- [ ] Linux half of the gateway and cloudflared roles has never run
      (systemd units, apt repo for cloudflared). The pre-production host
      is Linux per the deployment plan: run it once in a VM before the
      real one.

## Instrument apps (obs1 / sbs-inst1)

- [ ] PFS on the test host reports `0.0.0-d48a6d1`: a development build.
      The `instruments` role already has an updater that pulls tagged
      `.app` builds from GitHub releases; pre-production should run a
      tagged PFS build the same way, and the `hello.version` the gateway's
      `/healthz` reports should be that tag. Same for any other instrument
      the deployment file lists.
- [ ] The instrument's WS server is the one thing the night test could not
      measure the far side of; with the heartbeat on the proxy the browser
      side is covered, but a `heartbeat`/ping on the Cocoa `WSServer`
      itself would protect the LAN path too. Optional.

## Cloudflare

- [ ] `deploy/access-policies.yml` and `deploy/cloudflared/<name>/config.yml`
      are generated from `deployments/<name>.yml`
      (`tools/generate_deploy_artifacts.py --check` in CI, `--write` to
      update); the cloudflared role templates its own config from the
      inventory. Make one of them the source: either the role consumes the
      generated `config.yml`, or the generator goes. Today they agree by
      hand.
- [ ] `deploy/sync-access-policies.py` is run by hand against the
      Cloudflare API; either wire it into the playbook (post-task, with
      the API token in the vault) or document that it is a manual step
      with its own check. Not a rsync, but it is the same kind of
      laptop-dependence.
- [ ] Access session / idle timers: the night test found nothing at the
      Access layer, but the class-3 resets need the cloudflared log
      (`/opt/cloudflared/log/cloudflared.log`) read once on the host
      before calling the tunnel path production-grade.

## Definition of done

`uv run ansible-playbook -i inventory_<site>.yaml -l <host> playbooks/gateway.yml`
on a fresh host, with no astro-ph or zwo checkout on the control machine,
brings up gateway + imageweb + gcamweb + cloudflared from pinned
versions; a second run reports `changed=0`; `/healthz` lists every
instrument and guider `ok`; and a night of `tools/nighttest.py` shows no
class-1 or class-2 socket closes.
