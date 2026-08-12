# Migrate the LCO Cocoa instrument suite to a WebSocket external interface

## Context

The LCO Cocoa instrument apps (ADC, DCU, Henrietta, Swope, LDSS3, MagE, MIKE, IFUM, M2FS) share the same architectural pattern: each exposes an external TCP/IP command server on port `50001 + PROJECT_ID*100` (ADC = 52401, DCU = 51701, Henrietta = 52801, Swope = 51201). The protocol is line-based ASCII: `cmd par1 par2 par3\n` in, `text\n` out, with `-E…` error prefixes. Implemented in each app's `AppDelegate.m`'s `tcpip_handler:` delegate on top of `src/Common/Tcpip.{h,m}`'s `TCPIP_Server`.

This protocol is brittle for two reasons:
1. **No structured payload** — responses are CSV (`get_wheels` → `"1,2,3,0,5,0"`), space-separated bytes (`status` → `"0 1"`), or raw strings. Adding a field is a breaking change for every consumer.
2. **No live state push** — every consumer (existing Python scripting clients today, a future browser GUI tomorrow) has to poll `status` to learn anything has changed.

The end-state goal is a browser-based control surface for the instruments, with live state and the ability for *any* WS-capable client (a Python scripting library, a browser SPA, a sequencer) to drive the instrument without touching the Cocoa app.

The chosen design:
- **ObjC apps expose a WebSocket service**, JSON-framed, push-based for state, request/response for commands. As small as possible — no HTTP, no static asset serving on the WS port.
- **A separate Python static server** (own small repo) serves the browser SPA static assets and is otherwise unaware of the WebSocket protocol. The browser connects directly from JS to the ObjC WS endpoint. Decoupling means the frontend can iterate independently and the ObjC code stays minimal.
- **A separate HTTP image port** at `port + 3` serves raw FITS files (v2; not v1). Big binary data does not share the control WS. Display is the client's responsibility (JS9 today, a future webasm FITS viewer is its own v2+ project, out of scope here).
- **The legacy line-protocol TCP server stays running in parallel** for one observing run so existing Python TCP clients keep working unchanged.

**Lead rollout: ADC and DCU first.** They have the smallest external command surface (3 commands each) and produce no FITS, so the lead PR is a clean validation of the WSServer + InstrumentService + InstrumentRouter framework with minimal per-instrument complexity. Once that ships and is stable, the migration extends to Henrietta + Swope, then LDSS3 + MagE + MIKE (direct fits), then IFUM + M2FS once their legacy handlers are caught up.

**Out of scope for this plan:** FourStar (its command server lives in a separate C daemon, `src/StarServer/starserver.c`, not the Cocoa AppDelegate) and IMACS (also C-based). Both warrant their own migration plans when the time comes; the protocol design (project rules, message envelope, image-transfer port scheme) carries over but the implementation files do not.

### Operating assumptions (settled)

- **macOS 10.15+ only.** No fallback for older macOS — instrument Macs that can't run 10.15 are out of scope. This unlocks `Network.framework`'s built-in WebSocket support and means no hand-rolled RFC 6455 framing is needed.
- **Auth deferred.** The instruments run on an internal observatory network behind a VPN; v1 ships without authentication. A `requireToken` placeholder is reserved on the WSServer interface but not wired in v1; document the threat model and revisit when the network topology changes.
- **Single client at a time, with a multi-client alert.** No hard control lock in v1 — but if a second WS client connects while another is active, the InstrumentRouter fires a GUI notice and a warning to `main_logger` so the operator sees the contention. Hard control-lock semantics deferred to v2 if it becomes painful.

## Architecture

```
   Browser (laptop)
   ┌──────────────────────────────────┐
   │  index.html + app.js (SPA)       │
   │  loaded from Python static srv   │
   │              │                   │
   │              │ ws://adc:52403/ws  (direct, lead instrument)
   └──────────────┼───────────────────┘
                  │
   ┌──────────────┼───────────────────┐
   │ Python static server  (separate repo/dir)
   │   - serves index.html/app.js/css
   │   - knows nothing about WS protocol
   │   - any commodity HTTP server works:
   │     `python -m http.server`, FastAPI, nginx
   └──────────────────────────────────┘

   LCO Cocoa instrument app (ADC.app / DCU.app / Henrietta.app / …)
   ┌──────────────────────────────────────────────┐
   │ WSServer  (NEW, src/Common/WSServer.{h,m})   │
   │   Network.framework / NWListener + WS opts   │
   │   - subscribe / cmd / ack / state / event    │
   │   - one connection per client, JSON frames   │
   └────────────────┬─────────────────────────────┘
                    │ calls
   ┌────────────────▼─────────────────────────────┐
   │ InstrumentService (NEW, per-app)             │
   │   transport-agnostic methods returning       │
   │   NSDictionary; owns dispatch_sync(MAIN_QUEUE)│
   └────────────────┬─────────────────────────────┘
                    │
   Per-instrument controller(s) — unchanged
   (lead: ADC_Controller, DCUcontroller;
    later: CameraController + GalilHH + TempController, etc.)

   ┌──────────────────────────────────────────────┐
   │ Legacy TCPIP_Server delegate                 │
   │ Becomes a thin formatter on top of           │
   │ InstrumentService. Removed after deprecation │
   │ window.                                      │
   └──────────────────────────────────────────────┘

   Scripting clients (Python)
   ┌─────────────────────────┐    ┌─────────────────────────┐
   │ legacy TCP client       │    │ new WS client (Python)  │
   │  (existing today)       │    │  (small standalone lib) │
   └─────────────────────────┘    └─────────────────────────┘
```

