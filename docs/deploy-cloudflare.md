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

One hostname per telescope on the `chimera.observer` zone;
**instruments are paths on that hostname**:

| Telescope | Hostname | Instrument WS endpoints |
|---|---|---|
| Clay  | `clay.chimera.observer`  | `/pfs/ws`, `/dcu/ws`, `/adc/ws`, … |
| Baade | `baade.chimera.observer` | `/dcu/ws`, … |
| Swope | `swope.chimera.observer` | `/swope/ws`, … |
| SBS *(test)* | `sbs.chimera.observer` | `/adc/ws`, … |

**Why paths and not per-instrument subdomains**: Cloudflare's
Universal SSL certificate covers `chimera.observer` and
`*.chimera.observer` **one level deep only**. A second-level name
like `adc.sbs.chimera.observer` gets no TLS certificate — the
browser's `wss://` handshake fails before Cloudflare even routes
the request (observed live: `sslv3 alert handshake failure`).
Multi-level wildcard certs require Advanced Certificate Manager
($10/mo/zone); paths cost nothing and keep every instrument under
the telescope's single, certificate-covered hostname.

A **test telescope** (SBS) uses the same machinery end-to-end —
tunnel, DNS, Access application — against a development Mac
instead of a summit machine. Use it to rehearse a deployment
change, onboard a new instrument, or demo the SPA without touching
a production telescope. See § Test telescope walkthrough.

- The hostname's root serves the SPA static files (landing page + app).
- Each instrument's WebSocket rides `/<app>/ws` on the same
  hostname (image-transfer endpoints will follow the same pattern,
  `/<app>/fits/...`).
- One **Access application per telescope**, on the one hostname —
  a single allowed-email list governs every instrument on that
  telescope, current and future. The lists live in
  [`deploy/access-policies.yml`](../deploy/access-policies.yml) and
  are pushed by [`deploy/sync-access-policies.py`](../deploy/sync-access-policies.py)
  (see § Access policy as configuration).

## Prerequisites

- A Cloudflare account with the `chimera.observer` zone. The free
  plan includes Zero Trust for up to 50 users.
- An identity provider configured under **Zero Trust → Settings →
  Authentication**: the **Google** IdP (generic Google OAuth).
  Carnegie accounts are Google accounts, so operators sign in with
  their `@carnegiescience.edu` Google login and inherit whatever
  MFA Carnegie's Workspace enforces at Google's door; the Access
  policy still gates entry to allowed addresses. Setup:
  1. In [Google Cloud Console](https://console.cloud.google.com),
     create a project → **OAuth consent screen** (External) →
     **Credentials → OAuth client ID** (*Web application*) with the
     authorized redirect URI
     `https://<team>.cloudflareaccess.com/cdn-cgi/access/callback`.
  2. In Zero Trust → **Settings → Authentication → Login methods →
     Add new → Google**, paste the client ID + secret, save, and
     use the built-in **Test**.
  3. **Delete the default One-time PIN login method** — email OTP
     bypasses MFA and authenticates anyone who can read a mailbox,
     which is a weaker factor than the policy deserves.

  (The dedicated **Google Workspace** IdP adds group-based policies
  and tenant enforcement but requires admin consent on Carnegie's
  Workspace tenant — adopt it if/when Carnegie IT grants that;
  the generic Google IdP is the strictest option available without
  tenant admin.)
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

One tunnel, one hostname; one path rule per instrument:

```yaml
tunnel: <UUID>
credentials-file: /Users/<you>/.cloudflared/<UUID>.json

ingress:
  # --- instrument WebSockets: /<app>/ws → local WS port ---
  - hostname: clay.chimera.observer
    path: ^/pfs/ws$
    service: http://localhost:51603
  - hostname: clay.chimera.observer
    path: ^/dcu/ws$
    service: http://localhost:51703
  - hostname: clay.chimera.observer
    path: ^/adc/ws$
    service: http://localhost:52403

  # --- everything else on the hostname → SPA static files ---
  - hostname: clay.chimera.observer
    service: http://localhost:8080

  # Required catch-all.
  - service: http_status:404
```

The Cocoa WSServer accepts the upgrade regardless of the request
path, so `/pfs/ws` forwarded verbatim to `localhost:51603` works
without app-side changes.

If an instrument's Cocoa app runs on a *different* Mac than the
gateway, run a `cloudflared` replica **on that Mac** carrying that
instrument's path rule against `localhost` — do not proxy the WS
across the LAN in cleartext from the gateway
(`service: http://pfs-mac.local:51603`). Replicas of the same
tunnel share the hostname; each Mac only fronts its own local
ports, and instrument traffic never crosses the LAN unencrypted.

Rules match top-to-bottom; the catch-all must be last. WebSocket
upgrade is proxied automatically — no special flag. Validate:

```sh
cloudflared tunnel ingress validate
cloudflared tunnel ingress rule https://clay.chimera.observer/pfs/ws
```

### 4. Route DNS

One-time setup — a single record per telescope:

```sh
cloudflared tunnel route dns clay-telescope clay.chimera.observer
```

No wildcard, no per-instrument records — instruments are paths, so
DNS never changes after this step.

### 5. Apply the Access policy

Do **not** hand-create the Access application in the dashboard —
it's generated from configuration (next section) so the allowed
list is reviewable and versioned:

```sh
export CLOUDFLARE_API_TOKEN=...   # "Access: Apps and Policies Write"
uv run python deploy/sync-access-policies.py --telescope clay
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
the instrument chooser. On an HTTPS page the cards rewrite
themselves to path-mode automatically —
`app.html?ws_path=/pfs/ws` — and the SPA connects
`wss://clay.chimera.observer/pfs/ws` (host defaults to the page's
own hostname).

