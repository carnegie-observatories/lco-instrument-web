# Migrate FourStar (camera window) to the WebSocket external interface

Status: plan, 2026-08-31. **Gated on merging
[PR #5](https://github.com/carnegie-observatories/lco-instrument-web/pull/5)
(imageweb quick-look gateway)** — the Quick Look half of this plan
builds directly on it. Nothing below starts until #5 is on `main`.

## Context

[ws-migration-plan.md](ws-migration-plan.md) declared FourStar out of
scope on the theory that its command server lives in a separate C
daemon (`src/StarServer/starserver.c`) and would need a C-side WS
library. That described the v2.2 Carbon app. **The v3 rewrite at
`~/workspace/fourstar/src/FourStar` is a Cocoa/ObjC app of exactly the
PFS shape** — same author, same MRC conventions, `src/Common` byte-
identical to PFS's except for the two WS files, singleton controllers
with a clean read surface (`CameraController +instance`, `getPars`,
`getRawData:Loop:`), exposure loop on a worker thread with
`assert([NSThread isMainThread])` in the controllers. StarServer in v3
is the *detector* server the GUI dials as an outbound client (one per
HAWAII-2RG chip), not an external command interface. The standard
PFS port — embedded `WSServer` + `Service/InstrumentService` +
`Service/InstrumentRouter` — applies unchanged. This plan supersedes
the out-of-scope note for FourStar.

Two things make FourStar *easier* than PFS was, one makes it harder:

- **No legacy TCP command server.** v3 has no inbound socket at all
  (everything in `Tcpip.m`'s server half is unused). There is no
  legacy-parity constraint and no dual-protocol transition period; the
  WS interface is net-new surface.
- **A full-stack simulator exists.** `starserver` built with
  `SIM_ONLY` (`src/StarServer/rsim.c`) synthesises realistic 2048²
  star-field frames; `H2RG_Controller.m` in sim mode dials four local
  instances and even auto-restarts them. End-to-end development —
  exposure loop, FITS on disk, WS events, quick look — needs no
  hardware.
- **Four chips.** FourStar is a 2×2 mosaic of 2048×2048 HAWAII-2RG
  detectors (`main.h` `N_CHIPS 4`, `CameraController.h`
  `DEF_DIMX/DEF_DIMY 2048`, `u_short` pixels, `PEDESTAL 1000`). One
  exposure loop writes **one FITS per chip** (basename from
  `create_filename(..., chip+1, ...)`, prefix `fsr`), to up to
  `N_FOLDERS 3` datapaths simultaneously. Per-chip fan-out shows up in
  the topics, in the `exposure_complete` event, and in the quick-look
  story (below).

### What's different from PFS

| | PFS | FourStar |
|---|---|---|
| Detector | 1 CCD via Archon | 4× H2RG via 4 StarServer connections |
| FITS per exposure | 1 | 4 (one per chip) × up to 3 datapaths |
| Legacy TCP server | yes (parity constraint) | none |
| Windows beyond camera | Calibration | Motors, H2RG, Temp, LN2, Telescope/SkyMap, Graph, Macros |
| Hardware-free testing | `DataSimulator` (partial) | full-stack StarServer `SIM_ONLY` |

Scope: **the "FourStar" main expose window in `Camera.xib` (XIB window
id `1`) only**, same discipline as the PFS port. Motors, H2RG,
Temp.Control, LN2, Telescope, Graph, and the macro editors are
deferred; the multi-window manifest handles them later without
protocol changes.

## Port assignment

`PROJECT_ID 11` (`src/FourStar/main.h:27`) → formula WS port
`50001 + 100·11 + 2` = **51103**.

**Collision:** `H2RG_Controller.m` already uses `STARGUI_PORT 51101`,
`SERVER_PORT 51102`, and — simulator mode only — `SERVER_PORT+i+1` =
**51103–51106** for the four local sim servers
(`H2RG_Controller.m:94`). 51103 is exactly the WS port, and sim mode
is the primary dev path.

Resolution: keep WS at 51103 (the suite-wide `+2` convention is baked
into index.html, cloudflared ingress, and imageweb's port table; a
one-off offset for FourStar would be a permanent trap) and **move the
sim-server offset** to `SERVER_PORT+11+i` → 51113–51116 in
`H2RG_Controller.m` plus the matching StarServer launch/restart
plumbing. Sim-only, localhost-only, a two-line change.

(PR #1 in the fourstar repo mentions port-60002 forwarding, IT-2444 —
unrelated plumbing; confirm it isn't repurposed for this.)

## Step 0 — Audit the camera window

Write `~/workspace/fourstar/docs/ws-migration-step0-fourstar-camera.md`
in the step-0 format ([ws-migration-step0-adc.md](ws-migration-step0-adc.md);
PFS's lives at `~/workspace/pfs/docs/ws-migration-step0-pfs-camera.md`):
window scope, outlet→controller coverage matrix for the "FourStar"
window in `Camera.xib`, proposed topic snapshots with real values from
a sim-mode run, and the enable/disable logic mirrored from
`CameraController.m` (the bindings YAML comments cite these line
numbers, per house style). No `RUN_SERVER`-gate section — there is no
legacy server to gate.

The audit fixes the exact topic schemas and command list; everything
below is the provisional shape.

## Topics (provisional)

| Topic | Source | Notes |
|---|---|---|
| `exposure` | `CameraController getPars` / status | exptime, nloops, loop index, exptype/macro state, run#, object, comment |
| `readout` | `H2RG_Controller` | per-chip: `chips: [{n, state, progress}, …]` |
| `disk` | `AppDelegate dataPaths` | per-datapath free space, active paths, next filename |
| `filters` | `MotorController` | filter + pupil wheel positions, moving flags |
| `temps` | `TempController` | summary only (detailed Temp window deferred) |
| `telescope` | `TeleController` | read-only mirror, like PFS |
| `logs` | `Logger onAppend` | identical ring/rate-cap mechanics as PFS |

Per-chip data goes **inside** one topic as arrays (mirroring
`SysPars`'s per-chip arrays), not as four topics — one snapshot diff,
one `state` frame.

Commands: reads mirror the topics; writes per the audit, expected
set: `set_exptime`, `set_nloops`, `set_object_name`, `set_comment`,
`set_run_number`, `start_exposure`, `stop_exposure`, filter/pupil
moves. Macro record/edit stays GUI-only for now; whether `start_macro`
makes the first cut is an audit-time call.

## Step A — `Service/InstrumentService` for FourStar

Copy-adapt PFS's (`~/workspace/pfs/src/PFS/Service/InstrumentService.{h,m}`):
transport-agnostic façade over `CameraController` / `H2RG_Controller`
/ `MotorController` (+ `TempController`, `TeleController` read-only),
`-initWithAppDelegate:`, snapshot reads returning JSON-safe dicts,
writes wrapped in `dispatch_sync(main queue)` — FourStar's controllers
carry the same main-thread asserts PFS's do. Carry over the two
PFS-only additions ADC/DCU lack, both required here:

- the **event sink** (`-setEventHandler:`) — it is the hook imageweb
  depends on;
- the error-code taxonomy (busy / missing_argument / invalid_argument
  / hardware), swapping `ccd_power_off` for detector-offline.

## Step B — `WSServer` + `Logger` into `src/Common`

Copy from `~/workspace/pfs/src/Common` (or DCU — byte-identical and
current; **not** ADC, whose `WSServer.m` is one revision behind):

- `WSServer.{h,m}` — new files; add to the FourStar target with
  per-file `-fobjc-arc` (the app is MRC).
- `Logger.{h,m}` — overwrite FourStar's copy with the version that has
  the `onAppend` hook. That is the only diverging Common file; after
  this the two Commons are identical again.

## Step C — `Service/InstrumentRouter`

Copy-adapt PFS's router: `APP_NAME @"FourStar"`, the topics table and
`_snapshotForTopic:` switch, the command chain. Keep the threading
verbatim — the dedicated `pollQueue` (never main; the
`connections`-vs-`dispatch_sync(main)` deadlock note at
`InstrumentRouter.m:34-45` applies unchanged), the change-gated
per-topic snapshot diffing, the log ring under `@synchronized`, the
`onClientCountChanged` multi-client warning.

## Step D — `AppDelegate` wiring

Mirror `~/workspace/pfs/src/PFS/AppDelegate.m:364-392`: after the
controllers exist (and their NIBs are loaded — check when
`CameraController +instance` loads `Camera.xib`; force-load first if
outlets must exist before the service reads them, as PFS had to),
create service → `WSServer initWithPort:51103` → router, wire the
client-count warning, `[router start]`, `[server start]` on the global
queue; `[router stop]` in teardown. Loopback-only listening comes free
from `WSServer` (`nw_parameters_set_local_only`) — remote access is
the cloudflared tunnel's job, per the
[security audit](../security-audit-2026-08.md).

## Quick Look — protocol extension + mosaic assembly

App-side events mirror PFS
(`~/workspace/pfs/src/PFS/CameraController.m:2145` and `:2596`):
`exposure_started` `{id, exptime, nloops, exptype}` at loop start,
`exposure_complete` after the FITS closes — with one protocol
extension, because a FourStar exposure produces four files.

### `exposure_complete`: `fits_paths` becomes the canonical key

The event's only consumer anywhere is this repo's imageweb —
`source.py` and its `status()` JSON (read by `header-panel.js`);
`diagnostic.js` renders events generically without parsing the
payload. That licenses a clean cut-over instead of a permanent
dual-key:

- **Canonical: `fits_paths`** — ordered list of local absolute paths,
  one per file the exposure produced; single-file instruments send a
  one-element list. FourStar: ordered by chip 1–4, **one** event,
  fired after the last chip's `[FITS close:]` (join `dataPaths[0]` +
  `SysPars.datafile` basename at fire time — the struct keeps only
  the basename).
- **Deprecated alias: `fits_path`** (string). The deployed PFS app
  emits it (`CameraController.m:2596`); imageweb keeps accepting it,
  normalised to a one-element list on ingest, until PFS switches to a
  one-element `fits_paths` — a one-line change riding the next PFS
  release — after which the alias is retired from the protocol doc.

FourStar therefore emits only the list, no compat duplicate:

```json
{"type": "event", "name": "exposure_complete", "data": {
  "id": 123,
  "fits_paths": ["/Volumes/DATA_BAADE/…/fsr0123c1.fits",
                 "…c2.fits", "…c3.fits", "…c4.fits"]}}
```

Rejected shapes: polymorphic `fits_path: string | list` (every
consumer type-checks forever; a poor JSON contract even with one
consumer today) and one-event-per-chip (four decodes, no combined
frame, viewer flapping).

Every `fits_path` touchpoint in this repo converts in the same PR:
`source.py` `announce()`/`_decode`/`_load` go list-first, `status()`
reports `fits_paths`, `header-panel.js` shows the last basename plus
a file count when >1, and the `control.py`/`server.py`/`source.py`
docstrings follow. Cost of skipping the dual-key: an un-upgraded
gateway would show nothing from FourStar rather than one chip —
moot, since the gateway and the app-side event ship together and
nothing else consumes the event.

Recorded in [image-viewer-plan.md](image-viewer-plan.md) § Decided;
the ws-protocol doc (lco-ansible `docs/ws-protocol.md`) needs the
matching paragraph when this ships.

### Mosaic assembly (imageweb)

`control.py` is untouched (it hands `data` through). `source.py`:

- `announce()` takes `fits_paths` when present, else `[fits_path]`.
- `_load` gains a multi-path branch: read all files, place into one
  4096×4096 `uint16` canvas, chips abutting (inter-chip gaps ignored —
  this is quick look, not astrometry). **Chip placement and
  orientation are transcribed from the Cocoa `OverView`**
  (`QltoolController` already composes exactly this 4-chip overview,
  including any per-chip flips) — copy its layout, don't re-derive it.
- All-or-nothing: any chip file unreadable → `source.error` for the
  exposure, exactly like today's single-file failure. No partial
  mosaics.
- Still one decoded frame in memory; 4096² × 16-bit is 32 MiB raw,
  far under the 10560² PFS frames the gateway already handles.

### Header combination

The frame carries a single header dict (`extra.fits.cards`, shown
verbatim in the viewer's `k` panel). Rule: **forward the last chip's
header verbatim** — the four headers agree on all exposure-level
metadata (object, exptime, filter, UT, run#) and differ only in
chip-identity cards — then append synthetic cards `NCHIPS = 4` and
`FILE1`–`FILE4` (the four basenames), which make the mosaic's
provenance explicit; the source chip's identity card stays, read as
"the chip this header came from".

Rejected alternatives: merging all four with per-chip prefixes
(`CH1_…`) quadruples panel noise for ~95 %-identical cards; a computed
merge/diff derives meaning, against the viewer's "display verbatim,
derive nothing" rule. Per-chip header inspection stays a job for the
files on disk. The Step 0 audit should check whether any
chip-*varying* card actually matters at the eyepiece (per-chip
detector temperature, say) — if so, surface just those as prefixed
synthetic cards.

Gateway config: add `"fourstar": 51103` to `KNOWN_PORTS`
([imageweb/imageweb/server.py:45](../../imageweb/imageweb/server.py))
and run `imageweb --instrument fourstar` on the instrument Mac —
`fits_path` is a local path and the WS is loopback-only, so the
gateway must be co-resident (or share the mount), same as PFS.

## Web-repo work (this repo)

Per-instrument checklist, exactly the PFS recipe:

1. `uv run xib2ir extract ~/workspace/fourstar/src/FourStar/Camera.xib
   --window 1 --app fourstar --bindings instruments/fourstar/camera/bindings.yml
   -o generated/fourstar/camera.layout.json` (committed).
2. `instruments/fourstar/camera/bindings.yml` — from the Step 0
   coverage matrix, commented against `CameraController.m` line
   numbers. Expect PFS's widget vocabulary to cover it (progress bars,
   free-text inputs, mutually-exclusive action buttons, `enabled_if`);
   any 4-chip-specific widget (per-chip status cluster) may need a
   `rows_path`-style binding — flag in the audit.
3. `instruments/fourstar/manifest.json` — `camera` (default) +
   `{"id": "quicklook", "title": "Quick Look", "embed": "/image/fourstar/"}`.
4. `index.html` — one `data-app` card (host + port 51103) and the
   `data-path` quick-look card, matching PFS's pair.
5. `deploy/cloudflared` — one ingress rule `^/fourstar/ws$` →
   `localhost:51103` on the FourStar Mac's tunnel config. No Access or
   DNS change: policies are per-telescope hostname and the baade app
   already exists in `deploy/access-policies.yml` — but **confirm
   which Mac/tunnel FourStar actually runs behind** (the v3 shakedown
   ran on clay-inst1; FourStar is historically a Baade instrument).
6. Optional: `diagnostic.js` `APP_REGISTRY` entry (PFS skipped this;
   the Diagnostic view works without it).

## Verification

All hardware-free, on one Mac:

1. Build the sim StarServers (`src/Simulators` + StarServer
   `SIM_ONLY`), launch FourStar in sim mode → four local detector
   connections on the **relocated** 51113+ ports, WS up on 51103.
2. `websocat ws://127.0.0.1:51103/` — `hello` lists the topics;
   subscribe to each, run a sim exposure loop, watch `exposure` /
   `readout` state diffs, then a **single** `exposure_complete` whose
   `fits_paths` holds four absolute paths that exist on disk (no
   `fits_path` key — FourStar never emits the deprecated alias).
3. `imageweb --instrument fourstar` + browser → each sim loop yields
   one 4096² mosaic; chip placement/orientation matches the Cocoa
   Overview window side-by-side (sim star fields make flips obvious);
   the `k` header panel shows the chip-4 cards plus `NCHIPS`/`FILEn`;
   zero decode work with the tab inactive.
4. `python3 server.py` → landing card → SPA renders the camera layout
   against live topics; Quick Look sub-tab embeds the gateway; command
   round-trips (set exptime, start/stop) reflected in the Cocoa GUI.
5. Multi-datapath: enable 2 datapaths, confirm the announced path is
   the one that exists.

## Risks / open questions

- **OverView geometry fidelity** — the mosaic must match the Cocoa
  Overview window's chip placement and flips; transcribe from
  `QltoolController`/`OverView` and verify side-by-side in sim mode.
- **Chip-varying header cards** — audit whether any (per-chip detector
  temps?) matter for quick look; if so, add them as prefixed synthetic
  cards, otherwise last-chip-header-verbatim stands.
- **Sim-port relocation** — touches `H2RG_Controller.m` and the
  StarServer launch scripts/CI ("StarServer step"); coordinate with
  the fourstar repo's v3-production-readiness plan.
- **NIB load order** — verify `Camera.xib` outlets exist at service
  start (PFS needed an explicit force-load).
- **Loop-buffer memory** — the app retains up to 2 GiB of raw loop
  buffers; the WS layer only ever reads `getPars` snapshots, never
  pixels, so no interaction — but don't be tempted to stream pixels
  over the control WS (binary frames are reserved, and imageweb reads
  the FITS from disk).
- **Which telescope/tunnel** — Baade vs the clay-inst1 shakedown Mac
  (item 5 above); affects only the cloudflared config placement.
- **Deferred windows** — Motors/Temp/LN2/Telescope join later as
  extra manifest windows + topics; no protocol change required.

## Files

App repo (`~/workspace/fourstar`): `src/Common/WSServer.{h,m}` (new),
`src/Common/Logger.{h,m}` (update), `src/FourStar/Service/InstrumentService.{h,m}`
(new), `src/FourStar/Service/InstrumentRouter.{h,m}` (new),
`src/FourStar/AppDelegate.m`, `src/FourStar/CameraController.m`
(events), `src/FourStar/H2RG_Controller.m` (sim ports),
`FourStar.xcodeproj` (target membership + per-file ARC),
`docs/ws-migration-step0-fourstar-camera.md` (new).

This repo: `instruments/fourstar/{manifest.json,camera/bindings.yml}`
(new), `generated/fourstar/camera.layout.json` (generated),
`index.html`, `imageweb/imageweb/server.py` (`KNOWN_PORTS`),
`imageweb/imageweb/source.py` (`fits_paths` canonical + mosaic
branch), `imageweb/imageweb/static/header-panel.js` (status shows
`fits_paths`), `deploy/cloudflared/…/config.yml`. Protocol doc:
lco-ansible `docs/ws-protocol.md` (the `fits_paths` paragraph).
Follow-up in `~/workspace/pfs`: `CameraController.m:2596` emits a
one-element `fits_paths`, retiring the alias.
