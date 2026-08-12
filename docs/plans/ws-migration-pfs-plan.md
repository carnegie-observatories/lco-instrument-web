# Migrate PFS (camera window) to the WebSocket external interface

Per-instrument plan for PFS, layered on top of the canonical
[ws-migration-plan.md](ws-migration-plan.md). Scope is intentionally
narrow — **the camera window only**. Object catalog, calibration,
telemetry graph, qltool, and image transfer are explicitly out of scope
and not addressed here; they each warrant their own per-window plan
when the time comes. The framework — `WSServer`, `InstrumentService`,
`InstrumentRouter` — is unchanged from ADC/DCU; what changes is how
PFS slots into it.

## Context

PFS lives at [/Users/william/workspace/pfs/src/](/Users/william/workspace/pfs/src/).
[main.h:17](/Users/william/workspace/pfs/src/PFS/main.h) declares
`PROJECT_ID = 16`, but the comment notes this is "same as pfsgui/X11 —
unused": PFS has no `tcpip_handler:` and no `TCPIP_Server` — the inbound
TCP surface that ADC and DCU rely on does not exist on PFS.
[Tcpip.{h,m}](/Users/william/workspace/pfs/src/Common/Tcpip.h) is
present but only used as a *client* by [PLCIO](/Users/william/workspace/pfs/src/PFS/PLCIO.h)
(to the Siemens S7 PLC at `pfsplc:502`) and [MagIO](/Users/william/workspace/pfs/src/Common/MagIO.h)
(to the TCS at `mag2tcs:5811`). The PFSLOG daemon under [src/PFSLOG/](/Users/william/workspace/pfs/src/PFSLOG/)
is a separate C process with its own TCP server; out of scope.

PFS's AppDelegate owns three `NSWindowController`s — the camera window
is one of them:

- `ccdController` — [CameraController](/Users/william/workspace/pfs/src/PFS/CameraController.h),
  the primary operator window: exposure loop, readout, image display,
  embedded sub-panels for STA / dewar / PFS subsystem / PLC. **This is
  the window in scope.**
- `objController` — [ObjectController](/Users/william/workspace/pfs/src/PFS/ObjectController.h) —
  out of scope.
- `calController` — [CalibrationController](/Users/william/workspace/pfs/src/PFS/CalibrationController.h) —
  out of scope.

CameraController composes two hardware sub-controllers that *are* in
scope because they back the camera-window operator surface:

- [ArchonController](/Users/william/workspace/pfs/src/PFS/ArchonController.h) —
  CCD readout card driver. Backs exposure start/stop/pause and readout
  parameters (binning, window, speed, mode).
- [PFS_Controller](/Users/william/workspace/pfs/src/PFS/PFS_Controller.h) —
  master PLC façade (cryostat, pumps, shutters, motors, gauges,
  Hartmann door, slit/cell selectors, mirror, diffuser). Owns `PLCIO`.

[QltoolController](/Users/william/workspace/pfs/src/PFS/QltoolController.h)
and [GraphController](/Users/william/workspace/pfs/src/PFS/GraphController.h)
are out of scope.

PFS produces FITS via Archon. **Image transfer is out of scope for this
PR.** The WS surface can announce that a FITS file has been written (a
lightweight `exposure_complete` event carrying the on-disk path), but
no bytes flow over the network from this PR. JS9, ImageHTTPServer, the
`+3` port, and the `image_ready` event are all deferred to a future
image-transfer PR.

### What's different from ADC/DCU

| Dimension | ADC / DCU | PFS (camera-only) |
|---|---|---|
| Inbound TCP server | Present (3 cmds each) | **Absent** |
| Operator controllers in scope | 1 each | **CameraController + ArchonController + PFS_Controller** |
| FITS output | None | Yes (announced via event; bytes not transferred this PR) |
| KVO / notifications | Some (DCU has FFsensor/FFscreen) | **None observed** |
| Threading | Light dispatch_sync | **Heavy `[NSThread isMainThread]` asserts everywhere** |
| External TCP clients | TCS only | **PLC (Modbus TCP) + TCS** |
| Codebase size in scope | ~3k / ~5k LOC | **~6–8k LOC in scope** (CameraController + ArchonController + PFS_Controller + AppDelegate slice) |

