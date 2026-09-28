# Migrate MIKE (MIKE window) to the WebSocket external interface

Status: implemented 2026-09-27 on SBS (carnegie-observatories/mike#17, this
repo's `feature/mike`); Clay not started. Written against `~/workspace/mike`
`main` (3a85e5e); line numbers below are that commit's.

## As implemented

Where the build differs from the plan below, and why:

- **Deployment target 10.15.** The instrument Macs, simulator and production,
  run macOS 26.6.2 or later, which settles the first open question.
- **No SIM build configuration.** SBS runs the CI (Debug) build with the
  Configuration window's offline settings instead, as ADC does: the simulator
  port offset applies whenever a CCD host is `localhost` (the case in which the
  app starts `mikeserver`), and `dbe_sim_gui` 0 runs it without X11. The
  committed `mikeserver` was x86_64 only and is now universal; sbs-inst1 has no
  Rosetta.
- **Autostart.** Preferences → General gains Autostart (`dbe_autostart`), as
  ADC and PFS have, so the server comes up without the Configuration window.
- **Enabling rules are the window's.** Topics carry each control's enabled
  state as the Cocoa window sets it, and every command re-checks it; bindings
  read those flags rather than re-deriving the rules.
- **Questions and errors.** The window's questions (abort; object ≥ 60 s with
  the diffuser not out) come back as a `confirm` error that the page asks the
  operator and resends with `confirm: true`. ccdserver refusals come back as
  `hardware` errors, logged rather than shown as dialogs, and the router spaces
  writes 150 ms apart and caps its queue at 10 — a client looping on a failing
  command once filled a screen with dialogs.
- **Quick look.** `/image/mike/blue/` and `/red/` as planned; the viewer now
  loads its packages from `/pkg/` at the root, since the page sits three levels
  deep. The two tabs come from the manifest.
- **Commands.** `set_run`, `set_subraster_mode` (`subraster` only when the
  Subrasters sheet has some), `set_autofocus`; `both` where the window has a
  common column, plus binning and speed as the legacy server allows.
- **Deployment.** SBS (`deployments/sbs.yml`) instead of Clay; lco-ansible's
  instruments role gained per-app `defaults`/`shared_defaults` for the
  simulator settings.

Details per control: `mike/docs/ws-migration-step0-mike-window.md`.

Three pieces, and they ship separately: the controls SPA, the quick look, and
the slit viewer. **The slit viewer is ready now and depends on nothing in this
plan** — MIKE's camera has been on a Raspberry Pi behind gcam since 2024 and is
already in the lco-ansible inventory (`inventory_lco.yaml:78`). It is blocked
only on one missing number, described in § Slit viewer. The other two are gated
on the app-side port, which is gated in turn on a deployment-target bump.

## Picking this up cold

State on 2026-09-27, and the three things that will otherwise bite whoever
starts:

