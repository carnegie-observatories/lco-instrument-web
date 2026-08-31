# Instrument image viewer — readout-triggered WebSocket bridge — Plan

**Status:** Implemented for PFS through staging step 4, 2026-08-30 —
`imageweb/` (the gateway), the SPA Quick Look tab, and the deploy
config; emulator-rig verification and the sbs deploy (step 5) pending.
Depends on the
`exposure_complete` event already shipped in the PFS WS migration
(fires post-FITS-close with `fits_path`; see
[ws-migration-pfs-plan.md](ws-migration-pfs-plan.md)). Reference
implementation to copy from: the gcam web guider bridge in
`~/workspace/zwo/src/web/` ([design doc](../../../zwo/docs/plans/gcam-web-viewer.md)).

## What this is

A bridge process and a browser page that show each science frame as it
comes off the CCD:

- The bridge is a **client of the instrument's control WebSocket** (the
  same JSON protocol this SPA speaks). When the app announces a
  completed readout — the `exposure_complete` event, which
  CameraController emits after the FITS is closed on disk — the bridge
  reads that FITS from the local disk, encodes it with
  [chz1](https://github.com/astro-ph-labs/astro-ph), and pushes it to every
  connected browser over its own WebSocket.
- The page is the [@astro-ph-labs/viewer](https://github.com/astro-ph-labs/astro-ph)
  assembly gcamweb already proved out: WebGPU renderer, client-side
  stretch/colormaps, panner + magnifier, imexam, histogram — real
  16-bit ADU in the browser, not a server-rendered JPEG.
- The operator reaches it as a **Quick Look sub-tab inside the
  instrument SPA** (`app.html`, next to Camera/Calibration), which
  embeds the bridge's page and — the point of the tab — **opens the
  frame stream only while the tab is active**. An instance parked on
  the Camera tab transfers no pixels. See "The Quick Look tab" below.

```
instrument Mac ───────────────────────────────────────────────────┐
  PFS.app                                                         │
    ├─ control WS :51603  ── events/state ──┐                     │
    └─ FITS → /data/…/pfs0042.fits          │ (localhost client)  │
                                            ▼                     │
  bridge  imageweb  :8766                                         │
    ├─ WS client of :51603   (exposure_complete → fits_path)      │
    ├─ reads the FITS from disk, chz1-encodes                     │
    └─ serves /image/pfs/  (viewer page + CHZ1 frame WS)          │
──────────────────────────────────────────────────────────────────┘
        └─ browser  (direct on the VPN, or via the Cloudflare
           Tunnel: same hostname, ingress path ^/image(/.*)?$)
```

Everything in the dashed box is one machine. The control WS is the
*only* trigger; the FITS bytes never cross the control channel
(ws-migration-plan.md's rule 5 stands) and never cross the LAN raw —
they leave the machine only as CHZ1 frames on the bridge's port.

## Why this shape

**Why a bridge, not an ObjC ImageHTTPServer.**
ws-migration-plan.md originally sketched an in-app HTTP port (`+3`)
serving raw FITS to a JS9-style client. That design had two costs: a
new `Network.framework` HTTP server inside every Cocoa app, and raw
FITS on the wire (a full PFS frame is 10560×10560 ≥ 220 MB — unusable
remotely, heavy even on the LAN). The bridge needs **zero Cocoa
changes**: the `exposure_complete` event with `fits_path` already
ships, the bridge runs on the same Mac, and the path is local. chz1's
lossy tier turns 220 MB into single-digit MB with known,
header-carried added noise. The migration plans have since been
revised to point here: **the gateway is the image path, always** —
`ImageHTTPServer`, the `+3` ports, and the `image_ready`/`fits_url`
shapes are withdrawn, and nothing HTTP is ever built into the ObjC
apps.

**Why a control-WS client, not a directory watcher.** The event is the
protocol's own "readout finished, file is closed" signal, emitted after
`[fits close:1]` — no fsevents races with a file still being written,
no filename conventions to parse, and the same trigger works for every
instrument that adopts the protocol. (A `--watch <dir>` directory
fallback stays out of scope; propose it only if an instrument ever
lacks the event.)

**Why copy gcamweb rather than extend it.** The transport
(`stream.py`: chz1 pipeline, credit-flow WS handler, isolation
headers) and the viewer page carry over nearly verbatim. What differs
is the *source* and the *cadence*: gcam pulls a continuous 1–5 Hz
stream from a socket; here frames are **event-driven and minutes
apart**, which flips two design points (latest-frame replay and
per-client delivery, below). The copy is **temporary and
standardized** — see "Shared transport" below and the TODO list: the
boundary is drawn so the file goes upstream to chz1 unmodified, and
until then it is copied, not forked.

## Shared transport: the standardized boundary

`stream.py` began life as chz1's reference server (`python/server.py`,
which the wheel does not ship); gcamweb adapted it, imageweb copies
gcamweb's. Three bridges from one file is the signal it belongs
upstream. To make that possible *later* while copying *now*, the copy
is governed by a contract, not by convenience:

- **The file stays source-agnostic.** Everything instrument- or
  gcam-specific lives outside it. The only interface it may assume of
  a frame source is the protocol gcamweb already implements de facto —
  frozen here as the standard:

  ```python
  class FrameSource(Protocol):
      async def get(self, last_seq: int) -> tuple[int, Frame]
      def add_client(self) -> None
      def remove_client(self) -> None
      def status(self) -> dict
  ```

  (`get(last_seq)` is the one generalization over gcamweb's `get()`:
  seq-keyed delivery is what lets a late joiner replay the latest
  frame and lets every client see every frame — and it is what gcam's
  own `fits <timeout>` wire verb does, so it fits gcamweb too.)
- **Per-connection `Pipeline`**, created by `ws_handler` from
  `app["settings"]` per client instead of shared via
  `app["pipeline"]`. This fixes gcamweb's documented per-process
  config / shared-frame-rate limitation in the shared file, where the
  fix belongs — not as an imageweb-only patch.
- **No imageweb imports in `stream.py`, no gcamweb imports either.**
  `Frame`, `Settings`, `Pipeline`, `ws_handler`, `isolation_headers`
  and the `FrameSource` protocol are the module's entire public
  surface; bridges import from it, never the reverse. Anything a
  bridge needs to inject travels through the existing extension
  points (`Frame.extra`, `Settings`, the source).
- **The copy is byte-identical between the two repos** — since
  2026-08-31 including the docstring, which now speaks only in chz1
  terms (PR prep: repo-specific cross-references removed). Divergence
  is a bug; a change needed by one bridge is made in both (they are
  two files for repo-logistics reasons only). This is what makes the
  eventual upstreaming a move, not a merge.

Upstreaming happened 2026-08-31: the module lives on the astro-ph
checkout's `chz1-stream` branch as `chz1.stream` (wheel +
`chz1[stream]` aiohttp extra), and **both bridges now import it — the
local copies are deleted**. Until the branch merges, the checkout must
have `chz1-stream` checked out; after the merge nothing changes for
the bridges.

## The bridge (`imageweb`)

A uv package next to this repo's `server.py` — or in `tools/` — laid
out like gcamweb (`server.py` CLI + sub-apps, `source.py`, `stream.py`
adapted, `static/`). One process serves every instrument on the Mac:

```sh
uv run imageweb --instrument pfs@localhost:51603 \
                --instrument swope@localhost:51203
open http://127.0.0.1:8766/image/pfs/
```

| path | |
|---|---|
| `/image/` | landing page: instruments, last-frame seq/age |
| `/image/pfs/` | the viewer for one instrument |
| `/image/pfs/ws` | CHZ1 frame stream (binary out, `ack`/`config` in) |
| `/image/pfs/status` | JSON WS ~1 Hz: control-WS state, exposure/readout progress, last frame |
| `/image/pkg/chz1/`, `/image/pkg/viewer/` | the JS packages, shared |

All page URLs relative to the page's own directory, so the prefix is
the server's business alone — the pattern that lets gcamweb sit behind
the tunnel with a one-line ingress rule.

### Control-WS client (`control.py` — the new code)

An asyncio client of the instrument protocol (aiohttp
`ws_connect`; the frame shapes are the ones `ws.js` speaks):

- On connect: consume `hello` (app name, version → shown on the
  landing page and in `status`), then
  `{"type":"subscribe","topics":["exposure","readout"]}` — events are
  broadcast to all clients regardless, but the two topics feed the
  status channel (exposing/remaining, readout progress bar) for free.
- On `{"type":"event","name":"exposure_complete","data":{"id":…,"fits_path":…}}`:
  hand `fits_path` to the frame source. Everything else is ignored —
  the bridge sends **no `cmd` frames, ever**; it is a read-only
  presence on the control WS (the gcamweb rule about gcam's command
  port, transplanted).
- Reconnect with backoff; the control WS accepts multiple clients so
  the bridge does not displace the SPA or scripts.

### Frame source (`source.py`)

- On `fits_path`: open with astropy from the local disk — the path is
  **local and absolute by contract** (settled below under "fits_path
  contract"; the bridge runs on the Mac that wrote the file). `uint16`
  conversion as gcamweb's `_parse` does; forward **every
  non-structural header card verbatim** plus card comments
  (`fits.cards` / `fits.comments` in the frame header — same
  "display verbatim, derive nothing" rule, same >2⁵³-as-string
  JSON armor).
- **Latest-frame replay.** gcamweb's source hands each frame out once
  and drops history — right for a 5 Hz stream, wrong here: a browser
  opened between readouts must not stare at a black canvas for the
  length of an exposure. The source keeps the newest decoded frame;
  a new WS client is served it immediately (seq'd, so the credit flow
  is untouched), then waits for the next event.
- **Per-client delivery.** gcamweb's known limitation — N viewers
  share the frame rate, config is per-process — is unacceptable here
  (a frame every few minutes *must* reach every viewer). Frames are
  rare and encoding is fast relative to the cadence, so encode
  per-client at each client's own operating point: each WS connection
  gets its own `Pipeline` over the shared decoded ndarray — the
  per-connection-pipeline shape specified in "Shared transport" above,
  so gcamweb inherits the fix when the file syncs.
- The source keeps exactly **one** decoded frame. A "previous frame"
  history (`--keep N`) is explicitly deferred — not in the MVP, not in
  fit-and-finish; revisit only if operators ask after first light.
- **Lazy decode.** With zero viewers connected the event handler just
  records `fits_path` + seq; the FITS is opened and decoded on the
  first `add_client()` (or on the next event while clients exist). A
  bridge nobody is looking at does no work beyond holding its control
  WS — the same idle-costs-nothing property gcamweb gets from
  disconnecting its pull socket.

### Sizing (PFS full frame, the worst case)

`dimx = 8 × 1320`, `dimy = 10560` → 10560² × 2 B ≈ 223 MB raw
(binned readouts and windowed read modes are proportionally smaller;
LO_MEM builds halve each side). Scaling gcamweb's measured tiers:

| tier | ~wire size | note |
|---|--:|---|
| lossless | 30–75 MB | LAN-only; the "inspection" tier |
| bin 2 + q 0.5 | ~8 MB | |
| bin 4 + q 0.5 | ~2 MB | **default** — comfortable remotely |

Client default `bin 4, q 0.5`, toggle in the page down to lossless —
negotiated per client via chz1's `accept`/`config` handshake exactly
as today, so a client that never opts in gets lossless (fine on the
LAN, their wait).

One real constraint gcamweb never hit: **WebGPU
`maxTextureDimension2D` defaults to 8192**, and a full unbinned PFS
frame is 10560 wide. At the default tier this never bites (bin 2 →
5280). For the lossless look at a full frame the page must request
the higher device limit at adapter init (hardware allows 16384
everywhere that runs WebGPU) and fall back to bin 2 with a visible
notice if denied. Flag this in the viewer bring-up step.

Encode/decode latency at 223 MB raw is seconds, not the guider's
milliseconds — irrelevant against a minutes-long exposure cadence,
but the page should show "readout N% → encoding → downloading"
from the status channel rather than appearing hung.

### The page

Serves two ways from one implementation: standalone at
`/image/<app>/` (deep link, the landing page) and embedded as the
SPA's Quick Look tab. gcamweb's `static/` assembly minus the guider
layer:

- viewer core as-is: renderer, stretch, colormaps, imexam, histogram,
  panner/magnifier, `?` help.
- **Header panel** instead of the guider panel: the FITS cards
  verbatim under their own comments (OBJECT, EXPTYPE, EXPTIME, RA/DEC,
  AIRMASS, BINNING, filename…), fed from the frame header — the same
  `guider-panel.js` verbatim-cards machinery, re-grouped.
- **Status strip** from `/status`: control-WS connection state,
  "exposing — 34 s left", readout progress, "frame #42 · 3 min ago".
  Stale greying keyed on age, as gcamweb does.
- No sparklines, no guide box.
- Two host signals, both closing the frame WS (viewer state and the
  last-rendered canvas stay): the embed protocol below, and its own
  `document.visibilityState` — a backgrounded browser tab stops
  transferring too. Latest-frame replay is what makes this free: on
  reactivation the socket reopens and the newest frame arrives
  immediately, so gating costs one reconnect, never a stale view.

## The Quick Look tab (SPA integration)

The usage mode this serves: an operator opens **a couple of
`app.html` instances, each parked on the tab they need** — one on
Camera, one on Quick Look, maybe one Diagnostic. Renderer tabs stay
mounted-but-hidden with their topic subscriptions flowing (cheap JSON;
[window-host.js](../../window-host.js) is right to do that). The Quick
Look tab is the opposite case — its stream is the expensive part — so
it gets the inverted rule: **the frame WS exists only while the tab is
the active one in its page instance.**

- **Manifest-declared, no per-app SPA code.** A new entry kind in
  `instruments/<app>/manifest.json`:

  ```json
  { "id": "quicklook", "title": "Quick Look", "embed": "/image/pfs/" }
  ```

  window-host mounts entries with `embed` as an iframe pane instead of
  fetching a layout for renderer.js. Instruments without a bridge
  simply don't declare one. The URL is resolved the same way
  index.html's `data-path` guider cards are: tunnel mode → the path on
  the page's own hostname; local dev → the bridge's
  `http://127.0.0.1:8766` origin.
- **Gating protocol.** The iframe is created on first activation
  (lazy-mount, like every other tab) and *kept* on deactivation —
  window-host posts `{quicklook: "active" | "inactive"}` to it on tab
  switches, and the page closes/reopens its frame WS accordingly.
  Keeping the iframe preserves zoom/stretch/colormap and the last
  frame on screen; only the socket comes and goes. Bridge side this is
  `remove_client`/`add_client`, so an all-inactive bridge encodes
  nothing (and decodes nothing — lazy decode above).
- **Tab pinning across instances.** Today's last-tab persistence is
  localStorage per app — shared by every instance of the page, so two
  pinned instances would fight over it on reload. Add a `?tab=<id>`
  URL parameter that overrides localStorage (and is written back to
  the address bar on manual tab switches via `replaceState`), so each
  browser window keeps its tab across reloads and tabs become
  bookmarkable. Small window-host change, useful beyond quick-look.
- **Optional refinement, not MVP:** keep the ~1 Hz `/status` WS open
  even while inactive (tiny JSON) and badge the tab button — "frame
  #43" — so a parked Camera instance shows there's a new image without
  paying for its pixels.

**Cross-origin isolation is the one real constraint.** The decode
pool needs `crossOriginIsolated`, which an iframe only gets when the
*top-level* document also sends COOP/COEP. Behind the tunnel
everything shares one origin (instruments are paths on the telescope
hostname), so the fix is two headers added in `server.py` next to the
existing `Cache-Control: no-store` — safe, since every SPA subresource
is same-origin. On the direct-VPN model the SPA (`:8080`) and bridge
(`:8766`) are *different origins* (port counts), and an embedded
cross-origin isolated iframe cannot work; there the top-level
page must NOT send COOP/COEP (a COEP parent refuses a cross-origin
iframe whose CORP says same-origin), and without them the iframe
embeds fine — the viewer detects `crossOriginIsolated === false` and
falls back to inline decode (a fallback it already carries). So
`server.py` sends the isolation headers only on requests forwarded by
cloudflared (`Cf-Ray`), and the same manifest entry is graceful in
both models: full decode pool behind the tunnel, inline decode on the
VPN/dev origin split.

## Deployment

Same three layers as everything else here, strictest path only:

- Bridge binds `127.0.0.1:8766`. On the VPN model, bind the LAN
  address instead and browsers hit it directly.
- Tunnel: one ingress rule per Mac running a bridge, before the SPA
  catch-all, exactly like `/guider`:

  ```yaml
  - hostname: sbs.chimera.observer
    path: ^/image(/.*)?$
    service: http://127.0.0.1:8766
  ```

- Access: covered by the existing per-telescope application (path
  rules live under the same hostname) — no policy change.
- `index.html`: one card per instrument with `data-path="/image/pfs/"`,
  using the existing tunnel-aware card rewrite (the card is the deep
  link to the standalone page; the primary surface is the SPA tab).
- `server.py`: add the COOP/COEP isolation headers (Quick Look iframe
  requirement, above).
- launchd/ansible service unit alongside the app it watches — same
  open item as gcamweb's.

The COOP/COEP isolation headers ride along from `stream.py`
(the decode pool needs them) and pass through the tunnel — verified
by gcamweb.

## Staging

1. **Bridge MVP against PFS.** `control.py` + `source.py` + the
   `stream.py` copy (generalized per "Shared transport", mirrored back
   into gcamweb in the same change); verify with the DataSimulator
   rig: snap → event →
   frame in the diagnostic canvas page (chz1's example page, no
   viewer yet). Exit: an image appears in a browser within seconds of
   readout end, unattended, across app restarts on either side.
2. **Per-client pipelines + latest-frame replay.** Two browsers at
   different tiers; late joiner gets the last frame immediately.
3. **Viewer page.** The gcamweb assembly with the header panel and
   status strip; the WebGPU texture-limit check for lossless
   full-frames; visibility-gated socket.
4. **Quick Look tab.** Manifest `embed` kind + iframe pane +
   active/inactive postMessage in window-host; `?tab=` pinning;
   COOP/COEP in `server.py`. Verify the gating: park an instance on
   Camera through a readout and confirm zero frame-WS traffic; switch
   to Quick Look and confirm the replayed frame appears at once.
5. **Deploy.** Ingress rule, index card, service unit; try on sbs
   end-to-end through Access — including the iframe isolation headers
   through the tunnel.
6. **Second instrument** (Swope or Henrietta when their WS lands) —
   proves the multi-instrument sub-app shape and that nothing is
   PFS-specific beyond the config line and one manifest entry.

## Decided

- **`fits_path` contract: local absolute path.** This settles
  ws-migration-pfs-plan.md's open question 1: the path is absolute and
  refers to the filesystem of the Mac emitting the event; the bridge
  runs on that same Mac and opens it directly. If a future instrument
  writes to a NAS not mounted identically, add a `--data-root-map`
  remap flag then, not now.
- **`--keep N` deferred.** One decoded frame in memory; multi-loop
  sequences simply replace it. No history until operators ask.
- **Transport sharing: standardized copy now, upstream later.** The
  contract in "Shared transport" above; the debt is tracked in the
  TODO list below.
- **Quick Look is a SPA sub-tab, transfer gated on activation.** The
  frame WS exists only while the tab is active (and the page
  visible); an instance parked on another tab moves no pixels. The
  standalone `/image/<app>/` page remains as the implementation and
  the deep link.

## TODO — temporary measures to retire

Tracked here so a copy cannot quietly become a fork:

- [x] ~~`stream.py` copies in both bridges~~ **Retired 2026-08-31**:
      both bridges import `chz1.stream` from the astro-ph checkout's
      `chz1-stream` branch (the module ships in the wheel with a
      `chz1[stream]` aiohttp extra; `docs/stream.md`, `stream_host.py`
      and `test_stream.py` ride the same PR — 9-check gate, plus both
      smoke suites and a live PFS snap on the packaged module).
      Remaining step: push the branch and merge the PR; the bridges
      need no further change.
- [ ] **Viewer page assembly** (`static/` wiring of @astro-ph-labs/{core,viewer})
      is also near-duplicated from gcamweb. Lower priority — it is
      wiring, not protocol — but fold the common assembly into
      viewer's examples upstream when touching it next.

## Open questions

1. ~~**Where the code lives.**~~ **Decided**: `imageweb/` at this
   repo's root, a uv workspace member — it is a project deliverable
   (not a `tools/` utility) and the deploy story (ingress, cards,
   Access) is this repo's.
2. **Subraster/window reads.** Windowed readouts produce non-full
   geometry; chz1 and the viewer are size-agnostic, but confirm the
   header cards carry enough (windowX1/X2) to label the view.
3. **Tab badge.** Is the always-on `/status` "new frame" badge on the
   tab button (optional refinement above) worth its permanent ~1 Hz
   trickle, or should an inactive Quick Look tab be fully silent?