Ports (per app): keep legacy TCP at `50001 + PROJECT_ID*100`; add WS at `+2` and (v2, FITS-producing instruments only) image HTTP at `+3` so all three can run in parallel without colliding. ADC: TCP 52401, WS 52403, no image port (no FITS output). DCU: TCP 51701, WS 51703, no image port. Henrietta: TCP 52801, WS 52803, image 52804. Swope: TCP 51201, WS 51203, image 51204.

## Applicability across the LCO Cocoa instrument suite

Survey of the broader set confirms the design generalizes. A direct fit means Steps A–G below apply unchanged; per-instrument routes / topic shapes vary, the framework does not.

| Instrument   | Cocoa | `Common/Tcpip` | External handler                | Cmds | FITS out | Direct fit?                       |
|--------------|-------|----------------|---------------------------------|------|----------|-----------------------------------|
| ADC          | ✓     | ✓              | ✓                               |  3   | ✗        | **lead (text-only)**              |
| DCU          | ✓     | ✓              | ✓                               |  3   | ✗        | **lead (text-only)**              |
| Henrietta    | ✓     | ✓              | ✓                               | 15   | ✓ (H2RG) | direct                            |
| Swope        | ✓     | ✓              | ✓                               |  8   | ✓ (CCD)  | direct                            |
| LDSS3        | ✓     | ✓              | ✓                               | 17   | ✓        | direct                            |
| MagE         | ✓     | ✓              | ✓                               | 14   | ✓        | direct                            |
| MIKE         | ✓     | ✓              | ✓                               | 18   | ✓ (B/R)  | direct                            |
| IFUM         | ✓     | ✓              | ⚠ `#ifdef RUN_SERVER`, 2 cmds   |  2   | ✓        | needs handler ungating + expansion |
| M2FS         | ✓     | ✓              | ✗ (framework only)              |  0   | ✓        | needs handler implementation       |
| GuidePaddle  | ✓     | client only    | ✗ (it is a TCS client, not server) | n/a | ✗     | out of scope                      |

Rollout order (confirmed):

1. **Lead PR — ADC + DCU.** Smallest surface (3 commands each), no FITS, no wheels, no exposures. Validates the framework on the simplest cases and exposes design issues before larger instruments are touched.
2. **Henrietta + Swope.** Full exposure/wheel coverage; first FITS-producing instruments; introduces the image port (§ Image transfer).
3. **LDSS3, MagE, MIKE.** Direct fits; mostly mechanical translation of their existing `tcpip_handler:` to InstrumentService methods.
4. **IFUM, M2FS.** Only after their legacy handlers are caught up to LDSS3/MagE/MIKE parity. Catching them up is a separate, mechanical PR per instrument (copy LDSS3 handler shape, wire to existing `CCD_Controller`).

**Out of scope: FourStar and IMACS.** Both are C-based — FourStar's command server lives in `src/StarServer/starserver.c` (separate daemon from the Cocoa GUI); IMACS is C end-to-end. The protocol design and port scheme below carry over to them, but the implementation is a separate work item using a C-side WS library, not Apple's `Network.framework`. Plan that when the time comes.

**Out of scope: GuidePaddle.** It's a *client* of a remote TCS, not an instrument server. Nothing to migrate.

Per-instrument port assignments (control WS = `+2`, image HTTP = `+3` only for FITS-producing instruments):

| Instrument | PROJECT_ID | Legacy TCP | WS control | Image HTTP |
|------------|------------|------------|------------|------------|
| LDSS3      |  6         | 50601      | 50603      | 50604      |
| MIKE       |  8         | 50801      | 50803      | 50804      |
| Swope      | 12         | 51201      | 51203      | 51204      |
| MagE       | 15         | 51501      | 51503      | 51504      |
| DCU        | 17         | 51701      | 51703      | n/a        |
| IFUM       | 18         | 51801      | 51803      | 51804      |
| M2FS       | 18         | 51801      | 51803      | 51804      |
| ADC        | 24         | 52401      | 52403      | n/a        |
| Henrietta  | 28         | 52801      | 52803      | 52804      |

(IFUM and M2FS share `PROJECT_ID = 18` in their respective `main.h`. Pre-existing collision; if both run on the same host one needs a fresh ID. Not introduced by this plan but worth surfacing.)

## WebSocket protocol

JSON frames, one message per WS frame. Five message kinds, all with a `type` discriminator.