- **Do not update the astro-ph checkout.** Everything the quick look does rests
  on `chz1.stream`, a Python module that exists only on the unmerged
  `chz1-stream` branch of `~/workspace/astro-ph-labs/astro-ph`
  ([PR #1](https://github.com/astro-ph-labs/astro-ph/pull/1), still a draft).
  Upstream `main` has since moved 119 commits: it **deleted the Python `chz1`
  package** (`packages/chz1/pyproject.toml` and `python/__init__.py` are gone,
  the encoder is Rust in `crates/chz1`) and **changed the CHZ1 wire format
  incompatibly** (commit `6e6b190`, the filter is now a median). Both
  `imageweb/pyproject.toml` and gcamweb pin that package by path, and the
  deployed SBS stack is only self-consistent because it is on the old branch.
  Rebasing or pulling astro-ph is a design decision, not a chore — see
  `pre-production-todo.md` — and the quick-look steps here assume the current
  branch.
- **gcamweb comes from a worktree.** zwo
  [PR #37](https://github.com/carnegie-observatories/zwo/pull/37) (whole frames,
  per-client region and stride) is open, so any deploy needs
  `-e gateway_gcamweb_src=~/workspace/zwo-pc/src/web` until it merges.
- **MIKE's checkout is not on `main`.** It sits on `fix/lamp-timeout-MIKE-56`
  with an uncommitted `src/MIKE/main.h` (`LAMP_TIMEOUT` 1800 → 60, someone's
  test tweak). Branch from `main` (`3a85e5e`) and do not commit that line.
  The unmerged `fix/offtime-sanity-check` branch should land first: it refuses
  to start when the TCS clock offset is implausible, and a web page that
  published MIKE's clock offset would otherwise repeat the June 2026 wrong-
  timestamp incident.

Three facts still need a person, and only the first two block anything:
which macOS `clay-inst1` runs (gates every app-side step), how Clay's
deployment file tracks which slit viewer is mounted (§ Slit viewer), and which
Mac would host the Clay gateway (§ Deployment).

Order of work: the slit viewer can ship on its own today; the deployment target
gates the controls SPA and the quick look; everything else follows the steps
below in order.

## Context

MIKE is a Cocoa/ObjC app of the PFS family: same author, same MRC conventions
(`CLANG_ENABLE_OBJC_ARC = NO`, `src/MIKE.xcodeproj/project.pbxproj:364,419`),
`src/Common` byte-identical to PFS's for every shared file except `Logger`
(diff below), singleton-ish controllers with a clean read surface, and the
exposure loop on a worker thread. The standard port — `src/Common/WSServer` +
`Service/InstrumentService` + `Service/InstrumentRouter` + `AppDelegate`
wiring — applies, and `ws-migration-plan.md` § Applicability covers it.

Four things make MIKE different from every instrument ported so far, and all
four change the shape of the work:

- **It is a double spectrograph, and the two arms are two independent
  instruments.** `BLU`/`RED`/`CCD_COLORS 2` (`src/MIKE/main.h:36-38`), one
  `CameraController` per arm (`src/MIKE/GUI_Controller.m:53,121`, `camCon[2]`),
  each holding its own `SysPars` (`main.h:107-147`), its own CCD server on its
  own host (`ccd04`/`ccd05`, `main.h:59-60`), its own FITS prefix `b`/`r`
  (`src/MIKE/CCDIO.m:35`), its own run counter
  (`src/MIKE/CameraController.m:43,179`) and its own readout loop. Pressing
  Start with the camera set to BOTH does not start one exposure — it starts two
  (`GUI_Controller.m:1552-1555`, which recurses into `but_startB` then
  `but_startR`, each launching `run_loop` in the background at
  `GUI_Controller.m:1569`). Arms therefore appear as arrays inside each topic,
  as two `exposure_complete` events, and as two quick-look streams.
- **The formula's WS port is already a MIKE port.** `50803` is `BLU_D_PORT`,
  the blue arm's data-acquisition server port (`src/Simulator/mike.h:101-108`).
  Full treatment in § Port assignment; it is the one thing in this plan that
  cannot be waved through.
- **`MACOSX_DEPLOYMENT_TARGET = 10.13`** (`project.pbxproj:403,456`). `WSServer`
  is `Network.framework` WebSocket, which needs 10.15. Nothing on the app side
  starts until this is raised.
- **A live legacy TCP command server whose replies go through one shared
  property.** `GUI_Controller.tcpResponse` (`src/MIKE/GUI_Controller.h:88`) is
  written at the end of each `action_*` when the caller was the socket rather
  than the GUI (`GUI_Controller.m:1345,1396,1431,1465`) and read straight back
  by the TCP handler (`AppDelegate.m:527,578-592,621-659`). Two clients writing
  at once corrupt each other's replies. The resolution is in § Step A and it is
  cheap, because MIKE already has the right seam.

### What's different from PFS

| | PFS | MIKE |
|---|---|---|
| Detector | 1 CCD via Archon | 2 independent CCDs (blue, red), `MAX_NCHIPS 1` each (`main.h:75`) |
| FITS per exposure | 1 | 1 **per arm**, each written to up to `N_FOLDERS 2` datapaths (`main.h:73`, `src/Common/FITS.m:128`) |
| Readout time | seconds | per arm, measured: 87 s fast / 158 s slow at 1×1, 28 / 48 s at 2×2 (`src/MIKE/CameraController.m:831-835`), and 2×2 is what observers run |
| Legacy TCP server | none | yes, port 50801, pref-gated (`AppDelegate.m:234,246`) — parity constraint |
| Reply path | n/a | one shared `tcpResponse` property (`GUI_Controller.h:88`) |
| Deployment target | 10.15 | **10.13** — blocks `WSServer` |
| Hardware-free testing | `DataSimulator` (partial) | runtime "offline" popups **plus** a full C simulator that binds the WS port |
| Windows beyond the main one | Calibration | Configuration, Preferences, DataPaths, Subrasters, PLC-Hardhat, Camera Hardhat, Dewar Status, CCD Voltages, QlTool |
| Slit viewer | `pfs-sv`, deployed | `mike-sv`, in the inventory, one number short |

Scope: **the "MIKE" window in `src/MIKE/MikeGUI.xib` (XIB window id
`QvC-M9-y7g`, ~147 controls, `MikeGUI.xib:101`) only.** Deferred explicitly, to
be taken later as extra manifest windows with no protocol change: `Subrasters`
(`MikeGUI.xib:1608`), `PLC-Hardhat` (`MikeGUI.xib:1911`), the Configuration and
Preferences and DataPaths windows in `Base.lproj/MainMenu.xib`, and the camera
hardhats in `CameraHardhat.xib`. The Cocoa `QlTool` window stays Cocoa; the
browser quick look is a different pipeline reading the FITS off disk.

One footnote for whoever runs the converter: `QvC-M9-y7g` is **not unique in
this repo** — `Base.lproj/MainMenu.xib:768` uses the same id for the "MIKE -
Configuration" window (both windows came from the same Xcode template). The id
alone is not a key; record the (file, id) pair in `tools/xib2ir/README.md`.
Matching is exact, not substring (`tools/xib2ir/xib2ir/parser.py:34-39`), so
`--window MIKE` against `MikeGUI.xib` is unambiguous and safe.

## Port assignment — the formula port is taken

`PROJECT_ID 8` (`src/MIKE/main.h:17`) → `50001 + 100·8 + 2` = **50803**.

That number is already spoken for inside MIKE:

```
src/Simulator/mike.h:101-108
enum mike_ports {
  CAM_PORT = 50001+(100*PROJECT_ID),   /* 'mikegui' command port */   50801
  BLU_C_PORT,                          /* mike-blue command port */    50802
  BLU_D_PORT,                          /* mike-blue dacq-server port */50803  <-- WS port
  RED_C_PORT,                          /* mike-red command port */     50804
  RED_D_PORT,                          /* mike-red dacq-server port */ 50805
  LAST_PORT
};
```

The Cocoa side derives the same map: `CameraController.m:158` computes the
per-arm command port (`50002 + PROJECT_ID*100 + 2*color`, i.e. 50802 and
50804 — the literals are even in the comment), and `CCDIO.m:279` then takes
`dacqport = pint+1`, giving 50803 and 50805 for the bulk-pixel connections
opened at `CCDIO.m:590`.

**The audit script does not catch this.** It greps for `5xxxx` literals and
prints `50000 50001 50002 50801 50802 50804 52000 52401` — a list with 50803
conspicuously absent, which reads as a clean bill of health. 50803 never
appears as a literal anywhere: it is `pint+1` in one file and an unnumbered
`enum` successor in another.

### Is it actually a conflict?

In production, no. Those are **outbound** connections to `ccd04:50803` and
`ccd05:50805` (`main.h:59-60`, `CameraController.m:151-159`); a loopback
listener on `127.0.0.1:50803` and an outbound socket to a remote `:50803` are
different 4-tuples and coexist fine.

In the hardware-free path, yes, and that is the path this work lives in. Both
the compile-time simulator (`#ifdef SIM_ONLY`, `CameraController.m:150-151`)
and the runtime offline mode (`status.camstat` from the `dbe_ccd_online` pref,
`CameraController.m:148`, `main.h:47`) force `host` to `LOCALHOST` in a release
build. The app then dials `127.0.0.1:50802/50803` for blue, where
`src/Simulator/mikeserver.c` is listening —
`TCPIP_CreateServerSocket(dacq_port,…)` at `mikeserver.c:2576`, with
`dacq_port = DAT_PORT(chip)` at `mikeserver.c:493`. `WSServer` cannot bind
50803 while the blue simulator holds it, and vice versa. Developing the WS
layer and testing it against a simulated blue arm are mutually exclusive.

### Resolution

Keep the WS at 50803 and **move the other user, for the simulator only.** This
is FourStar's precedent (`ws-migration-fourstar-plan.md` § Port assignment):
the `+2` convention is baked into `instruments/ports.yml`, the gateway's
derived port, and the generated cloudflared ingress, so a one-off offset for
one instrument is a permanent trap in three places instead of a local one.

Concretely, and only under `SIM_ONLY` / offline-with-localhost:

1. `src/Simulator/mike.h:101-108` — add a `+10` to `CAM_PORT`'s base so the
   simulator's map becomes 50811/50812/50813/50814/50815. One line.
2. `src/MIKE/CameraController.m:158` and `src/MIKE/CCDIO.m:279` — the same
   offset on the app side when the host has been forced to localhost, so the
   two halves still agree. Guard it with the existing `SIM_ONLY`/`camstat`
   condition that already special-cases the host, and log the port as it
   already does (`CameraController.m:159-160` appends `ccd_host(…)= host:port`
   to the log, which makes a mismatch visible immediately).

Cost of not doing it: no hardware-free development of the WS layer, which is
most of the value of the port. Cost of the alternative (moving MIKE's WS port
off the formula): every future reader has to know MIKE is special, and the
deployment file needs an explicit `port:` with a comment — the schema allows
that (`deployments/schema.json`, instrument `port`, "Only for a non-standard
build") but it is a permanent exception for a two-line sim-only change.

Record in `instruments/ports.yml` — the entry already exists and is already
correct at line 32 (`mike: { project_id: 8, ws: 50803, ws_server: false }`);
flip `ws_server: true` in the same PR that makes the app listen.
`imageweb/imageweb/server.py:52` already carries `"mike": 50803` correctly, so
unlike Swope there is nothing to reconcile there.

## Hard prerequisite — raise the deployment target to 10.15

`MACOSX_DEPLOYMENT_TARGET = 10.13` in both configurations
(`project.pbxproj:403,456`). `WSServer` is built on `Network.framework`'s
WebSocket support, which does not exist before 10.15, so **nothing in Steps
A–D can be built until this changes**, and changing it is a decision about the
OS on the instrument Mac, not a porting detail.

Two open items, both for a person and not for the source: which macOS
`clay-inst1` runs today (`inventory_lco.yaml:69` gives its address, not its
OS), and whether any remaining 10.13/10.14 machine is expected to run MIKE.
The bump belongs with the standard modernisation set in the
`lco-macos-app-fixes` skill (Apple-Silicon target, light-mode launch, git
version stamping) rather than as a lone commit here.

While in the project file: `SIM_ONLY` is `#undef`'d in source
(`src/MIKE/main.h:28`) and defined by **no** build configuration
(`GCC_PREPROCESSOR_DEFINITIONS` is bare at `project.pbxproj:396` and `NDEBUG`
at `:449`), so the simulator build today means editing `main.h`. The same skill
carries "simulator flag per build configuration"; doing it here is what makes
the verification section below repeatable rather than a local hack.

## Step 0 — Audit the MIKE window

Write `~/workspace/mike/docs/ws-migration-step0-mike-window.md` in the step-0
format (`docs/plans/ws-migration-step0-adc.md`). It produces the
outlet → controller coverage matrix, topic snapshots with real values from a
run, and the enable/disable rules. Three things specific to MIKE that the audit
must settle, because guessing them wrong is what would force a rewrite rather
than an edit:

**1. The B/R/2 outlet triples.** Almost every control on this window exists
three times: `edit_exptimeB`, `edit_exptimeR`, `edit_exptime2`
(`GUI_Controller.h:34`), and likewise for `edit_loops`, `edit_runtime`,
`edit_doing`, `but_start`, `but_pause`, `but_abort`. The `…2` outlet is **not a
third arm** — it is a broadcast: `action_exptime:` with `tag == MIKE_BOTH` sets
the B field and re-enters, then the R field and re-enters
(`GUI_Controller.m:1331-1334`), and `action_start:` does the same
(`GUI_Controller.m:1552-1556`). The code aliases the triples into `NSArray`s
(`GUI_Controller.h:33,36,38,41,50`) but those arrays are built at runtime and
are not outlets, so `bindings.yml` — keyed by outlet name — needs all three
entries, with the `…2` one carrying `arm: both`.

**2. Which fields stay read-only on the web.** MIKE is the sharpest instance of
the second-writer problem in `references/app-side.md`. `edit_gratAzB/R` and
`edit_gratElB/R` (`GUI_Controller.h:53-54`) are operator-typed numbers,
persisted in preferences (`DBE_GRATAZ`/`DBE_GRATEL`, read at
`CameraController.m:171-172`) and copied into every FITS header, with no
readback from any mechanism. `edit_filterB/R` and `edit_camcomB/R`
(`GUI_Controller.h:51-52`) are the same shape. A remote client that sets a
grating angle silently poisons every subsequent header and nothing contradicts
it. v1 keeps all four read-only on the web side; the audit says so explicitly
per outlet rather than leaving it to the bindings author.

**3. The modal in the start path.** `action_start:` samples the diffuser and,
if it is not out and the exposure is an object longer than 60 s, opens a
`YalertQuestion` sheet on the instrument Mac and returns if the answer is
cancel (`GUI_Controller.m:1557-1566`); it also gates on `check_gui`
(`GUI_Controller.m:1550`). A WS `start_exposure` must never block on a local
click. The audit records the pre-flight conditions as data so the service can
evaluate them and return an error code instead of opening a sheet.

The audit gates Step A for a fourth reason peculiar to MIKE: **the legacy TCP
interface is strictly less capable than the window.** On the socket path every
setter runs with `tag = MIKE_BOTH` (`GUI_Controller.m:1369`, `sender =
edit_loops[tag]`), so TCP can only ever set both arms at once. Implementing
only what legacy TCP exposes would lock a real limitation into the new
protocol.

## Topics (provisional)

Per-arm data goes **inside** one topic as an array — one snapshot diff, one
`state` frame, and the two halves of one exposure cannot arrive out of step.
The array order is `[blue, red]`, matching `BLU=0`/`RED=1` (`main.h:36-37`), so
a binding's `path` is `exposure.arms[0].exptime`. Arms that are not configured
(`dbe_camera` selects blue-only, red-only or both — `main.h:46`,
`GUI_Controller.m:118`) appear with `"present": false` rather than being
omitted, so a widget's index never shifts under the page.

| Topic | Source | Notes |
|---|---|---|
| `exposure` | `CameraController` properties (`CameraController.h:33-40`) | per arm: `exptime`, `cur_exptime`, `nloops`, `loop`, `run`, `exptype`, `binx`, `biny`, `speed`, `subrmode`, `looping`, `exposing`, `paused`, `start_time`; shared: object name, comment, config, observer |
| `readout` | `CameraController` `reading` + the loop's line counter (`CameraController.m:669-720`) | per arm: `reading`, `lines_done`, `lines_total`, `elapsed_s`, next filename. **The topic that matters most on MIKE**: at 95–160 s per arm the operator watches the readout, not the exposure (`docs/mike.txt:37-40`), and imageweb shows it in its status line between frames |
| `shutter` | `view_shutterB/R` (`GUI_Controller.h:56`), `updateShutter:color:` | per arm; a `class_map` in the bindings, no raster assets ported |
| `disk` | `level_disk`, `edit_disk` (`GUI_Controller.h:20-21`), datapaths | free space per datapath (`N_FOLDERS 2`), which paths are active, next filename per arm |
| `mechanics` | `GUI_Controller` PLC surface | slit **plate** position and `slit_moving` (`popup_slit`, `prog_slit`, `GUI_Controller.h:23-24,87`), lamp selection with its auto-off state (`popup_lamp`, `cw_lamp`, and the idle timer at `GUI_Controller.m:430-442`, `LAMP_TIMEOUT` 300 s at `main.h:84`), diffuser (`GUI_Controller.h:27,90`), per-arm focus (`edit_focusB/R`, `prog_focusB/R`, `focus_moving`) |
| `temps` | `tempMike/Dome/Cell/Outs` (`GUI_Controller.h:83`), `edit_tblue`/`edit_tred` | summary only; the Dewar Status window (`CameraHardhat.xib`, `2JA-Ov-i6l`) is deferred |
| `telescope` | `sampleTCS:` (`GUI_Controller.h:119`) | read-only mirror: RA, Dec, az, el, airmass, parallactic and slit angles, rotator, UT, plus `adcStatus` |
| `logs` | `Logger onAppend` | identical ring and rate-cap mechanics to PFS |

PFS's list for comparison is `InstrumentRouter.m:60-62` (`exposure`, `readout`,
`shutter`, `disk`, `mechanics`, `focus`, `photons`, `telescope`, `logs`); MIKE
folds `focus` into `mechanics` (two numbers, not a window) and has no
`photons`.

Commands, every one taking `arm: "blue" | "red" | "both"` where the GUI has a
B/R/2 triple, mirroring `MIKE_BLUE`/`MIKE_RED`/`MIKE_BOTH`
(`main.h:151-155`): `set_exptime`, `set_loops`, `set_run`, `set_exposure_type`,
`set_binning`, `set_readout_speed`, `set_subraster_mode`, `start_exposure`,
`pause_exposure`, `stop_exposure`, `set_focus`, `autofocus`; and without an
arm, `set_object_name`, `set_comment`, `set_slit`, `set_lamp`. `arm: "both"`
fans out inside the service and returns **one** `ack` after both halves, so a
partial failure is one error rather than two acks the page has to reconcile.
Read-only for v1 per § Step 0: grating angles, filter names, camera comment.

## Step A — `Service/InstrumentService`

Copy-adapt PFS's `~/workspace/pfs/src/PFS/Service/InstrumentService.{h,m}`:
transport-agnostic façade, `-initWithAppDelegate:`, snapshot reads returning
JSON-safe dictionaries, writes wrapped in `dispatch_sync(MAIN_QUEUE, …)`,
errors in `InstrumentServiceErrorDomain` (`busy`, `missing_argument`,
`invalid_argument`, `hardware`), and the `-setEventHandler:` event sink that
imageweb depends on. `__weak` references to the two `CameraController`s and to
`GUI_Controller`.

MIKE-specific, and the reason the `tcpResponse` hazard is cheap here: **the
service calls the controllers directly and never re-enters `action_*`.**
`CameraController` already exposes exactly the surface needed —
`setExposureTime:`, `setLoopCounter:`, `setRun:`, `setBinningX:Y:`,
`setReadoutSpeed:`, `setSubrasterMode:`, `setExposureType:`, `setPause`,
`abort_flag`, `run_loop`, `statusPointer`, plus the class-level validators
`+isValidExptime:` and `+isValidLoops:` (`CameraController.h:44-45,54-73`).
Routing through `GUI_Controller`'s actions instead would write
`self.tcpResponse` mid-request (`GUI_Controller.m:1345,1396,1431,1465`) and
corrupt a concurrent legacy reply; calling the controllers means the two
transports never share a channel. Existing GUI button handlers keep their
direct calls — refactoring them is out of scope — but every new action routes
through the service, so the bypass shrinks.

Two service-level responsibilities that follow from the audit: replicate
`check_gui`'s pre-flight and the diffuser check (`GUI_Controller.m:1550-1566`)
as returned errors rather than sheets, and keep the GUI in step after a write —
the window already has `updateExptime:`, `updateLoops:`, `updateRun:`,
`updateBinning:`, `updateSpeed:`, `updateExptype:`, `updateSubrmode:`,
`updateLooping:` (`GUI_Controller.h:105-112`) for exactly this, and KVO on
`looping`/`exposing`/`reading`/`speed`/`subrmode` is already wired
(`GUI_Controller.m:126-142`).

## Step B — `WSServer` + `Logger` into `src/Common`

`src/Common` is byte-identical between MIKE and PFS for every shared file
except `Logger` (`FITS`, `MTwister`, `MagIO`, `Notice`, `Sound`, `Tcpip` all
compare equal; MIKE additionally has `TextField`, `UDP`, `Yutils`, which PFS
lacks and which are untouched). So:

- `WSServer.{h,m}` — new files, copied verbatim from `~/workspace/pfs/src/Common`
  (or DCU; **not** ADC, whose copy is one revision behind). Add to the MIKE
  target with per-file `-fobjc-arc`, since the app is MRC.
- `Logger.{h,m}` — overwrite with PFS's. The diff is exactly the `onAppend`
  broadcast hook and nothing else: the property (`Logger.h:28-36` in PFS's
  copy), `@synthesize`, the `_appendInternal:level:toArray:` refactor so
  `message:` reports real severity, and the MRC block-copy guard against a
  teardown race. MIKE's call sites (`initWithBase:useShared:` in `CCDIO.m:291`,
  `initWithBase:useShared:period:useUT:` in `AppDelegate.m:164`, `.mode`) are
  unchanged by it. After this the two `src/Common` trees are identical again
  for the shared set.

Before installing the hook, scan for logging inside tight loops: the readout
loop logs once a second by construction (`CameraController.m:713`), which
is fine, but `ccdio_logger` appends every command (`CCDIO.m:325`) and the
router's 100 entries/s cap is what stands between that and the browser.

## Step C — `Service/InstrumentRouter`

Copy-adapt PFS's. `APP_NAME @"MIKE"`, the topics table from § Topics, the
`_snapshotForTopic:` switch, the command dispatch table with the `arm`
argument. Keep the threading verbatim — the dedicated `pollQueue`, never main;
the `connections`-vs-`dispatch_sync(main)` deadlock note at PFS
`InstrumentRouter.m:34-45` (and its comment at `:68-75`) applies unchanged —
along with the change-gated per-topic diffing, the log ring under
`@synchronized`, and the `onClientCountChanged` multi-client warning. `hello`
goes out unprompted on connect: the gateway's `/healthz` treats the first
unsolicited frame as proof of life (`gateway.py:233-250`), so an app that waits
to be asked is reported down while working.

## Step D — `AppDelegate` wiring

Mirror `~/workspace/pfs/src/PFS/AppDelegate.m:364-392`: service →
`WSServer initWithPort:50803` → router, the client-count warning block,
`[wsRouter start]` then `[wsServer start]` on the global queue, `[wsRouter
stop]` in teardown. Loopback-only comes free from `WSServer`
(`nw_parameters_set_local_only`); remote access is the tunnel's job.

Placement matters more here than on PFS. The service reads controller state, so
it must be created after `camCon[0..1]` exist, which happens in
`GUI_Controller`'s NIB load (`GUI_Controller.m:121`), which happens when
`AppDelegate` instantiates the GUI at `AppDelegate.m:239` — inside the
Configuration window's "Start" action, not in
`applicationDidFinishLaunching:`. Wire the WS immediately after that `gui`
assignment and before (or beside) the legacy TCP server block at
`AppDelegate.m:245-269`.

**Do not put the WS behind the legacy server's gate.** That block is
conditional on `run_server`, read from the `dbe_tcpip` preference and the
Configuration window's `chk_tcpip` checkbox (`AppDelegate.m:234-236,245`). The
WS listener is the web interface's only transport and must come up whether or
not the operator enabled the legacy socket. If a gate is wanted for the WS,
give it its own preference so the two are independent.

## Quick Look — two arms, two streams

The app's entire image obligation is the event. No HTTP server, no pixels on
the control socket.

### How many files, and when

One exposure on MIKE with both arms configured produces **two logical images**,
blue and red, closed at different times. Per arm: `FITSopen:` builds the name
as `<prefix><run:04d>.fits` — `b0042.fits`, `r0042.fits`
(`CameraController.m:1018-1021`, prefix from `CCDIO.m:35`) — and
`FITS open:paths:` (`src/Common/FITS.m:128`) opens **one `FILE*` per datapath** and
writes every header card and every pixel line to all of them, plus a
`<prefix>.fits` symlink in `datapaths[0]` pointing at the newest file. So with
two datapaths configured, one arm's exposure puts the same bytes in two places.
The canonical path in the event is the `datapaths[0]` copy; the others are
mirrors, and the symlink is never announced (it moves under the viewer).

Run numbers are per arm (`DBE_RUN(color)`, `CameraController.m:43,179`), so
blue #42 and red #42 are not one exposure. That alone means the event must name
its arm.

### `exposure_complete`: one per arm

```json
{"type":"event","name":"exposure_complete","data":{
  "arm":"blue", "id":42,
  "fits_paths":["/data/mike/20260927/b0042.fits"]}}
```

Fired after that arm's `FITSclose:` (`CameraController.m:730`, and the method
at `:1149`). `arm` is `CCD_COLOR(color)`, which already yields exactly `"blue"`
and `"red"` (`main.h:41`). `fits_paths` is a one-element list; MIKE never emits
the deprecated `fits_path` string.

