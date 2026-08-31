# lco-instrument-web

Browser control surface for the LCO Cocoa instrument apps (ADC, DCU,
PFS, …). Talks the [instrument WebSocket protocol](https://github.com/carnegie-observatories/lco-ansible/blob/main/docs/ws-protocol.md)
— JSON frames, topic subscriptions, command/ack — to the WS server
each Cocoa app embeds.

No build step. Vanilla ES modules, static files, one optional Python
dev server.

## Quick start

```sh
uv sync                      # once — creates .venv with pyyaml + xib2ir
python3 server.py            # serves on http://localhost:8080/ (stdlib only)
```

The static server is stdlib-only; the [uv](https://docs.astral.sh/uv/)
environment is for the Python tooling (`xib2ir`, the Access policy
sync) — run those with `uv run …`.

Open <http://localhost:8080/> — the landing page lists the known
instruments (ADC 52403, DCU 51703, PFS 51603) and opens the SPA
against the one you pick. Direct links skip the chooser:

```
http://localhost:8080/app.html?host=<instrument-host>&port=<ws-port>
```

The corresponding Cocoa app must be running; its WS server starts
with the app.

## What's in the SPA

Two views, toggled in the header:

- **Window** — position-faithful re-creation of the Cocoa app's
  windows, generated from the XIBs. Multi-window apps (PFS) get a
  sub-tab per window (Camera, Calibration, …). Live topic-driven
  state, buttons and popups dispatch commands.
- **Diagnostic** — raw protocol surface: topic snapshot dump,
  generic command form, live log pane streaming the app's `logs`
  topic. The first thing to open when something misbehaves.

FITS-producing instruments (PFS today) additionally get a **Quick
Look** sub-tab in the Window view: the imageweb gateway's viewer
(see [imageweb/README.md](imageweb/README.md)) embedded
in the SPA. Its frame stream is opened only while the tab is active —
an instance parked on the Camera tab transfers no pixels — and
`?tab=quicklook` pins a browser window to it.

## Repo layout

| Path | Purpose |
|---|---|
| `index.html`               | landing page (instrument chooser) |
| `app.html`                 | the SPA shell (Window + Diagnostic views) |
| `ws.js`                    | WS transport: connect, topic stores, `cmd()` dispatch |
| `renderer.js`              | Window view — builds DOM from layout JSON, binds topics/commands |
| `window-host.js`           | sub-tab strip; mounts one renderer per manifest window |
| `diagnostic.js`            | Diagnostic view |
| `instruments/<app>/`       | per-app `manifest.json` + `bindings/*.yml` |
| `generated/<app>/`         | per-window layout JSON, produced by xib2ir — committed, don't hand-edit |
| `tools/xib2ir/`            | XIB → layout-JSON converter (Python, stdlib-only) |
| `imageweb/`                | science-frame quick-look gateway (control-WS triggered CHZ1 streaming + viewer) |
| `docs/plans/`              | design plans (protocol, XIB conversion, per-app audits) |

## Regenerating a layout

Layout JSON is generated from the Cocoa app's XIB plus a bindings
YAML, then committed. After changing either:

```sh
uv run xib2ir extract <path-to>.xib \
    --window <xib-window-id> --app <app> \
    --bindings instruments/<app>/<window>/bindings.yml \
    -o generated/<app>/<window>.layout.json
```

(`xib2ir` is installed into the uv environment as a workspace
member — run from the repo root, no `cd tools/xib2ir` needed.)

See [tools/xib2ir/README.md](tools/xib2ir/README.md) for the
per-instrument invocations and the lint (drift-check) mode.

## Adding an instrument

1. Cocoa side: give the app a WSServer + InstrumentService +
   InstrumentRouter (see the [migration plan](docs/plans/ws-migration-plan.md)).
2. Write the Step 0 audit (outlet → command/topic matrix).
3. `instruments/<app>/manifest.json` — one entry per window.
4. `instruments/<app>/<window>/bindings.yml` — outlet bindings.
5. Run xib2ir; commit `generated/<app>/<window>.layout.json`.
6. Add a card to `index.html`.

## Deployment

Two supported models — full comparison in
[security-models.md](https://github.com/carnegie-observatories/lco-ansible/blob/main/docs/plans/security-models.md):

- **On the observatory VPN** — serve these static files from any HTTP
  server on the segregated network; the SPA connects `ws://` straight
  to the instrument host. Zero external dependencies.
- **Cloudflare Tunnel + Access** — for remote operators, without a
  VPN client and without opening inbound ports on the instrument
  Mac. See [docs/deploy-cloudflare.md](docs/deploy-cloudflare.md).

## Development notes

- `node --check *.js` is the only syntax gate; there is no bundler.
- The Diagnostic view works against any server speaking the protocol
  — including a non-Cocoa one — making it the reference client for
  new server implementations.
- Wire-format traps (JSON booleans, YAML `on:` keys, NaN) are
  documented in the [protocol reference](https://github.com/carnegie-observatories/lco-ansible/blob/main/docs/ws-protocol.md)
  § Wire-format gotchas. Read it before writing bindings YAML.