**Client → server**
```
{"type":"subscribe","topics":["status","wheels","temperatures","pressure","exposure"]}
{"type":"unsubscribe","topics":["temperatures"]}
{"type":"cmd","id":"<client-chosen>","name":"move_wheel","args":{"wheel":"filter","position":3}}
```

**Server → client**
```
{"type":"ack","id":"<echo>","ok":true,"result":{...}}
{"type":"ack","id":"<echo>","ok":false,"error":{"code":"busy","message":"exposure in progress"}}
{"type":"state","topic":"wheels","data":{"grism":1,"diffuser":0,"filter":3,"slit":2,"slide":0,"moving":false}}
{"type":"event","name":"exposure_complete","data":{"id":42,"fits_path":"/data/.../n0042.fits"}}
{"type":"hello","app":"henrietta","version":"1.0.2","build":"b0485","protocol_version":1,"topics":["status","wheels","temperatures","pressure","exposure"]}
```

Rules:
- Server sends a `hello` frame on connect listing available topics.
- `state` is sent (a) on subscribe (snapshot), (b) when the underlying value changes, (c) at most once per second per topic as a heartbeat coalesce.
- `event` is for one-shots (`exposure_started`, `exposure_complete`, `move_started`, `move_complete`).
- Client `cmd.id` is opaque; server echoes it on `ack`.
- Errors use the same code vocabulary as the InstrumentService error domain: `busy`, `missing_argument`, `invalid_argument`, `unknown_wheel`, `hardware`.
- The `hello` frame advertises a `protocol_version` (v1 = JSON-only). Additive changes (new topics, new optional fields) keep the version stable; breaking changes bump it. Clients refuse versions they don't understand.

### Image data is not on the control WS

FITS files do not flow over the control WebSocket. A multi-MB pixel array on the same socket as state pushes and command acks would stall the control channel. Image transfer happens on a dedicated HTTP-GET-only port at `port + 3` — see "Image transfer" section below for the full design.

The control WS announces images via `event` frames carrying `image_id` / `shape` / `dtype` / URL; the client fetches the bytes from the image port. The control protocol stays JSON-only.

(Live readout streaming — partial-image updates during a long readout — is a separate v3+ slice that this plan does not address.)

### Command set

| Command name        | args                          | Henrietta | Swope | Replaces legacy   |
|---------------------|-------------------------------|-----------|-------|-------------------|
| `version`           | —                             | ✓         | ✓     | `version`         |
| `set_exptime`       | `{seconds}`                   | ✓         | ✓     | `exptime <s>`     |
| `set_object`        | `{name}`                      | ✓         | ✓     | `object NAME…`    |
| `set_imagetype`     | `{type:"object\|dark\|flat\|bias"}` | ✓   | ✓     | `imagetype …`     |
| `start_exposure`    | `{loops}`                     | ✓         | ✓     | `start [N]`       |
| `move_wheel`        | `{wheel,position}` (Henrietta: integer; Swope filter: name) | ✓ | ✓ | `move_<wheel>` |
| `wheel_names`       | `{wheel}`                     | ✓         | (n/a) | `wheel_names`     |

Topic snapshots:
- `status` → `{loop, motor_moving, exposure:{running, ...}}`
- `wheels` (Henrietta) → `{grism, diffuser, filter, slit, slide, moving}`
- `wheels` (Swope) → `{filter, moving}`
- `temperatures` → `{temps:{name:value,...}, unit:"C", timestamp}`
- `pressure` → `{value, unit:"mbar", timestamp}`
- `exposure` → `{running, id, remaining_s, loop_left, fits_path}`

## Image transfer (v2: dedicated HTTP port for FITS)

FITS-producing instruments (Henrietta, Swope, LDSS3, MagE, MIKE, IFUM, M2FS) ship raw FITS files to clients over a separate read-only HTTP port at `50001 + PROJECT_ID*100 + 3`. Not part of the lead ADC+DCU rollout, and not part of v1 generally — v1 ships no image transfer at all — but specified now so the v1 control-WS message shapes (`image_id`, `shape`, `dtype`) are forward-compatible with v2 image events.

**Display is the client's responsibility.** The backend serves raw FITS bytes. Today, JS9 (a mature browser-side JS FITS viewer) consumes HTTP-served FITS directly. A future small webasm-based custom FITS viewer is its own v2+ project, out of scope here. The point is that the backend has no opinion on rendering — no JPEG conversion, no zscale stretching, no preview generation. That keeps the ObjC code small and lets the rendering tech evolve independently.

### Endpoint (port + 3)

```
GET /fits/<image_id>   → full FITS file, supports HTTP Range, Content-Type: application/fits
```

`<image_id>` is a stable opaque token issued by the backend at exposure-complete time; once minted it never changes. URLs are cacheable. The mapping `image_id → on-disk FITS path` is held by `InstrumentService`, the same place that knows the data path.

### Control-WS announces, image port serves

After a readout completes, the control WS pushes:

```
{"type":"event","name":"image_ready","data":{
   "image_id":"img_42",
   "shape":[2048,2048],
   "dtype":"uint16",
   "fits_url":"http://<host>:<port+3>/fits/img_42",
   "timestamp":"2026-..."
}}
```