Rejected: **one joined event after both arms close.** The arms are independent
loops with independent durations, so joining would hold the blue frame hostage
to the red readout — up to 160 s of nothing on a page whose whole point is
seeing the frame as it lands — and there would be nothing to wait for when only
one arm is configured. Rejected: **one event with both paths**, for the same
reason plus the fact that they are not one picture.

And explicitly rejected: pasting the two arms into one canvas the way FourStar
does its four chips. Blue and red have different dispersions and different
detectors; a combined image would be a lie (`references/image-proxy.md`).

### What imageweb needs, honestly sized

Two changes, and neither is configuration:

**1. `fits_paths` does not exist yet.** `source.py:81` reads
`data.get("fits_path")` and warns when it is missing; `_decode` uses
`meta["fits_path"]` at `source.py:103`; `status()` reports `fits_path` at
`:138`. The string `fits_paths` appears **nowhere** in the repository. The
FourStar plan proposed the list-canonical cut-over but that plan is still
`Status: plan` and gated on its own PR, so MIKE cannot assume it. Whichever
instrument lands first does the same small change: `announce()` takes
`fits_paths` when present and falls back to `[fits_path]`, `_decode` and
`status()` go list-first, and the header panel shows a count when >1.

**2. One source per arm.** `InstrumentSource` keeps exactly one decoded frame
(`source.py:_frame`, and the docstring says `--keep N` is deferred), so feeding
both arms into one source means selecting red discards the only blue frame the
page had. The operator's normal state is watching one arm while the other
reads, so two sources it is. Today `instrument_app()` builds exactly one
`InstrumentSource` and one `ControlClient` per instrument, wiring
`source.announce` as the client's `on_image`
(`imageweb/imageweb/server.py:104-111`), and mounts `/ws`, `/status` and the
page under that one name (`:151-157`). The change: `instrument_app()` takes an
optional channel list; builds one source per channel; `on_image` becomes a
dispatcher keyed on `data["arm"]`, falling back to the single unnamed channel
when the key is absent so PFS and every other instrument are untouched; mounts
`<channel>/ws`, `<channel>/status` and `<channel>/` per channel with `/`
redirecting to the first. Result: `/image/mike/blue/` and `/image/mike/red/`,
each with its own seq counter, its own tier and its own bookmarkable URL — the
same philosophy the guider page already has.

