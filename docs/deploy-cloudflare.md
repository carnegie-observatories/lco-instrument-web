# Deploying behind Cloudflare Tunnel + Access

Serves the SPA and the instrument WebSocket to remote operators —
no VPN client, no inbound ports on the instrument Mac, per-user
authentication at Cloudflare's edge. The higher-level comparison
with the VPN model is in
[security-models.md](https://github.com/carnegie-observatories/lco-ansible/blob/main/docs/plans/security-models.md).

How it works: `cloudflared` runs on the instrument Mac and opens an
**outbound-only** TLS connection to Cloudflare. Cloudflare Access
authenticates every request against the IdP before it is forwarded
down the tunnel. The Cocoa app is untouched — the tunnel connects to
its WS port on localhost.

## Domain layout

One domain per telescope, on the `chimera.observer` zone:

| Telescope | Apex (landing + SPA) | Instrument hostnames |
|---|---|---|
| Clay  | `clay.chimera.observer`  | `pfs.clay.chimera.observer`, `dcu.clay.chimera.observer`, `adc.clay.chimera.observer`, … |
| Baade | `baade.chimera.observer` | `dcu.baade.chimera.observer`, … |
| Swope | `swope.chimera.observer` | `swope.swope.chimera.observer`, … |
| SBS *(test)* | `sbs.chimera.observer` | `adc.sbs.chimera.observer`, … |

A **test telescope** (SBS) uses the same machinery end-to-end —
tunnel, wildcard DNS, Access application — against a development
Mac instead of a summit machine. Use it to rehearse a deployment
change, onboard a new instrument, or demo the SPA without touching
a production telescope. See § Test telescope walkthrough.

- The **apex** serves the SPA static files (landing page + app).
- Each **instrument subdomain** carries that instrument's WebSocket
  at `/ws` (and, later, its image-transfer HTTP endpoint).
- One **Access application per telescope**, covering the apex and
  `*.<telescope-domain>` — so one allowed-email list governs every
  instrument on that telescope. The lists live in
  [`deploy/access-policies.yml`](../deploy/access-policies.yml) and
  are pushed by [`deploy/sync-access-policies.py`](../deploy/sync-access-policies.py)
  (see § Access policy as configuration).

## Prerequisites

- A Cloudflare account with the `chimera.observer` zone. The free
  plan includes Zero Trust for up to 50 users.
- An identity provider configured under **Zero Trust → Settings →
  Authentication**. For `@carnegiescience.edu` accounts on Google
  Workspace, add the *Google Workspace* IdP; the zero-setup
  fallback is **One-time PIN over email** (works with any address
  the policy allows).
- The instrument Mac running the Cocoa app (WS port per the
  formula `50001 + PROJECT_ID×100 + 2`) and, on the telescope's
  gateway Mac, the SPA static server (`python3 server.py`,
  default port 8080).

## Per-telescope setup

All commands run on the telescope's gateway Mac (or the single
instrument Mac if it hosts everything). Example uses Clay; swap
names for Baade / Swope.

### 1. Install and authenticate cloudflared

```sh
brew install cloudflared
cloudflared tunnel login        # opens a browser; pick chimera.observer
```

### 2. Create the tunnel

```sh
cloudflared tunnel create clay-telescope
# prints a tunnel UUID; credentials land in ~/.cloudflared/<UUID>.json
```

### 3. Write `~/.cloudflared/config.yml`

One tunnel carries the apex plus every instrument subdomain:

```yaml
tunnel: <UUID>
credentials-file: /Users/<you>/.cloudflared/<UUID>.json

ingress:
  # --- instrument WebSockets: /ws on each instrument subdomain ---
  - hostname: pfs.clay.chimera.observer
    path: ^/ws$
    service: http://localhost:51603
  - hostname: dcu.clay.chimera.observer
    path: ^/ws$
    service: http://localhost:51703
  - hostname: adc.clay.chimera.observer
    path: ^/ws$
    service: http://localhost:52403

  # --- SPA static files: apex and instrument subdomains alike ---
  # (the SPA is the same files everywhere; serving it on the
  # instrument hostnames lets an operator bookmark
  # https://pfs.clay.chimera.observer/ directly)
  - hostname: clay.chimera.observer
    service: http://localhost:8080
  - hostname: pfs.clay.chimera.observer
    service: http://localhost:8080
  - hostname: dcu.clay.chimera.observer
    service: http://localhost:8080
  - hostname: adc.clay.chimera.observer
    service: http://localhost:8080

  # Required catch-all.
  - service: http_status:404
```

If an instrument's Cocoa app runs on a *different* Mac than the
gateway, point its `/ws` rule at that host instead of localhost
(`service: http://pfs-mac.local:51603`) — the LAN hop stays inside
the observatory network.

Rules match top-to-bottom; the catch-all must be last. WebSocket
upgrade is proxied automatically — no special flag. Validate:

```sh
cloudflared tunnel ingress validate
cloudflared tunnel ingress rule https://pfs.clay.chimera.observer/ws
```