The client decides whether to fetch (with optional `Range:` header for partial reads) and how to render. JS9 takes the URL and renders.

### Implementation sketch

A new `ImageHTTPServer.{h,m}` in `src/Common/`, also built on `Network.framework`'s `NWListener`. Surface intentionally tiny:

- Parse Request-Line + headers only (no POST, no cookies, no chunked encoding, no TLS).
- One route (`/fits/<id>`).
- Stream file body via `NWConnection sendData:` in chunks; honor a single `Range:` byte range if present.
- `Content-Type: application/fits` (or `application/octet-stream`).
- `Cache-Control: public, max-age=31536000, immutable` on `image_id`-keyed URLs.

No image processing in the ObjC app. It maps `image_id` → path and streams the bytes.

### Why HTTP, not a second WebSocket

- JS9 and similar libraries consume HTTP-served FITS directly with `Range:` support. WS frames would require a custom adapter.
- `Range:` requests give partial-FITS retrieval for free (huge for large multi-extension FITS).
- Caching by immutable URL works correctly out of the box.
- HTTP failure modes (timeout, retry, status code) are well-understood; reconnect logic on a dedicated image WS is custom code we'd have to write and test.

The trade-off is an extra tiny HTTP server in the ObjC app — strictly read-only single-route GET, ~150 LOC. Instruments without image output (ADC, DCU) simply don't start it.

## Project rules

These constraints govern the migration so the WS work doubles as the foundation for a future backend/frontend split:

1. **`InstrumentService` is the single contract.** All transports — WS, legacy TCP, future SDKs — call into it. The service owns state mutations, the `dispatch_sync(MAIN_QUEUE, …)` plumbing, and error mapping.
2. **Source-of-truth lives in the backend.** The frontend (browser SPA, scripting client, future native UI) renders state pushed from the server. It does not cache, recompute, or invent state. If a frontend needs a derived value, the service computes it and pushes it.
3. **No new GUI action bypasses the service.** Existing button handlers in `CameraController.m` and friends keep their direct calls for now (refactoring all of them is out of scope for this PR), but any new action added from this point on routes through `InstrumentService`. Over time the bypass shrinks naturally; it does not grow.
4. **Protocol changes are versioned.** Use `protocol_version` in `hello`; document additions in a changelog kept next to the WS server source.
5. **Image data lives on a dedicated HTTP port (`port + 3`), not the control WS.** v1 ships no image transfer at all; v2 adds the image port for FITS-producing instruments. The backend serves raw FITS only — display lives in the client (JS9 today, future webasm viewer is its own project). v1 control-WS message shapes (`image_id`, `shape`, `dtype`) are forward-compatible with v2 `image_ready` events, so v1 clients won't break when the image port arrives.

## Implementation

### Step 0 — Audit each instrument's main expose window for coverage

Before writing any `InstrumentService` methods, inventory the primary user-facing window of each instrument being migrated — the "main expose window" or equivalent (the operator-facing control panel that gets used during observing). Engineering windows are explicitly out of scope: hard-hats, motor diagnostics, calibration jigs, preferences panels, the GalilHardhat window, the `galil.csv` debug viewers, and so on. The web UI's first job is to be a usable equivalent of the *primary* operator window, nothing more.

This audit drives Step A: the `InstrumentService` surface must cover every interactive control in the main window, not just what legacy TCP happens to expose today. Anything reachable from the main window but not from current TCP is a gap that gets a new service method.

For each instrument in the rollout:

1. **Identify the main window.** Open the `.xib` containing the day-to-day controls operators use during observing. Reference points:
   - ADC: main app window (ADC in/out, control state, encoder readouts).
   - DCU: main DCU window (flat-field screen + lamp controls).
   - Henrietta: [CameraController.xib](src/Henrietta/Camera/CameraController.xib).
   - Swope: equivalent camera window.
   - LDSS3 / MagE / MIKE: their respective camera/control windows.
   - IFUM / M2FS: main camera windows once their handlers are caught up.

2. **Enumerate every interactive control.** Buttons, popups, text fields, sliders, toggles. Skip windows that aren't the main expose window.

3. **Trace each control to its action method.** What `-IBAction:` fires; what controller method actually performs the work; what state the control reads from.

4. **Cross-reference with legacy TCP.** Is the operation reachable today via a TCP command? If yes, name it. If no, flag it as a gap.

5. **Produce a coverage matrix per instrument**, committed alongside the WS server source for review:

   | UI control (label / outlet) | Action method | Today's TCP command | New `InstrumentService` method | Notes |
   |---|---|---|---|---|

6. **Identify state values the window displays** that aren't captured by today's TCP (live readout temperatures, exposure progress bars, last-frame thumbnails, lamp on/off indicators, etc.). Each becomes a topic on the WS that needs a snapshot + change-publish path.

7. **Sign-off before Step A starts** for that instrument. The matrix gates Step A: implementing only the legacy TCP surface in `InstrumentService` would lock in the existing protocol's gaps and prevent a real web equivalent of the main window.