### Why not two deployment entries

`references/image-proxy.md` recommends "two instrument entries (`mike-blue`,
`mike-red`) … costs nothing in code". On this codebase it costs a great deal
and does not work:

- The schema's `app` pattern is `^[a-z][a-z0-9]*$` (`deployments/schema.json`)
  — a hyphen is rejected outright, so the names in that recommendation are not
  expressible.
- Each entry must have a `instruments/ports.yml` entry and an
  `instruments/<app>/` UI directory or the validator errors
  (`tools/validate_deployments.py:174-184`), so `mikeblue`/`mikered` would mean
  two ports entries pointing at one port and two copies of the SPA manifest.
- The gateway mounts exactly one imageweb sub-application per quick-look
  entry, keyed on `app` (`gateway.py:365,376-377`), so two entries mean two
  bridges where one is wanted.
- The gateway also proxies `/<app>/ws` per instrument entry
  (`gateway.py:184-205`) and builds one `/healthz` target per entry
  (`gateway.py:242`), so the same app would be probed twice.
- Decisively: both entries would open their **own** control WS to the same
  `127.0.0.1:50803` and each would receive **both** arms' events —
  `ControlClient._handle` has no filter (`imageweb/imageweb/control.py:70-76`).
  Arm awareness has to exist in the bridge either way, so the two entries buy
  nothing and add four kinds of duplication.

