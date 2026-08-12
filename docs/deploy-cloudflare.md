# Deploying behind Cloudflare Tunnel + Access

Serves the SPA and the instrument WebSocket to remote operators —
no VPN client, no inbound ports on the instrument Mac, per-user
authentication at Cloudflare's edge. The higher-level comparison
with the VPN model is in
[security-models.md](https://github.com/carnegie-observatories/lco-ansible/blob/main/docs/plans/security-models.md).

How it works: `cloudflared` runs on the instrument Mac and opens an
**outbound-only** TLS connection to Cloudflare. Cloudflare Access
authenticates every request against your IdP before it is forwarded
down the tunnel. The Cocoa app is untouched — the tunnel connects to
its WS port on localhost.

## Prerequisites

- A Cloudflare account. The free plan includes Zero Trust for up to
  50 users.
- A domain (or delegated subdomain) on Cloudflare DNS.
- An identity provider configured under **Zero Trust → Settings →
  Authentication**. Google Workspace, Okta, Azure AD — or the
  zero-setup option, **One-time PIN over email**.
- The instrument Mac running the Cocoa app (WS port per the
  formula `50001 + PROJECT_ID×100 + 2`) and the SPA static server
  (`python3 server.py`, default port 8080).

## Per-instrument setup

All commands run on the instrument Mac. Example uses PFS
(WS port 51603) and the hostname `pfs.obs.example.com` — substitute
your own.

### 1. Install and authenticate cloudflared

```sh
brew install cloudflared
cloudflared tunnel login        # opens a browser; pick your zone
```

### 2. Create the tunnel

```sh
cloudflared tunnel create pfs-instrument
# prints a tunnel UUID; credentials land in ~/.cloudflared/<UUID>.json
```

### 3. Write `~/.cloudflared/config.yml`

```yaml
tunnel: <UUID>
credentials-file: /Users/<you>/.cloudflared/<UUID>.json

ingress:
  # WebSocket control surface. Path-based split: /ws → Cocoa app.
  - hostname: pfs.obs.example.com
    path: ^/ws$
    service: http://localhost:51603
  # Everything else → SPA static files.
  - hostname: pfs.obs.example.com
    service: http://localhost:8080
  # Required catch-all.
  - service: http_status:404
```

Rules match top-to-bottom; the catch-all must be last. WebSocket
upgrade is proxied automatically — no special flag. Validate with:

```sh
cloudflared tunnel ingress validate
cloudflared tunnel ingress rule https://pfs.obs.example.com/ws
```

### 4. Route DNS

```sh
cloudflared tunnel route dns pfs-instrument pfs.obs.example.com
# creates the proxied CNAME → <UUID>.cfargotunnel.com
```

### 5. Protect it with Access

In **Zero Trust → Access → Applications**:

1. **Add an application** → *Self-hosted*.
2. Domain: `pfs.obs.example.com`.
3. Session duration: 24 h is a sensible default.
4. Policy — e.g. *Allow* → *Emails ending in* `@obs.example.com`,
   or an IdP group.

Until this step the tunnel is publicly reachable — do it before
sharing the hostname.

### 6. Run as a service

```sh
sudo cloudflared service install
```

Installs a launchd job so the tunnel survives reboots. Logs go to
`/Library/Logs/com.cloudflare.cloudflared.err.log`.

### 7. Connect

Operators open `https://pfs.obs.example.com/`, pass the Access
login, land on the instrument chooser. In the chooser's
**Advanced** section, enter `pfs.obs.example.com` as host and leave
port empty — the SPA switches to `wss://<host>/ws` automatically
when the page is served over HTTPS and no port is given.

## Multiple instruments

One tunnel can carry several hostnames — add ingress rule pairs
(`adc.obs.example.com` → 52403 + its static server, etc.) and a
`cloudflared tunnel route dns` per hostname, then one Access app
per hostname. Or run one tunnel per instrument Mac; both layouts
are supported, pick whichever matches how the Macs are distributed.

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