For ADC and DCU (lead PR), the audits are small enough to inline at the top of their respective `InstrumentService.h` files as a comment block. For richer instruments (Henrietta and on), keep them as separate `docs/main-window-coverage.md` files in each instrument's repo so they remain reviewable artifacts.

### Step A — Extract `InstrumentService` (per app, behavior-preserving)

For the lead PR (ADC + DCU), the surface is driven by the Step 0 audit, not by the legacy TCP command list. The audit (see [ws-migration-step0-adc.md](ws-migration-step0-adc.md) and [ws-migration-step0-dcu.md](ws-migration-step0-dcu.md)) showed legacy TCP covers only ~25% of ADC's main-window surface and <15% of DCU's — most of the operator-visible operations have no scripting path today. The surfaces below cover the entire main expose window for each.

**ADC** — new file `/Users/william/workspace/adc/src/ADC/Service/InstrumentService.{h,m}` (~12 entry points: 7 commands + 3 topics + version + init):

```objc
@interface InstrumentService : NSObject
- (instancetype)initWithController:(ADC_Controller *)ctrl;
// version (also surfaced in `hello`)
- (NSDictionary *)version;
// topic snapshots
- (NSDictionary *)status;            // adc, control, other, update, running, *_moving flags
- (NSDictionary *)lens;              // {lens_a:{encoder, target, moving}, lens_b:{…}}
- (NSDictionary *)telescope;         // {elevation, rotator} (from TCS)
// commands
- (NSDictionary *)setADCInsertion:(NSString *)pos error:(NSError **)err;        // "in" | "out"
- (NSDictionary *)setOtherInsertion:(NSString *)pos error:(NSError **)err;      // "in" | "out"
- (NSDictionary *)setControlMode:(NSString *)mode error:(NSError **)err;        // "auto" | "off"
- (NSDictionary *)setAutoUpdate:(BOOL)enabled error:(NSError **)err;
- (NSDictionary *)triggerManualUpdate:(NSError **)err;
- (NSDictionary *)setRunning:(BOOL)running error:(NSError **)err;               // combined run/stop
- (NSDictionary *)moveLens:(NSString *)lens angle:(NSNumber *)degrees error:(NSError **)err;  // lens="A"|"B"
@end
```

**DCU** — new file `/Users/william/workspace/dcu/src/DCU/Service/InstrumentService.{h,m}` (~15 entry points: 7 commands + 5 topics + version + init):

```objc
@interface InstrumentService : NSObject
- (instancetype)initWithController:(DCUcontroller *)ctrl;
// version (also surfaced in `hello`)
- (NSDictionary *)version;
// topic snapshots
- (NSDictionary *)status;            // running, deployed, error_state
- (NSDictionary *)lamps;             // 8 boolean lamps + quartz {level, value}
- (NSDictionary *)ffs;               // {position, brake, inserted, retracted, moving}
- (NSDictionary *)mcal;              // {position, brake, inserted, retracted, moving}  — NEW (no legacy TCP)
- (NSDictionary *)pressure;          // {air, min, ok}
// commands
- (NSDictionary *)setLamp:(NSNumber *)idx on:(BOOL)on error:(NSError **)err;    // idx 1..8
- (NSDictionary *)setQuartzLevel:(NSNumber *)level error:(NSError **)err;
- (NSDictionary *)setQuartzValue:(NSNumber *)value error:(NSError **)err;
- (NSDictionary *)setFFSPosition:(NSString *)pos error:(NSError **)err;          // "in" | "out"
- (NSDictionary *)setFFSBrake:(BOOL)on error:(NSError **)err;
- (NSDictionary *)setMcalPosition:(NSString *)pos error:(NSError **)err;         // "in" | "out"
- (NSDictionary *)setMcalBrake:(BOOL)on error:(NSError **)err;
@end
```

The service holds a `__weak` reference to its controller, set by `AppDelegate` at construction. It owns the `dispatch_sync(MAIN_QUEUE, …)` calls so transports never touch the main thread directly. Errors use `InstrumentServiceErrorDomain` with codes `Busy`, `MissingArgument`, `InvalidArgument`, `Hardware`.

For the legacy TCP path: keep `tcpip_handler:` only as a thin formatter for the *existing* commands (ADC: `version`/`status`/`angles`, DCU: `version`/`ffsState`/`ffsLamps`) so legacy clients keep working. The new write commands and additional topics live only on the WS — no need to extend the legacy line protocol with new verbs. Capture the TCP transcript before and after Step A; diff against legacy must be empty.

**Scaling to richer instruments.** When Henrietta + Swope, then LDSS3/MagE/MIKE follow, their InstrumentService methods grow accordingly — Henrietta has 11 methods covering wheels (`wheels`, `wheelNamesFor:`, `moveWheel:position:`), exposures (`startExposureWithLoops:`, `setExptime:`, `setObjectName:`, `setImageType:`), and environment (`pressure`, `temperatures`). The framework, error domain, and threading model are unchanged; only the per-instrument methods differ.

### Step B — Add `WSServer` to `src/Common/`