These differences ripple through Steps 0–D below; the framework
itself stays unchanged.

## Port assignment

Following the canonical formula (`50001 + PROJECT_ID*100`, WS at `+2`):

| Instrument | PROJECT_ID | Legacy TCP | WS control | Image HTTP |
|------------|------------|------------|------------|------------|
| PFS        | 16         | 51601 (n/a) | **51603**  | 51604 (future) |

`51601` is reserved by `PROJECT_ID*100` arithmetic but PFS has no legacy
TCP server, so the slot is unused — documented here so it's not double-
allocated to another instrument later. `51604` is reserved for the
eventual image-transfer PR and should not be claimed by anything else.

Add the PFS row to ws-migration-plan.md's port table as part of this PR.

## Step 0 — Audit the camera window

Mirrors the per-instrument audit in [ws-migration-step0-adc.md](ws-migration-step0-adc.md)
and [ws-migration-step0-dcu.md](ws-migration-step0-dcu.md). PFS's audit
is bigger than ADC/DCU's so it lives in a separate file at
`pfs/docs/ws-migration-step0-pfs-camera.md` (not inline in the header
like ADC/DCU).

**In scope:**

- [CameraController.xib](/Users/william/workspace/pfs/src/PFS/CameraController.xib)
  itself.
- Its embedded sub-panels: `panel_sta`, `panel_dewar`, `panel_pfs`,
  `panel_plc`, `panel_window`. These are part of the camera surface
  even though they're separate panels — the operator uses them during
  observing.

**Out of scope for the audit (deferred to future PRs):**

- preferences panels;
- the STA-internal diagnostic readouts on `panel_sta` that have no
  operator action (display-only engineering values);
- any motor-hardhat dialog (engineering surfaces, mirroring the ADC/DCU
  exclusion of `panel_calibrate`).

The header lists ~30+ outlets on CameraController alone:
`popup_exptype`, `popup_expmode`, `popup_readmode`, `popup_binx`,
`popup_biny`, `popup_speed`, `drop_slit`, `drop_cell`, `drop_hart`,
`popup_lamp`, `popup_diffuser`, `popup_mirror`, `popup_gshutter`,
`popup_pmt`, `edit_focus`, `edit_x1/x2/y1/y2`, `popup_ion`, etc. Each
becomes one row in the coverage matrix.

For each row: identify the `-IBAction:` method, the underlying call
(which sub-controller, which PLC coil/Modbus register or Archon API),
and the state value it reads to indicate position. **The "Today's TCP
command" column is empty everywhere** — there is no legacy server.
Annotate every row as a new surface.

State values to surface as topics:

- cryostat temps: `edit_temp_a/b/c`
- heater: `edit_heat_a`, `edit_setp_a`
- vacuum: `edit_vac`
- ion gauge: `edit_ion`, `popup_ion`
- exposure progress: `prog_exposure`, `prog_readout`, `edit_runtime`,
  `edit_estimate`, `edit_counts`, `edit_loop`
- disk-space level indicators: `level_disk1`, `level_disk2`
- shutter view: `view_shutter`

## New widget/binding types vs. ADC/DCU

The camera window introduces five widget/state shapes the ADC/DCU
pipeline never had to handle. Verified against
[CameraController.xib](/Users/william/workspace/pfs/src/PFS/CameraController.xib)
(window id `F0z-JX-Cv5`, the "PFS Camera" window). Sub-panels
(Subraster Window, Dewar Status, Archon Hardhat, PFS Status, PLC
Hardhat) are out of scope for this PR — the widgets unique to those
panels (NSStepper, threshold-coloured text fields) are not addressed
here.

