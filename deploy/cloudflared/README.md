# cloudflared ingress config, per telescope

`<telescope>/config.yml` here is the file installed as
`~/.cloudflared/config.yml` on that telescope's gateway Mac. It is
versioned so the "which path → which local port" table is reviewable
and so a rebuilt Mac can be restored without guessing.

## What is (and is not) in here

| File | Committed? | Why |
|---|---|---|
| `<telescope>/config.yml` | **yes** | tunnel UUID + ingress rules. The UUID is an identifier, not a credential — it is the DNS target (`<UUID>.cfargotunnel.com`), shows in `cloudflared tunnel list` and in logs, and grants nothing on its own. |
| `~/.cloudflared/<UUID>.json` | **never** | the tunnel secret. Holding it = running the tunnel = terminating `https://<telescope>.chimera.observer` on your own machine. |
| `~/.cloudflared/cert.pem` | **never** | account-scoped origin cert; creates/deletes tunnels and DNS routes. |

`.gitignore` refuses `deploy/cloudflared/**/*.json` and `cert.pem`;
keep it that way. If either ever lands in git history, rotate: delete
the tunnel (`cloudflared tunnel delete`) and re-run
`cloudflared tunnel login` — do not just remove the file.

## Install / update on the gateway Mac

```sh
cp deploy/cloudflared/<telescope>/config.yml ~/.cloudflared/config.yml
cloudflared tunnel ingress validate
sudo launchctl kickstart -k system/com.cloudflare.cloudflared   # restart the step-6 service
```

`credentials-file` is an absolute path, so it is per-Mac by nature
(the launchd service runs as root and cannot resolve `~`). Edit the
committed copy when the Mac or account changes; it is not a secret.

## Drift check

Before editing, confirm the Mac still runs what is committed:

```sh
diff ~/.cloudflared/config.yml deploy/cloudflared/<telescope>/config.yml
```

Edit the committed copy first, then install — never the reverse.