New files (each app gets its own copy, mirroring the existing `Tcpip.{h,m}` duplication). For the lead PR:
- `/Users/william/workspace/adc/src/Common/WSServer.{h,m}`
- `/Users/william/workspace/dcu/src/Common/WSServer.{h,m}`

Built on **`Network.framework`** — Apple's own networking API, macOS 10.15+, no Pods. Use `NWListener` with `NWProtocolTCP.Options` + `NWWebSocket.Options`. Each accepted `NWConnection` arrives already framed. The class surface:

```objc
@interface WSServer : NSObject
- (instancetype)initWithPort:(uint16_t)port;
@property (copy) void (^onConnect)(WSConnection *conn);
@property (copy) void (^onMessage)(WSConnection *conn, NSDictionary *msg);
@property (copy) void (^onDisconnect)(WSConnection *conn);
@property (copy) NSString *requireToken;   // RESERVED — not enforced in v1 (network is VPN-protected)
- (void)start;
- (void)stop;
@end

@interface WSConnection : NSObject
@property (readonly) NSString *remoteIP;
@property (readonly) NSSet<NSString *> *subscriptions;
- (void)sendJSON:(NSDictionary *)dict;
- (void)close;
@end
```

Receive loop: `connection receiveMessageWithCompletionHandler:` → `NSJSONSerialization` → callback. Send: `NSJSONSerialization` → `connection sendData:context:isComplete:completion:`. All callbacks run on a serial GCD queue inside `WSServer` so the consumer doesn't have to think about concurrency. Subscriptions are kept on `WSConnection`. Backpressure: drop coalesced state frames if a client's send queue exceeds N pending (cheap NSMutableArray + counter).

The `requireToken` property is reserved on the interface but unused in v1 — auth is deferred (instrument network is behind a VPN). Wiring it later is a single conditional in the receive loop and an Authorization header check on the upgrade handshake.

### Step C — Add `InstrumentRouter` per app

New files (lead PR):
- `/Users/william/workspace/adc/src/ADC/Service/InstrumentRouter.{h,m}`
- `/Users/william/workspace/dcu/src/DCU/Service/InstrumentRouter.{h,m}`