Two of these are *wire-protocol* decisions and need to be made before
Step A; they're reflected in the topic-shape comments above. The rest
are SPA-repo concerns (IR schema, renderer, bindings.yml) better
captured in a follow-up note inside `lco-instrument-web`, listed below
for completeness.

### A. Free-text inputs (`edit_object`, `edit_comment`, `edit_file`)

ADC/DCU only had numeric editable text fields. PFS adds free-form
strings.

- **IR**: same `textfield/input` kind, but `xib2ir` needs to infer
  string vs. number from the `textFieldCell`'s formatter (NSNumberFormatter
  → number, none → string). Today the converter assumes numeric.
- **Renderer**: pick `<input type="text">` instead of `<input type="number">`
  based on the new hint; existing focus-guard works as-is.
- **bindings.yml**: nothing new — the `$value` sigil already passes the
  raw control value.
- **Server**: `setObjectName:` / `setComment:` / `setFilenameStem:`
  validate at the InstrumentService boundary. Filename stem is the real
  footgun (`/`, `..`, control chars). Reject with `InvalidArgument`
  rather than letting the path through to disk.

### B. Determinate progress bars (`prog_exposure`, `prog_readout`)

ADC/DCU progress indicators were all indeterminate spinners driven by a
binary `moving` flag — a CSS class swap. The camera window's
`<progressIndicator style="bar" maxValue="100">` are actual percentage
bars.

- **IR**: new `progress/bar` subkind alongside the existing
  `progress/spinner`. Carries `min`/`max` from the XIB.
- **Renderer**: new DOM — `<div class="wf-progress wf-bar"><div class="wf-bar-fill"></div></div>`
  with the fill's width set inline from state.
- **bindings.yml**: a `read.value_kind` (`percent` | `fraction` |
  `absolute`) tells the renderer how to map the topic value to the
  bar's 0–100% range. Without this hint, runtime values "0–1" vs.
  "0–100" vs. "0–exptime" are ambiguous.
- **Server** *(wire-protocol decision)*: pre-compute the displayed
  value (`exposure.progress` ∈ [0, 100]) inside InstrumentService rather
  than shipping `runtime` and `exptime` separately. One number per
  topic, server owns the formula, easier coalesce. `exposure.runtime`
  and `exptime` are still available in the same snapshot for non-graphical
  clients.

### C. Level indicators (`level_disk1`, `level_disk2`)

NSLevelIndicator with `levelIndicatorStyle="continuousCapacity"` and
bake-in thresholds (`maxValue="100" warningValue="80" criticalValue="95"`).
A segmented capacity bar with green / yellow / red zones whose breakpoints
are fixed in the XIB.

- **IR**: new `levelindicator` kind. Must carry `min`/`max`/`warning`/`critical`
  from the XIB so the renderer reproduces Cocoa's colour transitions.
  Thresholds are layout-time data, not runtime, so they belong in the
  IR, not bindings.yml.
- **Renderer**: new widget — a single bar element styled with three CSS
  regions, plus a fill whose colour comes from the fill's percentage
  vs. the threshold values.
- **bindings.yml**: just `read.path` to a numeric. No `class_map` —
  colouring is intrinsic from the IR thresholds.
- **Server**: `disk` topic with `disk1_pct`, `disk2_pct` as 0–100
  numbers. No thresholds in the topic — they're a render-time concern.

### D. State-driven shutter image view (`view_shutter`)

ADC/DCU's image views were single-state CSS-class swaps. PFS's
`view_shutter` cycles among several distinct images (the Cocoa
controller calls `[view_shutter setImage:]` with different bundled
icons). Since the SPA doesn't ship raster assets, this becomes a
multi-state CSS rule.

- **IR**: existing `imageview/state-icon` subkind works unchanged.
- **Renderer**: existing `class_map` works.
- **bindings.yml**: identical pattern to DCU lamps —
  ```yaml
  view_shutter:
    read:
      topic: shutter
      path: state
      class_map: { open: is-open, closed: is-closed,
                   moving: is-moving, err: is-err }
  ```