### 4. Route DNS

One-time setup — the apex plus a **wildcard record** covering every
current and future instrument subdomain:

```sh
cloudflared tunnel route dns clay-telescope clay.chimera.observer
cloudflared tunnel route dns clay-telescope '*.clay.chimera.observer'
```

(If the wildcard route is rejected by your cloudflared version,
create the record manually in the Cloudflare DNS dashboard: proxied
CNAME `*.clay` → `<UUID>.cfargotunnel.com`.) With the wildcard in
place, adding an instrument later needs **no DNS change**.

### 5. Apply the Access policy

Do **not** hand-create the Access application in the dashboard —
it's generated from configuration (next section) so the allowed
list is reviewable and versioned:

```sh
export CLOUDFLARE_API_TOKEN=...   # "Access: Apps and Policies Write"
python3 deploy/sync-access-policies.py --telescope clay
```

Until this step the tunnel is publicly reachable — run the sync
before sharing any hostname.

### 6. Run as a service

```sh
sudo cloudflared service install
```

Installs a launchd job so the tunnel survives reboots. Logs go to
`/Library/Logs/com.cloudflare.cloudflared.err.log`.

### 7. Connect

Operators open `https://clay.chimera.observer/`, pass the Access
login (any `@carnegiescience.edu` address by default), and land on
the instrument chooser. Instrument cards on a tunnel deployment
should link with host-only queries —
`app.html?host=pfs.clay.chimera.observer` — which the SPA resolves
to `wss://pfs.clay.chimera.observer/ws` automatically (HTTPS page +
no port ⇒ tunnel mode).

## Adding an instrument later

The per-instrument Cloudflare overhead is deliberately minimal:

| Piece | Change |
|---|---|
| Access policy | **none** — the telescope's app already covers `*.<telescope-domain>` |
| DNS           | **none** — the wildcard record from step 4 already resolves it |
| `config.yml`  | two ingress rules (the `/ws` port mapping + the static-file rule) and a tunnel restart |
| SPA           | one landing-page card (repo change, not Cloudflare) |

The `config.yml` mapping is the irreducible piece — something has to
know "pfs → local port 51603", and cloudflared's ingress table is
the safest place for it (the alternative, proxying WebSockets
through the Python static server, would put hand-rolled proxy code
in the control path for no gain).

## Test telescope walkthrough (SBS)

A complete worked example against a development Mac — the same
seven steps as a production telescope, condensed. Useful for
rehearsing changes and verifying the pipeline before touching Clay
/ Baade / Swope. Assumes an instrument app running locally (any of
ADC / DCU / PFS in simulator or bench mode) and the SPA static
server on port 8080.

```sh
# 1-2. tunnel
brew install cloudflared
cloudflared tunnel login
cloudflared tunnel create sbs-telescope

# 3. ~/.cloudflared/config.yml — one instrument (ADC on 52403) to start
cat > ~/.cloudflared/config.yml <<'EOF'
tunnel: <UUID>
credentials-file: /Users/<you>/.cloudflared/<UUID>.json
ingress:
  - hostname: adc.sbs.chimera.observer
    path: ^/ws$
    service: http://localhost:52403
  - hostname: sbs.chimera.observer
    service: http://localhost:8080
  - hostname: adc.sbs.chimera.observer
    service: http://localhost:8080
  - service: http_status:404
EOF
cloudflared tunnel ingress validate

# 4. DNS — apex + one-time wildcard
cloudflared tunnel route dns sbs-telescope sbs.chimera.observer
cloudflared tunnel route dns sbs-telescope '*.sbs.chimera.observer'

# 5. Access policy (sbs entry ships in deploy/access-policies.yml)
export CLOUDFLARE_API_TOKEN=...
python3 deploy/sync-access-policies.py --telescope sbs

# 6. run
cloudflared tunnel run sbs-telescope     # foreground for a test box;
                                         # `sudo cloudflared service install`
                                         # if it should persist
```

Then, with the instrument app + `python3 server.py` running:
open `https://sbs.chimera.observer/`, log in with a
`@carnegiescience.edu` address, and click the ADC card — the
landing page rewrites it to `adc.sbs.chimera.observer`
automatically on HTTPS, and the SPA connects
`wss://adc.sbs.chimera.observer/ws`.

**Verification checklist:**

- [ ] Access login page appears before any content (step 5 ran
      before the hostname was shared).
- [ ] A non-allowed email is refused.
- [ ] Landing page renders; ADC card shows the subdomain.
- [ ] SPA connects — `hello` arrives, topics populate in the
      Diagnostic view, log pane streams.
- [ ] A command round-trips (`ack` in the log pane).
- [ ] Kill `cloudflared`, restart it — SPA auto-reconnects within
      a few seconds.