Single class that wires `WSServer` callbacks to `InstrumentService` and to the topic-publish system:
- **On connect:** send `hello` frame. Track active connection count.
- **On `subscribe`:** store, immediately push current snapshot of each topic.
- **On `cmd`:** dispatch to the matching `InstrumentService` method on a background queue, send `ack` with result/error.
- **On state change:** a few small `NSKeyValueObserver`s on the controller (or a polling timer if KVO isn't already wired). Coalesce updates per topic at most 1Hz, broadcast to subscribed connections.
- **On exposure events** (FITS-producing instruments only, not ADC/DCU): hook into existing `loop_doing` transitions to emit `exposure_started` / `exposure_complete`.

**Multi-client alert.** The router tracks the set of currently-connected `WSConnection`s. When a second connection is accepted while another is active, the router fires a `onClientCountChanged:(NSUInteger)count clients:(NSArray<NSString *> *)ips` callback that the AppDelegate hooks to:
1. Show a non-modal `NSWindow` notice ("Multiple WS clients connected: 192.168.1.4, 192.168.1.7 — operations may conflict") that auto-dismisses when count drops back to 1.
2. Write a warning line to `main_logger`: `"WARNING: 2 WS clients connected: …"`.

This is intentionally not a hard control lock — it just makes contention visible to the operator, who can resolve it by hand. The expected steady state is a single client; the alert exists to catch accidental double-connect scenarios. A hard lock (first-connect takes write privileges, others are read-only) is deferred to v2 if the alert proves insufficient.

### Step D — Wire from `AppDelegate`

In each app's `applicationDidFinishLaunching:` after the existing TCP server start. ADC example (around [adc/src/ADC/AppDelegate.m:174](src/ADC/AppDelegate.m#L174)):

```objc
_instrumentService = [[InstrumentService alloc]
    initWithController:_adcController];

int wsPort = 50001 + PROJECT_ID*100 + 2;       // ADC: 52403, DCU: 51703
_wsServer = [[WSServer alloc] initWithPort:wsPort];
_wsRouter = [[InstrumentRouter alloc] initWithServer:_wsServer
                                              service:_instrumentService];
_wsRouter.onClientCountChanged = ^(NSUInteger count, NSArray *ips) {
    if (count > 1) [self showMultiClientAlert:ips];
    else           [self dismissMultiClientAlert];
};
[_wsServer start];
```

DCU is identical with `_dcuController` substituted. Henrietta/Swope/etc. add the camera/galil/temp controllers when their turn comes.

Reuse the same `chk_tcpip` checkbox / `DBE_TCPIP` pref to enable both servers in lockstep — one switch toggles legacy TCP and new WS together.

Logging: route both legacy TCP and WS messages through `main_logger` with a clear prefix (`tcp:` vs `ws:`) so the operator can grep one logfile during the transition.

### Step E — Python static server (separate, independent)

Out-of-tree from the ObjC apps, deliberately. A small standalone repo (working name `lco-instrument-web`) so the same SPA serves any of the LCO instruments — picking a host is a URL query param. Contents:

```
lco-instrument-web/
├── index.html
├── app.js          # vanilla JS, ~300 lines, opens a WS, renders state
├── style.css
└── server.py       # python -m http.server is enough for v1
```

`app.js` opens `new WebSocket("ws://" + new URL(window.location).searchParams.get("host") + ":" + ws_port + "/ws")`. Host and port are query params so the same SPA serves Henrietta, Swope, LDSS3, etc. No build tooling required for v1; swap to Vue/Svelte later without touching the ObjC side.

Optional: package as a console-script entry point (`lco-instrument-web`) so operators just run the binary on their laptop.

### Step F — New WS client (Python library)

A small standalone Python package (working name `lco-instrument-ws`), publishable on a private index, depending only on `websockets`. Provides:
- A `Client` base class with `connect`, `subscribe`, `cmd(name, **args)`, `wait_event(name)` primitives.
- Per-instrument convenience subclasses, added as each instrument is migrated. Lead: `ADCClient`, `DCUClient`. Next: `HenriettaClient`, `SwopeClient`, then `LDSS3Client`, `MagEClient`, `MIKEClient`. Each subclass exposes the same high-level method names as today's TCP client (where one exists) so existing scripts switch by changing one import.
- Replaces busy-poll-on-status patterns with `await client.wait_event("move_complete")`.

Keep the existing TCP client library importable for one release cycle so production scripts can switch on their own schedule.

### Step G — Deprecation

After ADC + DCU have been stable in production for one full observing run with both servers up:
1. Gate the legacy TCP server behind a hidden default (`defaults write … LegacyTCP -bool YES`), default off, on ADC and DCU.
2. Switch production scripting usage to the WS client for those instruments.
3. Drop the legacy TCP delegate code on those instruments one release later.

Apply the same deprecation cadence to each subsequent instrument as it migrates — H+S start their own one-run window when their PR ships, etc.

## Files

### Lead PR (ADC + DCU)

**New (ADC):**
- `/Users/william/workspace/adc/src/Common/WSServer.{h,m}`
- `/Users/william/workspace/adc/src/ADC/Service/InstrumentService.{h,m}`
- `/Users/william/workspace/adc/src/ADC/Service/InstrumentRouter.{h,m}`

**New (DCU):**
- `/Users/william/workspace/dcu/src/Common/WSServer.{h,m}`
- `/Users/william/workspace/dcu/src/DCU/Service/InstrumentService.{h,m}`
- `/Users/william/workspace/dcu/src/DCU/Service/InstrumentRouter.{h,m}`

**Modified:**
- `/Users/william/workspace/adc/src/ADC/AppDelegate.m` — construct service+router+wsServer at startup; thin out `tcpip_handler:` to call into service; wire multi-client alert.
- `/Users/william/workspace/dcu/src/DCU/AppDelegate.m` — same shape.

### Subsequent rollout (Henrietta, Swope, then the rest)

Mirror the lead structure in each instrument's `src/<Instrument>/Service/` and `src/Common/`. Henrietta/Swope additionally introduce `ImageHTTPServer.{h,m}` in `src/Common/` (the FITS port). LDSS3/MagE/MIKE/IFUM/M2FS pick that up the same way.

### Out of tree (standalone repos, lead PR ships first cut)

- `lco-instrument-web/` — `index.html`, `app.js`, `style.css`, `server.py`. SPA targets ADC/DCU first; instrument is selected by URL query param. Grows additional UI panels as more instruments come online.
- `lco-instrument-ws/` — Python WS client library. Lead release ships the `Client` base class plus `ADCClient` and `DCUClient` subclasses; later releases add `HenriettaClient`, `SwopeClient`, and the rest.

## Verification

For the lead PR (ADC + DCU):

1. **Main-window coverage signed off (Step 0).** The matrix produced in Step 0 has been reviewed and the `InstrumentService` surface implemented in Step A covers every row. No control on the main window is unreachable from WS.
2. **InstrumentService unit tests** — small ObjC test target wiring the service against a mock controller. Verify each method's dict shape and error mapping. Protects the Step A refactor for both apps.
3. **Legacy parity smoke** — drive each app over its existing TCP port (`status`, `version`, `angles` for ADC; `version`, `ffsState`, `ffsLamps` for DCU) with a small script. Capture the TCP transcript before and after Step A. Diff must be empty.
4. **WS smoke (Python)** — `pytest` in `lco-instrument-ws`: open a WS to ADC's 52403 and DCU's 51703, subscribe to topics, send each command, assert on `ack` and `state` shapes.
5. **Browser end-to-end** — load the SPA from `lco-instrument-web`, point it at a running ADC, and exercise *every row* in the Step 0 coverage matrix from the browser. Confirm live status updates arrive without polling. Repeat for DCU.
6. **Multi-client alert** — open two WS clients simultaneously against ADC; confirm the GUI alert appears and the warning logs to `main_logger`. Close one; confirm the alert dismisses.
7. **Mixed-traffic stress** — drive legacy TCP and WS simultaneously for 60s of mixed reads. Verify both log streams interleave cleanly and the main thread doesn't stall.
8. **Idle leak** — leave WS server up 12h with one subscribed client; `lsof -p <pid> | wc -l` stays bounded; memory stable in Instruments.
9. **On-instrument acceptance** — run ADC and DCU at the observatory for a full engineering shift driven by the WS client (production scripting switched), with TCP still up as fallback. Acceptance: completed shift, no operator-visible regressions vs. prior TCP control.

For subsequent rollouts (Henrietta + Swope, etc.), redo Step 0 for that instrument's main window, then repeat 1–9. Henrietta/Swope additionally verify the image port: `GET /fits/<id>` returns the same bytes as the on-disk file, `Range:` requests work, and JS9 in the browser renders an exposure end-to-end.

## Long-term: backend/frontend transition (future, not this PR)

This WS migration is step one of a larger architectural transition: the Cocoa app eventually becomes a *headless backend* — a service hosting `InstrumentService` and the WS server, no `NSWindow`, no menu bar — with the user interface living entirely in clients (browser SPA, Python scripting libraries, possibly a future native client). The work in this plan unlocks that transition; it doesn't complete it. None of the items below are in scope for v1.

### Exit criterion for the larger migration

The transition is complete when the Cocoa app can launch with a `--headless` flag (or equivalent build configuration) and one or more WS clients provide every interactive capability the current GUI offers. Until that's true, "we're migrating to backend/frontend" is a direction, not a milestone.

### Stages after v1

1. **Spike: one ambitious live-updating browser feature.** Pick something that exercises the live-state path hard — recommended candidate is a live temperature/pressure graph fed by the existing [Graph.xib](src/Henrietta/Graph.xib) timeseries. If the WS protocol can drive a real graph at the browser without lag or coalescing artifacts, it'll handle anything else. This is the most informative early-warning system for protocol design problems.
2. **Image transfer (port + 3 HTTP).** Build the `ImageHTTPServer` specified in § Image transfer above. The QlTool image preview ([QltoolController.m](src/Henrietta/QlTool/QltoolController.m)) is the obvious first consumer, since it already does the FITS scaling/stretching that needs to move to the frontend.
3. **Surface the rest of the GUI's functionality through `InstrumentService`.** Each remaining Cocoa screen — preferences, GalilHardhat motor diagnostics, exposure log windows, calibration workflows, detector bias configuration — gets exposed as topics + commands on WS. Unglamorous plumbing; budget for it explicitly. Expect a long tail.
4. **Refactor existing GUI action methods onto `InstrumentService`.** Today's button handlers in `CameraController.m` bypass the service and call internal methods directly. For the headless future to work, those calls need to go through the service so the GUI is a regular client. Sequence it incrementally — every PR that touches a button handler also moves it onto the service. Months of mechanical work with no user-visible payoff; easy to lose momentum.
5. **Headless build target.** Once 3 and 4 are far enough along, add a build configuration that compiles without UI frameworks. The first time it works, you'll discover which `dispatch_sync(MAIN_QUEUE, …)` calls actually depended on the main thread for UI reasons and need to be relaxed.

### Architectural caveats to acknowledge now

- **Browsers are worse than Cocoa for some workflows.** ds9-class image inspection, multi-window layouts, instant keyboard shortcuts, MacOS clipboard integration. Decide early whether the headless future means *full GUI replacement* (browser does everything, including image work) or *coexistence* (Cocoa app stays for image inspection, browser for control + remote monitoring, both backed by the same service). The latter is a much smaller commitment and may be the right answer.
- **Two client styles pulling in different directions.** Scripting clients want discrete RPCs (`expose, return when done`); a live GUI wants pub/sub state. The same protocol can serve both, but the surface keeps growing. Without protocol-evolution discipline (versioning, deprecation notices, schema documentation), the API will balloon. Treat the protocol as a first-class artifact: document it, version it, review changes to it.
- **Cocoa controllers do non-trivial UI work synchronously.** Modal alerts, sheet windows, file pickers. A genuine headless backend has to replace each of those with a structured event the frontend translates. Expect to find a handful of these mid-migration.
- **State-mutation contention shifts.** Today TCP is effectively single-client because main-thread serialization absorbs concurrency. With many WS subscribers, the model changes. v1 leaves it free-for-all; a serious multi-user backend will need an explicit "control lock" or operator-handoff protocol.

## Open questions to resolve before implementation

1. **Where `lco-instrument-web` and `lco-instrument-ws` live.** Standalone repos under the LCO/Carnegie GitHub org so they serve any instrument, or vendored into an existing Python package for faster bootstrap. Recommendation: standalone repos from day one — bundling with a single instrument's deployment package wires in too much specificity, and the multi-instrument applicability is real.
2. **Idempotency for `start_exposure` (relevant once H+S/LDSS3/MagE/MIKE migrate).** Today double-firing returns `-Ebusy` for the second one. Same behavior over WS by default. Worth supporting an `idempotency_key` arg later if flaky network retries become a real problem; not v1.