- **CSS**: new per-state shapes (open = empty circle, closed = filled
  circle, moving = ring with spin, err = warning triangle). Pure CSS,
  no assets — same rule established by the ADC/DCU ban on raster ports.
- **Server** *(wire-protocol decision)*: `shutter` topic ships
  `state: "open" | "closed" | "moving" | "err"` enum, not the
  `open:bool` originally sketched. Topic shape updated in Step A.

### E. Mutually-exclusive action buttons (`but_start`, `but_snap`, `but_pause`, `but_stop`)

ADC/DCU's buttons were independent. PFS's exposure buttons enable/disable
based on `exposure.running`: start/snap disabled while running,
pause/stop disabled while idle. This is the first time the SPA needs to
disable a control from topic state.

- **IR**: no change.
- **Renderer**: new `enabled_if` binding clause, structurally identical
  to the existing `hidden_if`. Toggles the `disabled` attribute on
  `<button>`.
- **bindings.yml**:
  ```yaml
  but_start: { write: { cmd: start_exposure },
               enabled_if: { topic: exposure, path: running, equals: false } }
  but_pause: { write: { cmd: pause_exposure },
               enabled_if: { topic: exposure, path: running, equals: true  } }
  but_stop:  { write: { cmd: stop_exposure  },
               enabled_if: { topic: exposure, path: running, equals: true  } }
  but_snap:  { write: { cmd: snap_exposure  },
               enabled_if: { topic: exposure, path: running, equals: false } }
  ```
- **Server**: nothing new — `exposure.running:bool` is already in the
  topic shape.

### Forward-compatibility

All five additions slot under the existing `protocol_version: 1`
envelope. Topic-shape changes (derived `exposure.progress`, `shutter.state`
enum) are additive — clients that ignore new fields keep working. The
IR/renderer/CSS additions live in the SPA repo and don't touch the
wire protocol.

## Step A — `InstrumentService` for PFS

One `InstrumentService` class. Methods are organized into groups
(`#pragma mark`) by sub-controller because the camera surface alone is
bigger than ADC's or DCU's full surface.