So: **one `mike` entry with `quicklook: true`**, and a new `arms: [blue, red]`
field on the instrument entry in `deployments/schema.json` for the gateway to
know which sub-routes to mount and the landing page to link.

### Two tabs without the duplicate

The Quick Look tab is appended at run time, not listed in the manifest, and
hand-adding an `embed` entry for it is the documented way to end up with two
tabs. The out is in the code: the auto-append is skipped when the manifest
already contains a window whose id equals `"quicklook"`
(`window-host.js:305-308`, against `quicklookWindow()`'s `id: "quicklook"` at
`:126-134`). So the manifest carries

```json
{ "id": "quicklook",     "title": "Blue", "embed": "/image/mike/blue/" },
{ "id": "quicklook_red", "title": "Red",  "embed": "/image/mike/red/"  }
```

and the page shows exactly two quick-look tabs, both live, no duplicate. (There
is also an unschema'd `inst.quicklook_path` override read at
`window-host.js:132`; it is not needed here, and noting it saves the next
person the search.)

### Co-residence

`fits_paths` are local absolute paths and the control WS is loopback-only, so
imageweb runs **on the MIKE instrument Mac** — `clay-inst1` in the current
inventory. `fits_root:` is in the schema for the split-host case and has no
consumers anywhere in the repo, so a gateway on another host would be handed an
unreachable path with no error. Plan co-residence; if that ever changes,
implement `fits_root` first.