## Adding an instrument later

The per-instrument Cloudflare overhead is deliberately minimal:

| Piece | Change |
|---|---|
| Access policy | **none** — the telescope's app covers the hostname, and every instrument is a path on it |
| DNS           | **none** — one record per telescope, ever |
| TLS           | **none** — the hostname is already covered by Universal SSL |
| `config.yml`  | **one ingress rule** (`path: ^/<app>/ws$` → local port) and a tunnel restart |
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
  - hostname: sbs.chimera.observer
    path: ^/adc/ws$
    service: http://localhost:52403
  - hostname: sbs.chimera.observer
    service: http://localhost:8080
  - service: http_status:404
EOF
cloudflared tunnel ingress validate

# 4. DNS — one record, one time
cloudflared tunnel route dns sbs-telescope sbs.chimera.observer

# 5. Access policy (sbs entry ships in deploy/access-policies.yml)
export CLOUDFLARE_API_TOKEN=...
uv run python deploy/sync-access-policies.py --telescope sbs

# 6. run
cloudflared tunnel run sbs-telescope     # foreground for a test box;
                                         # `sudo cloudflared service install`
                                         # if it should persist
```

Then, with the instrument app + `python3 server.py` running:
open `https://sbs.chimera.observer/`, log in with a
`@carnegiescience.edu` address, and click the ADC card — on HTTPS
the landing page rewrites it to path-mode automatically and the
SPA connects `wss://sbs.chimera.observer/adc/ws`.

**Verification checklist:**

- [ ] Access login page appears before any content (step 5 ran
      before the hostname was shared).
- [ ] A non-allowed email is refused.
- [ ] Landing page renders; ADC card shows `/adc/ws`.
- [ ] SPA connects — `hello` arrives, topics populate in the
      Diagnostic view, log pane streams.
- [ ] A command round-trips (`ack` in the log pane).
- [ ] Kill `cloudflared`, restart it — SPA auto-reconnects within
      a few seconds.

**Tear-down** (a test telescope shouldn't outlive its test):

```sh
cloudflared tunnel delete sbs-telescope   # after stopping it
# then remove the DNS record in the dashboard, and delete the
# sbs Access app in Zero Trust → Applications. No orphans: every
# artifact of the test is gone when the test is.
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
uv run python deploy/sync-access-policies.py                 # all telescopes
uv run python deploy/sync-access-policies.py --telescope clay
uv run python deploy/sync-access-policies.py --dry-run       # print API payloads only
```

The script is idempotent — it finds each telescope's Access
application by domain and updates it in place, or creates it on
first run. One application per telescope covering
`<domain>` + `*.<domain>`; one *Allow* policy per application,
rebuilt from the YAML every run. An empty `allowed:` list is
refused (it would lock everyone out) unless `--allow-lockout` is
passed.

The script runs in the repo's [uv](https://docs.astral.sh/uv/)
environment (`uv sync` once, then `uv run python
deploy/sync-access-policies.py …`) and needs a
`CLOUDFLARE_API_TOKEN` environment variable — created as follows.

### Creating the API token

Use an **account-owned token** — it belongs to the observatory's
Cloudflare account, not to any individual, so it survives staff
turnover and is centrally revocable. (User-owned tokens exist but
are not used here.) Account tokens live at dashboard →
**Manage Account → Account API Tokens**; the menu is only visible
to Super Administrators — have one create it.

1. **Create Token** → scroll past the templates to
   **Create Custom Token** → **Get started** (there is no
   ready-made Access template — the custom builder is the right
   path, not a template).
2. **Token name**: something greppable, e.g.
   `access-policy-sync (chimera.observer)`.
3. **Permissions** — exactly one row, nothing else:
   - first dropdown: **Account**
   - second dropdown: **Access: Apps and Policies**
   - third dropdown: **Edit**
   (The dashboard shows *Edit*; API error messages call the same
   permission "Access: Apps and Policies Write" — they are the
   same grant.)

   There is no account-selection step: an account-owned token is
   scoped to the account it's created under, by construction.
   (The "Account Resources — Include" selector you may know from
   the *user*-token builder doesn't exist here — that selector
   only appears for user tokens, where one person can belong to
   several accounts.)
4. **Client IP Address Filtering**: *Is in* → the observatory's
   egress IP range. A leaked token is then useless off-site.
5. **TTL**: set an expiry — one year at most. Policy syncs are
   rare; re-creating the token on a calendar reminder is cheap,
   an immortal credential is not.
6. **Continue to summary** → confirm it lists exactly one
   permission, *Access: Apps and Policies: Edit*, on this account
   → **Create Token**.
7. **Copy the secret immediately** — it is shown exactly once.
   Store it in the observatory password manager (never in the
   repo, never in shell history — `export` it from the manager at
   sync time), then:

   ```sh
   export CLOUDFLARE_API_TOKEN=<the-secret>
   uv run python deploy/sync-access-policies.py --dry-run   # verify it works
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