```objc
// /Users/william/workspace/pfs/src/PFS/Service/InstrumentService.{h,m}

@interface InstrumentService : NSObject

- (instancetype)initWithAppDelegate:(AppDelegate *)app;

// version (also surfaced in `hello`)
- (NSDictionary *)version;

#pragma mark - Camera (Archon-backed)

// Snapshots
- (NSDictionary *)exposure;       // {running, paused, id, exptime, runtime,
                                  //  estimate, progress, loop, nloops,
                                  //  counts, type, mode}
                                  //   progress: server-derived 0–100, drives
                                  //   the prog_exposure bar; see § New
                                  //   widget/binding types.
- (NSDictionary *)readout;        // {binx, biny, speed, readmode, progress,
                                  //  window:{x1,x2,y1,y2}}
                                  //   progress: server-derived 0–100, drives
                                  //   the prog_readout bar.
- (NSDictionary *)shutter;        // {state: "open"|"closed"|"moving"|"err",
                                  //  last_change_ts}
                                  //   enum, not bool — view_shutter cycles
                                  //   among multiple icons; see § New
                                  //   widget/binding types.
- (NSDictionary *)disk;           // {disk1_pct, disk2_pct, target_dir}

// Writes
- (NSDictionary *)setExptime:(NSNumber *)seconds error:(NSError **)err;
- (NSDictionary *)setExposureType:(NSString *)type error:(NSError **)err;
- (NSDictionary *)setExposureMode:(NSString *)mode error:(NSError **)err;
- (NSDictionary *)setNLoops:(NSNumber *)n error:(NSError **)err;
- (NSDictionary *)setBinning:(NSNumber *)bx by:(NSNumber *)by error:(NSError **)err;
- (NSDictionary *)setReadMode:(NSString *)mode error:(NSError **)err;
- (NSDictionary *)setReadoutSpeed:(NSString *)speed error:(NSError **)err;
- (NSDictionary *)setReadoutWindowX1:(NSNumber *)x1 x2:(NSNumber *)x2
                                  y1:(NSNumber *)y1 y2:(NSNumber *)y2
                               error:(NSError **)err;
- (NSDictionary *)setObjectName:(NSString *)name error:(NSError **)err;
- (NSDictionary *)setComment:(NSString *)comment error:(NSError **)err;
- (NSDictionary *)setFilenameStem:(NSString *)stem error:(NSError **)err;
- (NSDictionary *)startExposure:(NSError **)err;
- (NSDictionary *)snapExposure:(NSError **)err;       // single test exposure
- (NSDictionary *)pauseExposure:(NSError **)err;
- (NSDictionary *)stopExposure:(NSError **)err;

#pragma mark - PFS_Controller (PLC-backed)

// Snapshots
- (NSDictionary *)dewar;          // {temps:{a,b,c}, heater:{level, setp},
                                  //  vac:float, ion:{state, value, range}}
- (NSDictionary *)mechanics;      // {slit, cell, hart, lamp, diffuser,
                                  //  mirror, gshutter, pmt} —
                                  //  each {position, moving}
- (NSDictionary *)focus;          // {value:float, locked:bool}
- (NSDictionary *)plc;            // {pumps:{glycol, ion}, tape,
                                  //  heater_enabled}

// Writes
- (NSDictionary *)moveMechanism:(NSString *)name to:(NSString *)position
                          error:(NSError **)err;
   // name ∈ {slit, cell, hart, lamp, diffuser, mirror, gshutter, pmt}
- (NSDictionary *)setFocus:(NSNumber *)um error:(NSError **)err;
- (NSDictionary *)setHeaterSetpoint:(NSNumber *)celsius error:(NSError **)err;
- (NSDictionary *)setHeaterEnabled:(BOOL)enabled error:(NSError **)err;
- (NSDictionary *)setIonPump:(BOOL)on error:(NSError **)err;
- (NSDictionary *)setGlycolPump:(BOOL)on error:(NSError **)err;

#pragma mark - Telescope (TCS readback, read-only)

- (NSDictionary *)telescope;      // {ra, dec, elevation, rotator,
                                  //  focus, weather}
@end
```

**PFS-specific service notes:**

1. **Init takes the AppDelegate, not a single controller.** ADC and
   DCU could pass a single controller to `initWithController:` because
   the surface lived on one class. PFS spans three NSWindowControllers
   plus sub-controllers — passing the AppDelegate (which already owns
   all of them) is cleaner than threading multiple controller args
   through the initializer. The service keeps `__weak` refs to whichever
   sub-controllers it needs (camera-window scope: CameraController,
   ArchonController, PFS_Controller).

2. **Main-thread dispatch must be explicit.** PFS sub-controllers are
   peppered with `assert([NSThread isMainThread])`. Every WS-routed
   call into a controller method must `dispatch_sync(dispatch_get_main_queue(), …)`
   from the WS queue. Same rule as ADC/DCU, but the assert density
   makes any miss crash-loud rather than silent corruption — fine, just
   plan to catch them during PR review/CI.

3. **No KVO infrastructure exists.** ADC/DCU had `FFsensor`/`FFscreen`
   posting KVO updates; PFS has none. The router's snapshot loop will
   be a 1 Hz polling timer reading the service's topic methods. Sized
   well for the camera surface (cryostat / mechanics state changes are
   slow; exposure progress is fast enough that 1 Hz is acceptable
   feedback). **Do not retrofit KVO speculatively in this PR.**

4. **Error domain stays `InstrumentServiceErrorDomain`** with codes
   `Busy`, `MissingArgument`, `InvalidArgument`, `Hardware`. Add
   `PLCUnreachable` for Modbus timeouts (distinct from generic
   `Hardware` so clients can decide whether to retry vs. surface to
   operator).

## Step B — `WSServer` to src/Common

Identical to the canonical plan. Copy `WSServer.{h,m}` from
[adc/src/Common/](/Users/william/workspace/adc/src/Common/) (post-cleanup)
to [pfs/src/Common/](/Users/william/workspace/pfs/src/Common/). No
PFS-specific adjustments expected. Add to the Xcode project
(PBXFileReference + PBXBuildFile + PBXGroup sections).