One temptation to name and refuse: MIKE already keeps a stretched, LUT-applied
buffer for its own Cocoa quick-look window (`QltoolController`, fed line by
line from the readout loop at `CameraController.m:697-710`) and streaming that
would skip FITS decoding entirely. Don't. The viewer's premise is client-side
stretch over 16-bit linear ADU; pre-stretched pixels throw away what the
histogram, the cuts, imexam and the header panel work on, and freeze the Cocoa
LUT into the web page.

## Slit viewer — ready, blocked on one number

MIKE's slit viewer is a ZWO camera on its own Raspberry Pi behind gcam, exactly
like PFS's, and **the Cocoa app knows nothing about it** — the audit's `slit`
and `guider` hits in `src/MIKE` are all the slit *plate*, a named position on
the PLC mechanism (`popup_slit`, `prog_slit`, `action_slit:` at
`GUI_Controller.m:1082`), which belongs in the controls SPA as a popup and has
no image. The camera is already provisioned:

- `lco-ansible inventory_lco.yaml:78` — `{ name: mike-sv, ini: mike, host:
  mike-sv.lco.cl, tcs_mode: 2 }` on `clay-inst1`, beside `pfs-sv`.
- `lco-ansible roles/gcam/defaults/main.yml` — a full `gcam_ini_settings.mike`
  block (optics, `angle: -119`, `gnum: 3`, `mode: 3`).

So there is nothing to build. The name, however, is not MIKE's to own.

**`gcam_name` is `gcam13` — the same name PFS's slit viewer uses.** The digit
pair is `gcam<rotator port><gnum>`. MIKE sits at Clay's Nasmyth East platform,
which is TCS rotator port 1, and the deployed launch command says so directly:
`mikesv` is an alias for `zwogcam -f mike.ini -t2 -p1 -h 200.28.147.147`
(recorded in MIKE-46, 2024-02-25), where `-p` is the rotator port
(`~/workspace/zwo/src/gcam/zwogcam.c:383`). `mike.ini` carries `gnum 3` and has
its port line commented out and question-marked (`#port 1  ?`,
`etc/ini/clay_gcam/mike.ini:2`) precisely because the port arrives on the command
line. Port 1 plus guider 3 gives `gcam13`, and `etc/ini/clay_gcam/pfs.ini` says
port 1, gnum 3 as well.

That is not a mistake to fix, it is what the platform has. Nasmyth East offers
exactly three guider slots, because `gnum` is capped at 1–3
(`~/workspace/zwo/src/web/gcamweb/server.py:34`) and the image port is
`52300+gnum` and nothing else. Two are the telescope's own guiders
(`etc/ini/clay_gcam/gcam11.ini` port 1 gnum 1, `gcam12.ini` port 1 gnum 2 — the
NASE Shack-Hartmann and principal guider), leaving slot 3 for whichever
instrument is mounted. `gcam13` therefore means "the NASE slit viewer", not
"PFS's camera".

**The consequence for the deployment file, which must be written down rather
than discovered.** The gateway joins the deployment's `gcam_name` against
gcamweb's own `guiders.json` (`gateway.py:296-303`), so if a `clay.yml` listed
both `pfs-sv` and `mike-sv` with `gcam_name: gcam13`, both URLs would proxy to
the one `gcam13` stream and the page label would lie about which instrument the
operator is looking at. Since MIKE and PFS are never mounted together, the
correct shape is that Clay's deployment file and `gateway_gcamweb_guiders` name
**only the slit viewer of the instrument currently mounted**, changed at
instrument change alongside the rest of the swap. The alternative — both entries
present, one down — is worse than it sounds: `/healthz` would report a guider
down that is not missing but mislabelled.

Worth settling with whoever runs instrument changes: whether the swap procedure
can carry a one-line ansible variable change, or whether the deployment should
name the slot (`nase-sv`) rather than the instrument. Naming the slot would make
one entry correct all year at the cost of a URL that no longer says MIKE.

Everything else the guider page does — the guide box from `GDBOXX/Y`/`GDDX/Y`,
readout in camera pixels, per-client region and stride, `?roi=…&every=N` in the
URL — MIKE inherits unchanged. No viewer work.

