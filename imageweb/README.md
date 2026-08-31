# imageweb — the instrument image gateway

Browser quick-look for the science frames of the LCO Cocoa instrument
apps. A Python bridge is a **read-only client of the instrument's
control WebSocket**: on the `exposure_complete` event (fired after the
FITS is closed on disk, carrying the local absolute `fits_path`) it
reads the file, streams it as [CHZ1](https://github.com/astro-ph-labs/astro-ph)
over its own WebSocket, and serves the viewer built on
[@astro-ph-labs/viewer](https://github.com/astro-ph-labs/astro-ph): WebGPU
renderer, client-side stretch/colormaps, panner + magnifier, imexam,
histogram, plus the **header panel** (`k`) showing the frame's FITS
cards verbatim and the exposure/readout state. `?` lists every key.
Design: [docs/plans/image-viewer-plan.md](../docs/plans/image-viewer-plan.md).

No Cocoa changes: the event already ships; nothing HTTP lives in the
apps (ws-migration-plan.md § Image transfer).

## Run

Needs [uv](https://docs.astral.sh/uv/) and the
[astro-ph monorepo](https://github.com/astro-ph-labs/astro-ph)
checkout at `../astro-ph-labs/astro-ph` (relative to this repository's
parent) — `chz1` is a path dependency, and the JS packages (`chz1`,
`core`, `viewer`) are mounted from the checkout (`--astro-ph` if it
lives elsewhere).

```sh
(cd ../astro-ph-labs/astro-ph && npm install && npm run build)  # once — builds core + viewer
uv sync                                   # once, from the repo root
uv run imageweb --instrument pfs          # control WS at localhost:51603
open http://127.0.0.1:8766/image/pfs/
```

One process serves any number of instruments, each
`--instrument NAME[@HOST[:PORT]]`; the control-WS port defaults from
the migration plan's table (pfs 51603, swope 51203, henrietta 52803, …).
The corresponding Cocoa app must be running; frames appear as readouts
complete, and a browser opened between readouts is replayed the newest
frame at once.

| path | |
|---|---|
| `/image/` | the list of instruments (and `/image/instruments.json`) |
| `/image/pfs/` | the quick-look viewer for that instrument |
| `/image/pfs/ws`, `…/status` | its frame stream and status channel |
| `/image/pkg/{chz1,core,viewer}/` | the JS packages, from the astro-ph checkout |

The prefix (`--prefix`, default `/image`) is the server's business
alone: the page uses only URLs relative to its own directory.

## Data on the wire

The page defaults to `bin 4 · q 0.5`: 16-bit linear ADU, binned 4×4,
dithered quantization at half the measured noise — a full 10560² PFS
frame lands at single-digit MB. `bin 2` and `lossless` are one select
away, negotiated per client (each connection has its own encode
pipeline; a client that never sends a `config` gets lossless). One
device limit to know: WebGPU's default `maxTextureDimension2D` is
8192, so an unbinned full PFS frame cannot be displayed at
`lossless`; the page refuses the tier with a notice instead of
failing (raising the device limit needs upstream viewer support).

## Transfer gated on viewing

- With **zero viewers** connected, an `exposure_complete` only records
  the path — no decode, no encode. The first viewer triggers the load.
- The page closes its frame WS while it is hidden (a backgrounded
  browser tab, or the SPA's Quick Look tab going inactive via
  `postMessage {quicklook: "active"|"inactive"}`) and reopens it on
  return; latest-frame replay makes that cost one reconnect, never a
  stale view.

## Behind a Cloudflare Tunnel

The gateway binds `127.0.0.1:8766` and speaks plain HTTP; cloudflared
forwards paths unchanged, so the ingress rule is the prefix
(see [deploy/cloudflared/sbs/config.yml](../deploy/cloudflared/sbs/config.yml)):

```yaml
  - hostname: sbs.chimera.observer
    path: ^/image(/.*)?$
    service: http://127.0.0.1:8766
```

Behind https the page opens its sockets as `wss:` (it follows
`location.protocol`). The COOP/COEP headers the decode pool needs are
set by the gateway and pass through.

## Layout

```
pyproject.toml           the package (uv workspace member); chz1 as a path
                         dependency on the astro-ph monorepo checkout
imageweb/server.py       CLI, the per-instrument sub-apps, landing page
imageweb/control.py      read-only control-WS client (events + exposure/readout topics)
imageweb/source.py       exposure_complete -> lazily decoded latest frame
imageweb/static/         index.html, app.js, header-panel.js, viewer.css

The CHZ1 transport (pipeline + credit-flow WebSocket handler) is
`chz1.stream`, imported from the astro-ph checkout — the module this
bridge and gcamweb grew and upstreamed (chz1's docs/stream.md).
```