If PFS's build settings are stricter than ADC's, expect to need the
same `extern` fix DCU needed in its build-settings step. Watch for
`-Wimplicit-retain-self` warnings on the new code; the ADC/DCU cleanup
silenced these.

## Step C — `InstrumentRouter` per app

Same structure as ADC/DCU's [InstrumentRouter](/Users/william/workspace/adc/src/ADC/Service/InstrumentRouter.h),
adapted for PFS:

- **On connect** — send `hello` with `app:"pfs"`, version from
  [main.h](/Users/william/workspace/pfs/src/PFS/main.h), and the topic
  list scoped to the camera-window surface: `exposure`, `readout`,
  `shutter`, `disk`, `dewar`, `mechanics`, `focus`, `plc`, `telescope`.
- **On `subscribe`** — push snapshot of each subscribed topic.
- **On `cmd`** — `dispatch_sync(MAIN_QUEUE, …)` into the
  InstrumentService.
- **State publish** — 1 Hz polling timer. Each tick: read each topic
  snapshot, compare to last-sent value, push `state` frame if changed.
  No coalescing logic yet; the timer's natural 1 Hz cadence is the
  coalesce.
- **Exposure lifecycle events** — hook ArchonController's exposure
  start/end/pause/stop transitions to emit `event` frames:
  `exposure_started`, `exposure_paused`, `exposure_stopped`,
  `exposure_complete`. `exposure_complete` carries the on-disk
  `fits_path` (string) for now — no `image_id` / `fits_url` / `shape` /
  `dtype` fields. A future image-transfer PR will extend this event
  with the URL fields; clients written against this PR should still
  parse forward-compatibly (ignore unknown fields).
- **Multi-client alert** — same rule as ADC/DCU. Route to `main_logger`
  (a warning per client-count change) and add a small NSWindow notice
  in CameraController (the always-open window).

**Explicitly not in this PR:**
- `image_ready` events (no image transfer).
- ImageHTTPServer / FITS bytes.
- High-frequency state push (no graph window).

## Step D — `AppDelegate` wiring

In [AppDelegate.m](/Users/william/workspace/pfs/src/PFS/AppDelegate.m)'s
`applicationDidFinishLaunching:`, after the three NSWindowControllers
are constructed and `showWindow:`'d:

```objc
_instrumentService = [[InstrumentService alloc] initWithAppDelegate:self];

const uint16_t wsPort = 50001 + PROJECT_ID*100 + 2;   // 51603

_wsServer = [[WSServer alloc] initWithPort:wsPort];
_wsRouter = [[InstrumentRouter alloc] initWithServer:_wsServer
                                              service:_instrumentService];
_wsRouter.onClientCountChanged = ^(NSUInteger count, NSArray *ips) {
    if (count > 1) [self showMultiClientAlert:ips];
    else           [self dismissMultiClientAlert];
};
[_wsServer start];
```

No legacy TCP server to coexist with — single switch on a hidden
default (`defaults write … WSEnabled -bool YES`, default YES) to
disable the WS server in case of trouble. No `chk_tcpip` checkbox
lineage to honor because there's no checkbox in the first place.

## State publishing — concrete plan