Ansible side, two names for one camera: `gcam_guiders[].name` is `mike-sv`
(already present), and the gateway host's `gateway_gcamweb_guiders` takes
**gcamweb's** name and rejects `mike-sv`.

## Deployment — Clay has no gateway yet

This is the part with no precedent to copy from, because `deployments/` holds
exactly one file (`sbs.yml`) and `inventory_lco.yaml` has **no gateway host at
all** (`grep gateway inventory_lco.yaml` is empty; the only gateway host vars
in the repo are `inventory_sbs.yaml:56-68`). The name is at least already
expected: the gateway role defaults `gateway_deployment` to whichever of
`sbs`/`clay`/`baade`/`swope` a host's groups intersect
(`roles/gateway/defaults/main.yml:20`), so a `clay` group picks up
`deployments/clay.yml` with no extra variable.

New `deployments/clay.yml`: `name: clay`, `domain: clay.chimera.observer`, a
`gateway:` host, one `instruments:` entry (`app: mike`, `host: clay-inst1`,
`address: 127.0.0.1`, `quicklook: true`, `arms: [blue, red]`, port omitted so
it derives to 50803), one `guiders:` entry (`name: mike-sv`, `gcam_name:` the
settled value, `ini: mike`, `gnum: 3`, `tcs_mode: 2`), and an `access:` list.
Instruments are **paths, not subdomains** — one hostname per site, because
Universal SSL does not cover a second label; `mike.clay.chimera.observer` was
tried and reverted.

Four consequences of being the second deployment file, all worth knowing before
the PR:

1. **`access:` will overwrite the hand-written Clay entry.**
   `deploy/access-policies.yml` already carries `clay` with
   `domain: clay.chimera.observer` and `*@carnegiescience.edu`, and the
   generator replaces `telescopes[name]` wholesale from the deployment file
   (`tools/generate_deploy_artifacts.py:99-115`). Carry the existing list
   across verbatim or access changes silently.
2. **The generated cloudflared config gets placeholders.** With no
   `deploy/cloudflared/clay/config.yml` committed, `existing_tunnel()` emits
   `<tunnel-uuid>` and `<path to …>` (`generate_deploy_artifacts.py:42-49`).
   Someone has to create the Clay tunnel and commit those two per-host facts;
   the generator preserves them from then on.
3. **Bare `gateway.py` stops working.** `default_deployment()` returns a file
   only when `deployments/` holds exactly one (`deployment_config.py:126-133`).
   From this PR on, every invocation needs `--deployment`. Update the README
   and the ansible role's command in the same change.
4. **`heartbeat=20` on every proxied socket** is not decoration: a Cloudflare
   tunnel cuts an idle socket at 125 s and a never-written one at exactly
   20:00. Both were found over a full night
   (`docs/reports/night-test-2026-09-17.md`) and MIKE's 160 s readouts sit
   right on top of the first one. Read that report before touching the proxy.

lco-ansible, in `inventory_lco.yaml`: `MIKE` is already in Clay's
`instruments:` list (`:133`), so the app install is covered. Add a gateway host
group modelled on `inventory_sbs.yaml:56-68` — `gateway_deployment: clay`,
`gateway_gcamweb_guiders: [<mike's gcam name>]`, `gateway_version` (a tag, not
`main`), `cloudflared_tunnel_id`, `cloudflared_hostname: clay.chimera.observer`.
Note that the gateway role still rsyncs two working trees from the control
laptop (`docs/plans/pre-production-todo.md`); assume those become pinned
artifacts and do not add a third rsync.

### The "both" column is a fan-out, not a third camera

The window has three columns of exposure widgets: blue, red, and a common set
(`edit_exptimeB/R/2`, `but_startB/R/2`, `GUI_Controller.h`). The common column is
not a synchronized trigger and must not be bound as one. Typing an exposure time
there writes the value into both per-arm fields, and the common Start calls the
blue start and then the red start back to back, launching two independent loops;
v3.1 deliberately stopped it synchronizing exposure time and loop count, and the
ability to start the arms separately is a feature observers use and noticed
losing when a 2023 regression removed it.

So bind the common controls as a fan-out: one command per arm, or one command
carrying `arm: both` that the service expands. Nothing in the UI should imply the
two arms fire together, because they do not.

## Web-repo work (lco-instrument-web)

1. `instruments/ports.yml:32` — flip `ws_server: true`, in the PR that makes
   the app listen and not before.
2. `python3 -m xib2ir extract ~/workspace/mike/src/MIKE/MikeGUI.xib --window
   QvC-M9-y7g --app mike --bindings ../../instruments/mike/window/bindings.yml
   -o ../../generated/mike/window.layout.json`, committed. Record the (repo,
   file, window-id) triple in `tools/xib2ir/README.md` in the same commit —
   the generated JSON does not store the XIB path, and the id is ambiguous
   across MIKE's two XIBs.
3. `instruments/mike/window/bindings.yml`, written from the Step 0 matrix, each
   disable rule commented against the `GUI_Controller.m` line it mirrors. Three
   entries per B/R/2 triple. Quote every `value_map`/`label_map` key that YAML
   1.1 would read as a boolean (`on`, `off`, `yes`, `no`) — this bit
   `pfs/calibration` already.
4. `instruments/mike/manifest.json` — the `window` entry plus the two
   quick-look tabs from § Two tabs without the duplicate. Nothing validates a
   manifest against the layouts it names, so a typo shows up in the browser as
   "Layout not available", not in CI.
5. `imageweb/imageweb/source.py` — `fits_paths` list-first.
6. `imageweb/imageweb/server.py` — per-arm channels in `instrument_app()`;
   `KNOWN_PORTS` already correct at `:52`.
7. `imageweb/imageweb/control.py` — arm-keyed dispatch in `_handle`, with the
   no-arm fallback so every other instrument is untouched.
8. `deployments/schema.json` — the `arms` field on an instrument entry.
9. `gateway.py` — mount the per-arm quick-look sub-routes; landing-page cards
   for both arms and the guider.
10. `deployments/clay.yml` — new, per § Deployment.
11. `uv run python tools/validate_deployments.py --inventory ../lco-ansible`,
    then `tools/generate_deploy_artifacts.py --check` and `--write`, and commit
    the generated diff. Do **not** hand-edit `index.html` or the cloudflared
    config; older per-instrument plans say to and are stale.
12. Optional: a `diagnostic.js` `APP_REGISTRY` entry for MIKE's hand-command
    form. The lookup is not case-folded, so it must match `hello.app` exactly.
    PFS has no entry and works fine without one.