**Tear-down** (a test telescope shouldn't outlive its test):

```sh
cloudflared tunnel delete sbs-telescope   # after stopping it
# then remove the two DNS records in the dashboard, and either
# delete the sbs Access app in Zero Trust → Applications or leave
# it (harmless once the tunnel is gone — nothing resolves).
```

## Access policy as configuration

Who may control each telescope lives in
[`deploy/access-policies.yml`](../deploy/access-policies.yml):

```yaml
account_id: "YOUR_CLOUDFLARE_ACCOUNT_ID"
session_duration: "24h"

telescopes:
  clay:
    domain: clay.chimera.observer
    allowed:
      - "*@carnegiescience.edu"
  baade:
    domain: baade.chimera.observer
    allowed:
      - "*@carnegiescience.edu"
  swope:
    domain: swope.chimera.observer
    allowed:
      - "*@carnegiescience.edu"
```

Two entry forms:

- `*@domain.tld` — whole-domain wildcard, maps to an Access
  *Emails ending in* rule. **This is the only wildcard Cloudflare
  Access supports** — `will*@x.edu` or `*@*.edu` are rejected by
  the sync script with an explanatory error.
- `person@example.org` — exact address, maps to an Access *Email*
  rule.

Entries are OR-ed. To grant a visiting astronomer access to Clay
only:

```yaml
  clay:
    domain: clay.chimera.observer
    allowed:
      - "*@carnegiescience.edu"
      - "visiting.astronomer@partner-university.edu"
```

Then re-run the sync:

```sh
python3 deploy/sync-access-policies.py                 # all telescopes
python3 deploy/sync-access-policies.py --telescope clay
python3 deploy/sync-access-policies.py --dry-run       # print API payloads only
```

The script is idempotent — it finds each telescope's Access
application by domain and updates it in place, or creates it on
first run. One application per telescope covering
`<domain>` + `*.<domain>`; one *Allow* policy per application,
rebuilt from the YAML every run. An empty `allowed:` list is
refused (it would lock everyone out) unless `--allow-lockout` is
passed.

The script needs `pip install pyyaml` and a `CLOUDFLARE_API_TOKEN`
environment variable — created as follows.

### Creating the API token

There are two token flavours; either works for the sync script:

- **Account-owned token** (recommended for the observatory — it
  survives any individual leaving): dashboard →
  **Manage Account → Account API Tokens**. This menu is only
  visible to Super Administrators.
- **User-owned token** (fine for a personal test / SBS):
  <https://dash.cloudflare.com/profile/api-tokens>, i.e. click
  your avatar (top-right) → **My Profile** → **API Tokens** in the
  left sidebar.

From whichever token page:

1. **Create Token** → scroll past the templates to
   **Create Custom Token** → **Get started** (there is no
   ready-made Access template — the custom builder is the right
   path, not a template).
2. **Token name**: something greppable, e.g.
   `access-policy-sync (chimera.observer)`.
3. **Permissions** — one row:
   - first dropdown: **Account**
   - second dropdown: **Access: Apps and Policies**
   - third dropdown: **Edit**
   (The dashboard shows *Edit*; API error messages call the same
   permission "Access: Apps and Policies Write" — they are the
   same grant.)
4. **Account Resources**: *Include* → the account that owns the
   `chimera.observer` zone. Don't leave it on "All accounts" if
   the token owner belongs to more than one.
5. **Client IP Address Filtering** *(optional but recommended)*:
   *Is in* → the observatory's egress IP range, so a leaked token
   is useless off-site.
6. **TTL** *(optional)*: an expiry forces periodic rotation;
   policy syncs are rare enough that re-creating the token
   annually is no burden.
7. **Continue to summary** → confirm it reads
   *"All accounts — Access: Apps and Policies:Edit"* (or your
   selected account) → **Create Token**.
8. **Copy the secret immediately** — it is shown exactly once.
   Store it in the observatory password manager, then:

   ```sh
   export CLOUDFLARE_API_TOKEN=<the-secret>
   python3 deploy/sync-access-policies.py --dry-run   # verify it works
   ```

The token can *only* manage Access applications and policies — it
cannot touch DNS, tunnels, or billing. Tunnel and DNS operations in
this guide authenticate separately via `cloudflared tunnel login`
(browser-interactive, no long-lived secret on disk beyond the
tunnel credential file).

**Treat the YAML as the source of truth.** Dashboard edits to
these applications will be overwritten by the next sync. Review
changes to `access-policies.yml` like code — each entry is a
person who can command a telescope.

## Failure modes

- **Tunnel process dies** — the local Cocoa GUI is unaffected;
  remote SPAs show "disconnected" and auto-reconnect (2 s backoff)
  once the tunnel is back. launchd restarts it automatically.
- **Cloudflare edge incident** — same as above. For critical runs,
  keep the observatory-VPN path documented as a fallback.
- **IdP outage** — sessions with valid cookies keep working until
  the TTL expires; new logins fail until the IdP recovers.

## Cost

Free for up to 50 Zero Trust users; bandwidth through the tunnel is
included. The `logs` topic is the chattiest stream and is rate-capped
at 100 entries/s server-side, so sustained bandwidth stays trivial.