Two categories of state on the camera surface, both 1 Hz poll for this
PR. KVO and event-driven push are deferred (and have no live consumer
that would justify them within this PR's scope):

| Category | Examples | Strategy |
|---|---|---|
| Slow / discrete | mechanism positions, mode popups, focus, heater setpoint | 1 Hz poll, diff-and-push |
| Fast / numeric | temperatures, vacuum, exposure runtime | 1 Hz poll, diff-and-push |
| Lifecycle events | exposure start / pause / stop / complete | event-driven from Archon callbacks |

The 1 Hz poll is deliberately stupid — the framework needs to land
first, optimization comes when there's a real client driving it.

## PFS-specific risks

1. **PLC reachability.** `pfsplc:502` is a real network endpoint. If
   Modbus times out, `setIonPump:` etc. block until the underlying
   `PLCIO` call returns or errors. Wrap PLC writes with a finite
   timeout in InstrumentService; surface `PLCUnreachable` to the
   client. Do not let a PLC outage hang the WS handler thread; it will
   starve other commands.

2. **Archon exposure-state synchronization.** Cocoa's button handlers
   on CameraController are written assuming the user can only press
   one button at a time. A WS client can fire `startExposure` while
   the operator is mid-click. The service must serialize on the main
   queue, and ArchonController must reject `startExposure` cleanly if
   `loop_doing` is already true (returning `Busy` to the WS, not
   asserting). Audit ArchonController's `loop_doing` transitions for
   races during the Step 0 audit.

3. **The two PLC actors (becoming three).** PFS_Controller drives the
   PLC for operator moves; PFSLOG (the separate C daemon) also queries
   the PLC for logging. Adding a WS write surface introduces a third
   actor. The PLC itself serializes Modbus requests, so this isn't a
   correctness issue, but it can cause subtle latency spikes when three
   callers collide. Note in the PR description; revisit if observed.

4. **No existing test target for the camera path.** [PFSTests/](/Users/william/workspace/pfs/src/PFSTests/)
   exists but coverage of the controller methods is thin. The Step A
   "InstrumentService unit tests" verification item is a bigger lift
   here than on ADC/DCU. Budget for ~10 mock-PLC + mock-Archon tests in
   this PR.

5. **Simulator mode (`SIM_ONLY`).** Compiled in; affects PLC port
   65034 and localhost TCS. Confirm the WS path works with `SIM_ONLY`
   enabled so CI can run without observatory hardware. The simulator
   already exists; we just need to confirm it covers the Archon path
   too.

## Files

**New (pfs):**
- `/Users/william/workspace/pfs/src/Common/WSServer.{h,m}`
- `/Users/william/workspace/pfs/src/PFS/Service/InstrumentService.{h,m}`
- `/Users/william/workspace/pfs/src/PFS/Service/InstrumentRouter.{h,m}`
- `/Users/william/workspace/pfs/docs/ws-migration-step0-pfs-camera.md`

**Modified (pfs):**
- `/Users/william/workspace/pfs/src/PFS/AppDelegate.{h,m}` — service +
  router + WS server; multi-client alert plumbing.
- `/Users/william/workspace/pfs/src/PFS.xcodeproj/project.pbxproj` —
  register new files; confirm link to `Network.framework`.
- `/Users/william/workspace/pfs/src/PFS/ArchonController.m` — hook
  exposure lifecycle to emit `exposure_*` events via the
  InstrumentService.

**Repo-level (lco-ansible):**
- This plan file: [docs/plans/ws-migration-pfs-plan.md](docs/plans/ws-migration-pfs-plan.md).
- Append PFS row to the port table in [docs/plans/ws-migration-plan.md](docs/plans/ws-migration-plan.md).
- Append PFS to the "Applicability across the LCO Cocoa instrument
  suite" table in the canonical plan (mark as "needs server creation —
  no existing handler" akin to the M2FS row).

**SPA repo ([lco-instrument-web](../..)):**
- `instruments/pfs/camera/bindings.yml`
- `generated/pfs-camera.layout.json`
- Hello-driven app routing already selects by `app`. The SPA's
  multi-window selector is a separate question deferred to whichever
  future PR introduces the second PFS window.

## Verification

Run the canonical plan's verification checklist (§ ws-migration-plan.md
Verification) against PFS, adapted:

1. CameraController main-window coverage signed off (step0 audit
   committed at `pfs/docs/ws-migration-step0-pfs-camera.md`).