## Verification

Hardware-free through step 6, on one Mac.

1. Raise the deployment target, add a SIM configuration, move the simulator's
   port map. Build `src/Simulator` (`mikeserver`) for blue and red and confirm
   they bind the **relocated** ports and that the app's log line
   (`ccd_host(Blue)= …`) shows the same numbers.
2. `websocat ws://127.0.0.1:50803/` — a `hello` arrives unprompted, listing the
   topics. Subscribe to each; confirm every snapshot has a two-element `arms`
   array with `present` reflecting `dbe_camera`.
3. Run a blue-only loop. Watch `exposure` diff, `readout` count lines through a
   full 95–160 s readout, then exactly **one** `exposure_complete` with
   `arm:"blue"` and a `fits_paths` whose single path exists on disk. No
   `fits_path` key.
4. Run BOTH. Confirm **two** events, one per arm, at different times, with
   different `id`s, and that each arm's `run` advanced independently.
5. Configure two datapaths. Confirm the same bytes land in both and that the
   announced path is the `datapaths[0]` one, not the symlink.
6. Legacy parity: capture a full legacy TCP transcript before Step A and after
   Step D and diff it — the diff must be empty. Then drive a legacy request and
   a WS command concurrently in a loop and confirm no reply crossover (the
   `tcpResponse` risk).
7. `imageweb --instrument mike` plus a browser on `/image/mike/blue/` and
   `/image/mike/red/`: each readout appears within seconds of its own arm's
   close, the two streams have independent seq counters, header cards show
   verbatim in the `k` panel, and no decode work happens with the tab inactive.
8. `gateway.py --deployment clay` locally: the SPA renders the MIKE window
   against live topics, commands round-trip and are visible in the Cocoa GUI,
   the `arm: both` widgets move both arms, and exactly two quick-look tabs
   appear.
9. Drive step 8 through Chrome's DevTools port rather than trusting a
   screenshot — dispatch the input, read back the DOM and the page's own state.
   A second Chrome profile is needed; the default one refuses a debug port.
10. Deploy: `uv run ansible-playbook -i inventory_lco.yaml playbooks/gateway.yml
    < /dev/null > deploy.log 2>&1` (stdin redirect, and a 1Password prompt a
    person approves). Then `curl -s https://clay.chimera.observer/healthz` —
    MIKE `ok`, and `mike-sv` either `ok` or down with PFS's gcam holding 52303,
    which is expected. A second run reports `changed=0`.

## Risks / open questions

- **Which macOS `clay-inst1` runs**, and whether anything else expects MIKE at
  10.13 — gates every app-side step. A person, not the source.
- **How Clay's deployment file tracks which slit viewer is mounted** — the
  rotator digit is settled (port 1, so `gcam13`), but that name is shared with
  PFS's slit viewer, so one entry has to change at instrument change or the slot
  has to be named instead of the instrument. For whoever runs the swaps.
- **Who lands `fits_paths` first**, MIKE or FourStar. Same change either way;
  coordinate so it happens once.
- **The Clay tunnel** — created and its UUID plus credentials path committed
  before the generated cloudflared config is usable.
- **NIB load order** — confirm `camCon[0..1]` and the `MikeGUI` outlets exist
  when the service is created, given the GUI is built inside the Configuration
  window's Start action (`AppDelegate.m:239`). PFS needed an explicit
  force-load.
- **`dbe_camera` changing under a live page** — the arm array's `present` flags
  handle the single-arm case, but switching configuration requires a restart in
  the app today; confirm in the audit that it cannot change mid-session.
- **The window is about twice PFS's, and that is the schedule risk.** ~148
  controls in the MIKE window against ~61 in PFS's camera window, and PFS's
  camera bindings wire 36 outlets. If the bindings work overruns, the fallback is
  to split the read-only blocks — telescope readouts and temperatures — into a
  second manifest window: no protocol change, and the data-taking controls stay
  in the default tab.
- **The window may want a widget the renderer lacks.** PFS's two
  windows exercised nearly all of the vocabulary, but MIKE's level indicator
  and its per-arm progress clusters should be checked against `renderer.js`
  during the audit. A new IR `kind` plus CSS is a small PR — but it is a PR,
  not a bindings entry.
- **Deferred windows** join later as extra manifest entries and extra topics,
  with no protocol change. The Subrasters panel is the most likely first
  follow-up, since subraster mode is already in `exposure`.

## Files

**App repo (`~/workspace/mike`)** — `src/Common/WSServer.{h,m}` (new),
`src/Common/Logger.{h,m}` (overwrite with PFS's),
`src/MIKE/Service/InstrumentService.{h,m}` (new),
`src/MIKE/Service/InstrumentRouter.{h,m}` (new), `src/MIKE/AppDelegate.m`
(wiring, after `:239`, independent of the `run_server` gate at `:245`),
`src/MIKE/CameraController.m` (the per-arm `exposure_complete` after
`FITSclose:` at `:730`; sim port offset at `:158`), `src/MIKE/CCDIO.m` (sim
port offset at `:279`), `src/Simulator/mike.h` (`:101-108`, sim port map),
`src/MIKE.xcodeproj/project.pbxproj` (deployment target 10.15, target
membership, per-file ARC for `WSServer`, a SIM configuration defining
`SIM_ONLY`), `docs/ws-migration-step0-mike-window.md` (new).

**Web repo (`~/workspace/lco-instrument-web`)** —
`instruments/mike/manifest.json` (new),
`instruments/mike/window/bindings.yml` (new),
`generated/mike/window.layout.json` (generated, committed),
`instruments/ports.yml` (`:32`, `ws_server`), `tools/xib2ir/README.md` (the
recorded invocation), `imageweb/imageweb/source.py` (`fits_paths`),
`imageweb/imageweb/server.py` (per-arm channels),
`imageweb/imageweb/control.py` (arm dispatch), `deployments/schema.json`
(`arms`), `deployments/clay.yml` (new), `gateway.py` (per-arm mounts, landing
page), and the generated `deploy/cloudflared/clay/config.yml` +
`deploy/access-policies.yml`. README/role updates for the now-required
`--deployment`.

**lco-ansible** — `inventory_lco.yaml` (a Clay gateway host group modelled on
`inventory_sbs.yaml:56-68`), `roles/gcam/defaults/main.yml` (`port` for
`gcam_ini_settings.mike`), `docs/ws-protocol.md` (the `arm` key and the
one-event-per-arm rule).