2. **`xib2ir` backwards-compatibility** — the new widget types (§ New
   widget/binding types) require schema additions to the IR
   (`progress/bar` subkind, `levelindicator` kind, `value_kind` hint on
   text inputs). After landing those, re-run `xib2ir lint` against
   ADC's and DCU's committed layout JSONs and confirm **zero diff** for
   both:
   ```sh
   cd lco-instrument-web/tools/xib2ir
   python3 -m xib2ir lint /Users/william/workspace/adc/src/ADC/Base.lproj/MainMenu.xib \
       --window TNc-LT-7qW --app adc \
       --bindings ../../instruments/adc/bindings.yml \
       --expected ../../generated/adc.layout.json
   python3 -m xib2ir lint /Users/william/workspace/dcu/src/DCU/Base.lproj/MainMenu.xib \
       --window 371 --app dcu \
       --bindings ../../instruments/dcu/bindings.yml \
       --expected ../../generated/dcu.layout.json
   ```
   Both must exit 0. ADC and DCU XIBs don't contain progress-bar /
   level-indicator widgets, so schema additions should be opt-in (new
   fields appear only when the corresponding XIB element is present);
   any change that produces a diff in either committed JSON is a
   converter regression and must be fixed before merging. This is the
   same lint subcommand committed in `lco-instrument-web@d693a79` —
   it's already CI-ready, just needs to be added to PFS's PR checklist.
3. InstrumentService unit tests against a mock AppDelegate (mock PLC +
   mock Archon). Includes at least one PLC-timeout test
   (`PLCUnreachable`).
4. **No legacy parity smoke** — there is no legacy TCP server to
   parity against. Replaced by: snapshot the pre-PR camera-window
   behavior manually (operator runs through ~5 typical exposures with
   the GUI only, observes no regressions), then re-run after the PR.
5. WS smoke (Python `lco-instrument-ws.PFSClient`): connect to 51603,
   subscribe to `exposure`/`readout`/`dewar`/`mechanics`, assert
   snapshot shapes, send each write command.
6. **No image-port verification** — image transfer is out of scope.
   `exposure_complete` event carrying `fits_path` is the only check;
   confirm the path matches what Archon wrote and the file exists on
   disk.
7. Browser end-to-end via [lco-instrument-web](../..)
   Diagnostic view first, then the position-faithful Camera window
   once the layout JSON is generated. Verify every Step 0 row.
8. Multi-client alert.
9. Mixed-traffic stress: snap-exposure loop driven from WS while
   operator also clicks the GUI; verify no main-thread stalls and no
   doubled exposures.
10. Idle leak: 12h soak.
11. Single full engineering shift driven from the WS client at the
    observatory, with WS disable hidden-default available as fallback.

## Open questions

1. **`exposure_complete.fits_path` format.** Absolute path? Path
   relative to a documented data root? Forward-compatible with a
   future image-transfer PR that adds `image_id` / `fits_url`?
   Recommend: absolute path for this PR; the image-transfer PR adds
   `image_id` as a new sibling field without removing `fits_path`,
   so clients keep working.

2. **InstrumentService init signature.** Plan above passes
   `initWithAppDelegate:`. Alternative is
   `initWithCameraController:archon:pfs:` (explicit). Trade-off:
   AppDelegate-injection is concise but couples the service to
   AppDelegate's structure; explicit is more testable but threads
   three params. Recommend AppDelegate for this PR, refactor only if
   tests get hard to write.

3. **Headless build target.** Per ws-migration-plan.md's long-term
   stages, the end state is `--headless` Cocoa. PFS's multi-window /
   multi-controller architecture makes this more disruptive than
   ADC/DCU; this PR is foundational but every direct GUI-bypass call
   in CameraController is a future blocker. Note in PR description;
   do not address in scope.

4. **Embedded sub-panels as separate SPA windows or one camera
   layout.** `panel_sta`/`panel_dewar`/`panel_pfs`/`panel_plc` are
   NSPanels but operationally part of the camera surface. Treat as
   one logical "camera" layout in the SPA (single bindings.yml +
   single generated JSON), or one per sub-panel? Recommend single
   camera layout with the sub-panels embedded; the Cocoa-side panel
   geometry can be flattened into one renderer document by xib2ir.
