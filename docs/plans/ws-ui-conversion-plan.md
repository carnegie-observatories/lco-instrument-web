# Convert Cocoa main expose windows to web UIs (XIB → IR → HTML)

## Context

The WS migration plan ([ws-migration-plan.md](ws-migration-plan.md)) gives each LCO Cocoa instrument a JSON WebSocket service exposing `cmd` / `state` / `event` frames. The browser SPA in [lco-instrument-web/](../..) currently renders a generic command form (`APP_REGISTRY` in [app.js:13](../../app.js#L13)) and a state dump — adequate for proving the protocol but not a real operator surface.

The next step is to render each instrument's **main expose window** — the operator-facing control panel that opens at app launch — as a position-faithful web UI driven by the WS protocol. Lead instruments are ADC and DCU; rollout extends to Henrietta, Swope, LDSS3, MagE, MIKE, IFUM, M2FS as their WS migrations land.

Hand-porting eight XIBs would drift visually per instrument and lock in Cocoa-specific knowledge for every contributor. The XIB is XML, layout in these windows is `fixedFrame="YES"`, and the controller customObject already names every outlet — programmatic extraction is feasible and the right shape for the rollout.

**The existing generic command form is not removed.** It is the most useful tool for shaking out raw protocol behavior (firing arbitrary commands, watching every state push, reading the structured ack) and stays as a switchable "diagnostic" view alongside the new "main window" view. Both share the same WS connection and the same in-memory topic stores.

## Goals & non-goals

**Goals:**
- Visually faithful reproduction of each main window in the browser (positions, sizes, group structure preserved). Operator muscle memory survives.
- Live WS-driven binding for both reads (state pushes) and writes (commands).
- Pipeline that scales: same tool runs across ADC, DCU, then Henrietta/Swope/etc., gated only by per-instrument bindings YAML.
- Coexistence with the existing diagnostic SPA — the generic command form and state dump remain available via a view toggle so protocol debugging stays low-friction.
- **Multi-window per app — SPA infrastructure only in lead PR.** The SPA gains the ability to render any number of XIB-derived windows per instrument behind a sub-tab strip inside the Window view, and lazy-loads each window's layout JSON. **But:** the master WS migration plan ([ws-migration-plan.md](ws-migration-plan.md):245) explicitly defers engineering / calibration / hardhat / preferences surfaces to per-instrument follow-ups. A window can only be wired in the SPA once its underlying Cocoa controller exposes a WS `InstrumentService` surface — bindings YAML cannot bind to topics that don't exist. So lead PR ships the sub-tab strip, manifest loader, and per-tab renderer mounts (exercised against ADC + DCU's single main window each), and leaves the second-window-onward authoring for follow-up PRs that extend the corresponding `InstrumentService` first.
- **Streamed log topic.** The Cocoa apps emit operator-relevant log messages today (`NSLog`, `fprintf(stderr, …)`) that are only visible by tailing Console.app on the instrument machine. A new `logs` topic on the WS protocol broadcasts log lines to subscribed SPAs so the Diagnostic view's existing log-pane becomes a live remote-tail. Lead PR ships the topic + Cocoa-side tap + SPA pane.
- Lead PR ships ADC + DCU end-to-end, matching the WS migration's lead PR scope.

**Non-goals (v1):**
- Authoring engineering / calibration / target-list / hardhat / preferences panels for every instrument. The master WS plan defers those WS service surfaces ([ws-migration-plan.md](ws-migration-plan.md):245); without a service surface there's nothing for the SPA to bind to. The multi-window SPA infrastructure ships in this PR, but only ADC + DCU's main-window bindings land here. Each subsequent window is a coupled change: extend the relevant `InstrumentService` (Cocoa side) + add a manifest entry + author bindings YAML + commit the layout JSON. Per-instrument, per-window follow-up PRs — not a v1 deliverable.
- Pixel-perfect anti-aliasing match. Cocoa points map 1:1 to CSS px; small font/bezel drift is accepted.
- A reflowable / responsive layout. Pixel-frozen; UI scaling is a single uniform `transform: scale` slider. A real reflowable redesign is its own future plan.
- A Svelte/Vue port. The IR is component-library-agnostic; v1 ships a vanilla renderer (~150 LOC). Future Svelte port swaps the renderer.
- Persisted / queryable log history. The `logs` topic is a live stream + small ring-buffer replay on subscribe; no on-disk indexing, no time-range queries, no full-text search. If operators want that later it's a separate service downstream of the WS tap.

## Architecture: two-stage pipeline

```
   .xib  ─────►  xib2ir  ─────►  generated/<app>/<window>.layout.json  ─────►  renderer.js  ─────►  DOM (Window view)
   ▲             (Python,        + instruments/<app>/manifest.json              │  (one instance per active sub-tab)
   │              one-shot)                                                     ├──── shared ws.js (topic stores, cmd, logs stream)
   │                                                                            │
   bindings/<window>.yml (one per window, transcribed from Step 0 audit)        └──── existing app.js → DOM (Diagnostic view)
                                                                                       (topics dump + APP_REGISTRY form + live log pane)
```

Stage 1 — `xib2ir` — Python tool. One-shot, run on demand. Inputs: a `.xib`, a window id-or-title, a `bindings.yml`. Output: a flat-list JSON IR committed to the SPA repo. One XIB can contain multiple windows; the converter is invoked once per `(xib, window)` pair.

Stage 2 — `renderer.js` — small JS module added to [lco-instrument-web/](../..). Loads a `<window>.layout.json` at runtime, builds the DOM, wires each element to WS topic stores and command dispatch via the shared `ws.js` helper. Mounted once per sub-tab in the Window view.

**SPA layout** — header includes a primary view toggle (`Window | Diagnostic`):
- **Window view** (default): a sub-tab strip listing every window declared in the current app's `manifest.json` (e.g. PFS = `Camera | Calibration | Targets`). Selecting a sub-tab loads (and caches) that window's `layout.json` and mounts a `renderer.js` instance in the host element. Single-window apps (ADC, DCU) still get the sub-tab strip — it just has one tab. Last-selected sub-tab persisted to `localStorage` per app.
- **Diagnostic view**: the existing topic-dump panel and `APP_REGISTRY`-driven command form, plus an enhanced log pane that streams the `logs` topic (level filter + auto-scroll). Useful for raw-protocol exploration, log tailing, and as a fallback when bindings are incomplete.

Both views read the same `topic()` stores and dispatch through the same `cmd()`. The toggle is purely a DOM swap; the WS connection is shared.

Generated JSON is committed to `lco-instrument-web/generated/<app>/<window>.layout.json` and served as static assets — not pushed via WS `hello`. The `manifest.json` listing windows is also committed and static. Layout is build-time, not runtime.

CI lint re-runs `xib2ir extract` for every `(xib, window)` pair in each manifest and diffs against the committed JSON, failing if drift is detected. This gives build-step safety without making `xcodebuild` depend on Python.

## IR schema

Flat list of elements, each with `parent_id`. Tree is implicit and reconstructed by the renderer at startup; flat shape is easier to diff in PRs and easier to validate.

```json
{
  "_generator": "xib2ir@0.1",
  "app": "adc",
  "window_id": "TNc-LT-7qW",
  "window": { "title": "ADC", "width": 480, "height": 294 },
  "elements": [
    {
      "id": "460-xA-5Vr",
      "parent_id": null,
      "kind": "box",
      "title": "Telescope",
      "frame": { "x": 17, "y": 20, "w": 446, "h": 62 },
      "content_inset": { "dx": 0, "dy": 12 }
    },
    {
      "id": "TLp-uq-4O7",
      "parent_id": "460-xA-5Vr",
      "kind": "textfield",
      "subkind": "readout",
      "outlet": "edit_elevation",
      "frame": { "x": 83, "y": 23, "w": 50, "h": 21 },
      "title_default": "00.00",
      "binding": { "read": { "topic": "telescope", "path": "elevation", "format": "%.2f" } }
    },
    {
      "id": "533",
      "parent_id": null,
      "kind": "button",
      "subkind": "bevel-toggle",
      "outlet": "but_lamp1",
      "frame": { "x": 18, "y": 13, "w": 54, "h": 42 },
      "title_default": "L1",
      "binding": {
        "write": { "cmd": "set_lamp", "args": { "idx": 1, "on": "$state" } },
        "read":  { "topic": "lamps", "path": "lamps[0].on",
                   "icon_map": { "true": "icon_on", "false": "icon_off", "err": "icon_err" } }
      }
    }
  ],
  "warnings": [
    { "kind": "custom_class", "id": "592", "outlet": "prog_screen",
      "custom_class": "FFprogress", "base_class": "progressIndicator" }
  ]
}
```

Schema rules (one place; renderer trusts these without re-checking):
- `frame.y` is **already top-left in the IR.** The y-flip happens in the converter; the renderer does not flip again.
- `frame` of a child is relative to its parent's `content_inset`-adjusted origin (already applied; renderer treats it as already-relative).
- `binding` carries both `read` and `write` — buttons may have both (lamp buttons). `$state` sigil = "use the local control's current value".
- `title_default` always present so the layout previews with no WS connection (essential for screenshot tests).
- `kind` is the base widget (`button`, `popup`, `textfield`, `progress`, `colorwell`, `imageview`, `box`, `separator`, `label`); `subkind` carries cell-style nuance (`push`/`check`/`bevel-toggle`/`pulldown`/`readout`/etc.).

## Coordinate translation rules

Two non-obvious traps; encode both in the converter so the renderer never has to know:

1. **Cocoa origin is bottom-left, HTML is top-left.** For each frame, `y_html = parent_h - y_cocoa - h`.
2. **NSBox contentView inset.** An `<box>` has a child `<view key="contentView">` whose own frame is offset (e.g., box outer height 62, inner contentView height 50 — a 12 px gap for the title bar). Children of the box are relative to the **inner contentView**, not the box. The converter accumulates this inset when descending. The IR exposes the inset on the box element (`content_inset`) for renderer styling, but child `frame` values are already adjusted — the renderer doesn't re-apply it.

Cell titles live under `<textFieldCell title="...">`, not on the `<textField>` element. Same for `buttonCell`, `popUpButtonCell`. The converter walks one level deeper to find the display string.

UI scaling: wrap the rendered window in a div with `transform: scale(var(--ui-scale))` (not `zoom` — Firefox handles `transform` more predictably and event coordinates work without surprises). Slider in the SPA header sets `--ui-scale`, default 1.5×, persisted to `localStorage`. Required because ADC is 480×294 and DCU is 677×144 — at 1× on a 4K monitor they'd be ~3% of screen height.

## Streamed log topic

A new reserved topic `logs` carries operator-facing log lines from the Cocoa app to subscribed SPAs. The same topic-subscribe protocol the rest of the data plane uses, with one shape difference: the initial `state` frame returns a **ring-buffer replay** (last N entries, oldest first), and subsequent updates arrive as `event` frames rather than re-snapshotted `state` frames. This keeps the wire protocol uniform while matching the append-only nature of a log stream — no diffing N×500-entry arrays each tick.

**Wire format.**

```json
// hello announces logs alongside the data topics
{ "type": "hello", "app": "pfs", "topics": ["exposure", "mechanics", ..., "logs"] }

// subscribe is unchanged
{ "type": "subscribe", "topics": ["logs"] }

// server replies with a ring-buffer snapshot (one state frame)
{ "type": "state", "topic": "logs",
  "data": { "entries": [
    { "ts": "2026-05-14T17:42:01.231Z", "level": "info",  "src": "CameraController", "msg": "exposure 12345 started" },
    { "ts": "2026-05-14T17:42:03.984Z", "level": "warn",  "src": "PLCIO",            "msg": "iodine_temp below 50°C" }
  ] } }

// every subsequent log line is an event frame with a single entry
{ "type": "event", "topic": "logs",
  "data": { "ts": "2026-05-14T17:42:05.001Z", "level": "info", "src": "ArchonController", "msg": "readout complete" } }
```

Entry fields:
- `ts` — ISO-8601 UTC with millisecond precision. Generated server-side.
- `level` — one of `debug`, `info`, `warn`, `error`. Maps to the Cocoa side's existing severity (NSLog has none — see below — so most call sites default to `info`).
- `src` — short component tag (controller class name, subsystem). Free-form string, optional.
- `msg` — single-line UTF-8 text, no embedded newlines (multi-line messages are split into multiple entries).

**Cocoa-side tap — extend the existing `Logger` class.** Every app already routes operator-relevant logging through `main_logger`, a `Logger` instance defined in [Common/Logger.h](../../../adc/src/Common/Logger.h) (Christoph Birk, 2013). `Logger` already takes per-message integer severity levels, writes to a per-night logfile, and feeds a Cocoa NSTableView. Hundreds of `[main_logger message:text level:dt:file:]` and `[main_logger append:text]` call sites exist across ADC, DCU, and PFS.

Rather than introduce a parallel logging path, the plan adds a broadcast callback to the existing `Logger` interface:

```objc
// new on Logger
@property (nonatomic,copy,nullable) void (^onAppend)(const char *text, int level);

// existing append:/message: invoke onAppend after the disk-write side-effect
- (void)append:(const char*)text;
- (void)message:(const char*)text level:(int)level time:(int)dt file:(BOOL)file;
```

The `InstrumentRouter` installs `main_logger.onAppend` on `start`. The block converts each `(text, level)` pair into a ring-buffer entry (synthesises `ts`, maps `level` → severity string, defaults `src` to the app name) and broadcasts an `event { topic: "logs" }` frame to subscribed connections. The ring buffer is owned by the router, not by `Logger` — so non-WS-aware tooling that links `Logger` stays unaffected.

**Zero call-site migration.** Every existing `[main_logger append:…]` and `[main_logger message:…]` call automatically reaches WS subscribers. There is no `WSLog` macro; the existing API stays the API. New call sites use the same `Logger` methods they would have anyway.

**Level mapping.** `Logger`'s `level:(int)` argument already exists, with semantics that drift slightly across apps. Pin a mapping during impl review: `0`→`debug`, `1`→`info`, `2`→`warn`, `≥3`→`error`. The bare `append:` form (no level) defaults to `info`. The mapping lives in `InstrumentRouter`, not in `Logger` — `Logger` keeps its integer; the wire format gets the canonical string.

**Levels & filtering.** Filtering happens client-side (the level dropdown in the SPA log pane); the server emits everything. Server-side filtering would require per-subscription state and adds no real win for ≤ a few hundred entries/min.

**Backpressure.** The ring buffer is fixed-size; if a slow client falls behind, `event` frames pile up in the nw_connection's send buffer. The existing `WSServer` 64 KB-message-cap protects against pathological single frames; for a logs flood, the router rate-caps emit to 100 entries/sec (drop oldest with a synthetic `"… N entries dropped"` warn entry). This matches what every other log streamer does and avoids OOMing the Cocoa app on a stuck client. The cap is applied **after** `Logger`'s disk write — disk logging is the source of truth and is never rate-limited.

**`Logger` modification scope.** The callback property + the two-line invocation in `append:` and `message:` are the entire change to `Logger.{h,m}`. The class is duplicated across all three Cocoa repos (`adc/src/Common/Logger.{h,m}`, same for `dcu` and `pfs`) — the same patch lands on each copy, in line with how `WSServer.{h,m}` is already maintained.

**SPA side.** The log pane in the Diagnostic view subscribes to `logs` on connect. The replay frame fills the pane up to the ring-buffer size; subsequent events append. Standard log-viewer behaviors: pause-on-hover, sticky-tail-when-scrolled-to-bottom, copy-line, level filter dropdown, free-text grep box. The Window view does not show logs in v1 — it's a Diagnostic-view affordance — but the same `topic('logs')` store is available if a per-window log tray is wanted later.

## Multi-window per app

Each app has a `manifest.json` listing every window the SPA can render for it. The `Window` view renders a sub-tab strip from this manifest and lazy-loads layouts as their tab is selected.

```json
// lco-instrument-web/instruments/pfs/manifest.json
{
  "app": "pfs",
  "windows": [
    { "id": "camera",      "title": "Camera",      "layout": "camera.layout.json",      "default": true },
    { "id": "calibration", "title": "Calibration", "layout": "calibration.layout.json" },
    { "id": "targets",     "title": "Targets",     "layout": "targets.layout.json" }
  ]
}
```

Manifest rules:
- `id` is the routing key (used in `localStorage` for last-selected-tab, and in the optional URL hash `#camera` for deep-linking).
- `title` is the sub-tab label, free-form.
- `layout` is a path under `generated/<app>/`.
- Exactly one window has `"default": true` — the tab opened on first connect.
- Order in the array drives sub-tab order.

**Bindings per window.** Each window gets its own `bindings/<id>.yml`. Window-scoped because outlet names are only unique within a controller's customObject; PFS's `CameraController` and a hypothetical `CalibrationController` can both expose a `but_run` without collision. The xib2ir invocation pairs `(xib_path, window_id_or_title) → bindings/<id>.yml → generated/<app>/<id>.layout.json`. Same converter, run N times per app.

**Tab lifecycle.** Layouts are loaded on-demand (first tab activation), but once loaded they stay mounted in the DOM with `display: none` for inactive tabs. Rationale: keeps WS subscriptions alive so re-activation is instant, and a topic that drives state on multiple windows (e.g. `mechanics` shows up on Camera and Calibration in PFS) only needs one subscription. Subscriptions are reference-counted across active renderers.

**Switching apps.** When the WS `hello` indicates a different app than the previously cached manifest, the SPA tears down all mounted renderers, fetches the new manifest, and rebuilds the sub-tab strip. The `logs` and `version` topic subscriptions persist (they're app-agnostic).

**Single-window degenerate case.** ADC and DCU have one window each; their manifest has one entry. The sub-tab strip renders that single tab — the user sees one tab labeled "Main", not a bare window with no chrome. Avoids a special-case branch in the SPA.

**XIB → manifest authoring.** The manifest is hand-authored per app, **not** auto-generated from the XIB. XIBs commonly contain many windows (sheets, popovers, debug palettes) that the SPA shouldn't expose — the manifest is the allowlist. The xib2ir CLI gains a `--list-windows <xib>` helper that prints every window the XIB declares, to make manifest authoring trivial.

## Element-type mapping

| XIB | IR `kind`/`subkind` | DOM | Notes |
|---|---|---|---|
| `<window>` | (implicit; `window` block in IR) | top-level container | dims from `<view contentView>` frame |
| `<box title>` | `box` | `<fieldset><legend>` | `content_inset` carries the title-bar gap |
| `<box boxType="separator">` | `separator` | styled `<hr>` or thin div | direction inferred from frame aspect |
| `<button type="push">` | `button` / `push` | `<button>` | click → `cmd` |
| `<button type="check">` | `button` / `check` | `<input type="checkbox">` | toggle → `cmd`, value ← state |
| `<button type="check"|"bevel">` with state-bound icon | `button` / `state-toggle` | `<button class="state-toggle">` | DCU lamps; both read+write. State applied as CSS class (`.is-on`/`.is-off`/`.is-err`) — **no PNG assets** |
| `<button type="radio" enabled="NO">` | `indicator` / `radio` | `<span class="indicator">` | read-only status |
| `<popUpButton>` | `popup` / `popup` | `<select>` | options from `<menuItem>` titles |
| `<popUpButton pullsDown="YES">` | `popup` / `pulldown` | `<select>` skipping index 0 | first menu item is title placeholder |
| `<textField editable="YES" borderStyle="bezel">` | `textfield` / `input` | `<input type="text"|"number">` | commit on blur/enter; **focus-guard against state pushes** |
| `<textField state="on" borderStyle="border">` (readout) | `textfield` / `readout` | `<output>` | value ← state, with `format` |
| `<textField>` no outlet, decorative | `label` | `<span class="label">` | static text |
| `<progressIndicator indeterminate spinning>` | `progress` / `spinner` | `<span class="spinner">` | shown when `*_moving` true; **optimistic on cmd-send** |
| `<colorWell enabled="NO">` | `indicator` / `swatch` | `<span class="dot">` | state pushes an enum (`on`/`off`/`err`); renderer maps to CSS class. **No raw RGB over WS.** |
| `<imageView>` | `indicator` / `state-icon` | `<span class="state-icon">` | runtime-set in Cocoa; topic pushes enum, renderer maps to CSS class — colored shape/glyph drawn purely in CSS, no PNGs |
| `customClass="..."` on any of above | base kind preserved + `custom_class` field | base rendering | converter emits warning; manual binding required |

## Bindings YAML

Per **window**, transcribed from the Step 0 audit. Lookup is by outlet name (the property name on the controller's customObject — `popup_adc`, `but_run`, `prog_lensA`, `but_lamp1`, etc.). Single-window apps still nest the file under `bindings/`:

```yaml
# lco-instrument-web/instruments/adc/bindings/main.yml
ignore_outlets: [view_eng]   # decorative, no behavior

outlets:
  popup_adc:
    read:  { topic: status, path: adc }
    write: { cmd: set_adc_insertion, args: { pos: $state } }
  but_update:
    write: { cmd: trigger_manual_update }
  but_run:
    read:  { topic: status, path: running, label_map: { true: Stop, false: Run } }
    write: { cmd: set_running, args: { running: $not_state } }
  edit_encA:
    read:  { topic: lens, path: lens_a.encoder, format: "%.2f" }
    write: { cmd: move_lens, args: { lens: A, angle: $value } }
  edit_targetA:
    read:  { topic: lens, path: lens_a.target, format: "%.2f" }   # readonly
  prog_lensA:
    read:  { topic: lens, path: lens_a.moving }                   # spinner visibility
  col_adcIn:
    read:  { topic: status, path: adc, value_map: { in: on, out: off, moving: warn } }
```

DCU lamps get eight explicit rows with `idx: 1..8`. State drives a CSS class on the button (`.is-on` / `.is-off` / `.is-err`) — no PNG icons, just stylesheet rules:

```yaml
  but_lamp1: { read: { topic: lamps, path: lamps[0].on,
                       class_map: { "true": is-on, "false": is-off, "err": is-err } },
               write: { cmd: set_lamp, args: { idx: 1, on: $state } } }
  but_lamp2: { read: { topic: lamps, path: lamps[1].on,
                       class_map: { "true": is-on, "false": is-off, "err": is-err } },
               write: { cmd: set_lamp, args: { idx: 2, on: $state } } }
  # ... but_lamp3 .. but_lamp8
```

Status icons (DCU `view_in`, ADC `view_eng`) bind the same way — `class_map` flips a CSS class on the indicator span. State indicators are stylesheet-only; no raster assets ship with the SPA.

Sigils: `$state` (current control state), `$value` (current input value), `$not_state` (toggle). The converter validates that every sigil resolves to something matching the `args` shape declared by the InstrumentService method.

## Risks called out explicitly

These don't get to be discovered mid-implementation:

- **NSBox contentView inset trap** — see Coordinate rules. Skip it and every box-child is 12 px too high.
- **Buttons can be state-bound, not write-only.** DCU lamp buttons drive `[button setImage:icon_on/off/err]` from `lamps[i]`. Schema and renderer must support read+write on the same outlet from day 1.
- **State writes during typing must not clobber user input.** `<input>` elements check `document.activeElement === this` and skip incoming state if focused. Document the rule in the renderer.
- **1 Hz state coalesce + spinner = flicker.** A 200 ms motor move + 1 Hz coalesce shows "click → silence → spinner appears 1s later → vanishes." Renderer optimistically shows the spinner on `cmd` send and clears on `ack` + state. Works regardless of bandwidth.
- **Bindings drift.** Outlet added in XIB without an entry in `bindings.yml` ⇒ converter emits warning `binding_status: unbound`. SPA logs a console warning and shows a red dev-mode border on unbound elements. Otherwise we'll ship dead buttons.
- **No raster assets ported.** DCU's `icon_on`/`icon_off`/`icon_err`/`icon_in` and ADC's `warning.png` (`view_eng`) are PNGs in the Cocoa bundle, set via `awakeFromNib`. The web UI does **not** copy or ship them — state is rendered as CSS-class swaps on a `<span class="state-icon">` (filled circle, ring, warning triangle drawn purely in CSS). Loses the exact glyph but keeps the semantic state and avoids an asset pipeline.
- **Color values.** ADC color wells have a hardcoded RGB in the XIB that the controller overrides via `setColor:`. The renderer treats the XIB color as ignored and waits for state-driven enum; the IR strips the hardcoded color so it doesn't tempt anyone.
- **Pulldown vs popup distinction** matters for `drop_ffs`/`drop_mcal` (pullsDown="YES" — index 0 is the title) vs `popup_quartz`/`popup_ffbrake`/`popup_mcbrake` (popups — index 0 is selectable). Converter reads `pullsDown` from XML.
- **`label_quartz` is state-driven.** Controller swaps the textField title between "Var.Q" and "Cal" depending on quartz mode. Bound as `read: { topic: lamps, path: quartz.label }` (or similar — confirm during impl from `DCUcontroller.m`).
- **`transform: scale` and event coordinates.** Today's controls are all click/select/type — no drag. Flagged for the future graph widget.
- **Log floods OOM the WS server.** An existing `[main_logger append:]` inside a tight loop (e.g. an aggressive PLC poll path that already logs every reading) becomes a WS flood the moment the `onAppend` callback is installed, even though the on-disk logger has been tolerating it for years. Mitigation: `InstrumentRouter` rate-caps the WS broadcast to 100 entries/sec **before** the ring buffer, with a coalesced `"… N entries dropped"` synthetic entry on overflow. Reviewers should scan the existing high-frequency call sites during impl and consider downgrading them to `level: debug` so the SPA's default level filter (`info` and above) hides them by default. The disk logger is unaffected; this is purely about the WS broadcast.
- **`logs` topic is unique: replay frame ≠ snapshot.** Every other topic's `state` frame is a current-truth snapshot — a renderer treats `state` as "the data is now this". For `logs`, `state.data.entries` is a backlog, and live updates arrive as `event` frames with a single entry. The SPA's topic store has to special-case `logs` so the log pane appends rather than replaces. Document the rule in `ws.js`'s topic factory and ship a unit test.
- **Sub-tab focus loss on switch.** Today's focus-guard rule (skip incoming state if `document.activeElement === input`) protects a focused field within the active window. When the operator switches sub-tabs while typing, the previous tab's focused input is `display:none`'d (still `activeElement` until something else grabs focus — browsers vary). Renderer must drop focus explicitly on tab deactivation, otherwise a stale `<input>` in a hidden subtree keeps blocking state pushes.
- **Multi-app teardown leaks subscriptions.** Switching apps (new `hello` from a different instrument) must walk every mounted renderer and unsubscribe topics it owns before tearing down. Forgetting one means the WS server keeps streaming to a dead DOM tree. Reference-counted subscriptions in `ws.js` need a corresponding "clear all" on app change — not just per-renderer detach.

## Tooling: `xib2ir`

Lives at `lco-instrument-web/tools/xib2ir/`. Same repo as the SPA — run on demand, output ships next to the SPA, ~600 LOC total.

Module layout (five files, not a 600-line monolith):

```
lco-instrument-web/tools/xib2ir/
  pyproject.toml
  README.md
  xib2ir/__init__.py
  xib2ir/__main__.py        # python -m xib2ir
  xib2ir/parser.py          # lxml walk, outlet/connection resolution
  xib2ir/coords.py          # y-flip + NSBox content-inset accumulation
  xib2ir/elements.py        # XIB element → IR kind/subkind table
  xib2ir/bindings.py        # bindings.yml load + apply, sigil resolution
  xib2ir/validate.py        # warnings: unbound, custom_class, missing args
  xib2ir/cli.py             # argparse entry
  tests/                    # pytest, small synthetic fixture XIBs
```

CLI:

```
xib2ir extract <xib> --window <id-or-title> --bindings <yaml> -o <out.json> [--strict]
xib2ir validate <out.json> [--bindings <yaml>]
xib2ir lint <xib> --bindings <yaml>           # CI: fail on warnings
xib2ir list-windows <xib>                     # print every window id+title in the XIB — input to manifest authoring
xib2ir build-manifest <app_dir>               # re-runs extract for every (xib, window) declared in manifest.json
```

Dependencies: `lxml`, `pyyaml`. Installable via `pip install ./tools/xib2ir`.

## Implementation steps (lead PR — ADC + DCU)

Order matters: each step's output is testable on its own, and the schema is forced through a real renderer before the converter calcifies it.

1. **Refactor `app.js` into a shared `ws.js` + a `diagnostic.js`** that owns the existing `APP_REGISTRY` panel and topic dump. Behavior unchanged; just the file split. The Window view in later steps consumes the same `ws.js`. End state: SPA looks identical; the toggle UI doesn't exist yet.

2. **Add a `Window | Diagnostic` view toggle in the header.** Default state: Diagnostic (until a Window IR exists, switching to Window shows an empty placeholder). Persist last selection in `localStorage`. End state: toggle works; nothing visible changes in Diagnostic.

3. **Hand-author a 5-element fixture IR** (`adc/main.layout.json` with one box, one push button, one popup, one editable textfield, one readout) plus a one-entry `adc/manifest.json`. Build the renderer (`renderer.js`) and the Window-view sub-tab strip against this fixture only — no converter yet. End state: visually-faithful empty shell with a single "Main" sub-tab, WS connects, button issues `cmd`, readout updates from `state`. **The schema is pinned by being used before being generated.**

4. **`logs` topic — Cocoa side.** Add an `onAppend` callback property to `Common/Logger.{h,m}` (same patch on all three repos — ADC and DCU in this PR, PFS in its follow-up). Existing `append:` and `message:` methods invoke the callback after their existing disk-write side-effect. The `InstrumentRouter` installs the callback on `start`, owns the 500-entry ring buffer + 100-entries-per-second rate cap, advertises `logs` in `hello.topics`, returns the replay snapshot on subscribe, and fans out live entries as `event` frames. Zero call-site changes — every existing `[main_logger append:…]` and `[main_logger message:…]` site reaches WS subscribers for free. End state: subscribing to `logs` over the Diagnostic CLI tool shows live log lines and the on-disk logfile is unchanged.

5. **`logs` topic — SPA side.** Special-case the `logs` topic store in `ws.js` (append on `event`, replace-with-backlog on `state`). Replace the Diagnostic view's existing log pane with a live `logs` subscriber: pause-on-hover, sticky-tail, level filter, free-text grep. End state: opening the SPA against a running Cocoa app shows its log stream in real time.

6. **`xib2ir` parser, no bindings.** Walk the XIB, emit IR with `binding: null` everywhere. Run on ADC and DCU; commit `generated/{adc,dcu}/main.layout.json` plus a one-window `manifest.json` per app. Renderer displays every element static; no commands wired. Test: visual diff against screenshots of the running Cocoa apps. Acceptable: ±1-2 px font/bezel drift; not acceptable: misaligned groups, missing controls. State indicators (lamp buttons, `view_in`, `view_eng`) render as bare CSS shapes — colored circles, rings — not raster icons.

7. **Bindings v1 — writes only.** Author `instruments/{adc,dcu}/bindings/main.yml` mapping each outlet → `cmd`. Re-run converter. Renderer sends commands on click/change but does not update state. Test against the WS migration's `InstrumentService` — every button that should fire a command does, with the right args. Cross-check against the Diagnostic view's command form: same cmd, same args, same ack.

8. **Bindings v2 — reads.** Add `read:` clauses, including `class_map` for lamp buttons + status indicators, `value_map` for color-well swatches, `format` for numeric readouts. Subscribe to topics on connect. End state: full live UI for ADC and DCU. Cross-check against the Diagnostic view's topic dump: same data, just rendered visually.

9. **UI scale control.** CSS `--ui-scale` variable, slider in the SPA header (Window view only), default 1.5×, persisted to `localStorage`.

10. **Multi-window sub-tab plumbing.** Even though ADC + DCU each have one window, ship the sub-tab strip + manifest-driven loader + lazy-mount logic now. Tested with a temporary two-window manifest on a dev branch (duplicate the ADC layout as `engineering` for the test), reverted before merge. End state: SPA renders the sub-tab strip with one tab per app; the loader infrastructure is in place for PFS's follow-up multi-window PR.

11. **Polish commits, one per concern:** focused-input guard, optimistic spinners, unbound-outlet dev-mode highlight, pulldown index-0 handling, focus management on sub-tab switch (drop focus from inputs in deactivated tabs), focus management on tab order within an active window.

12. **CI lint.** GitHub Action runs `xib2ir build-manifest` for each app and diffs all generated `<window>.layout.json` against committed copies; fails on any diff. Last so CI flapping doesn't disrupt the PR.

13. **Multi-app routing in the SPA.** App identity comes from the WS `hello` frame's `app` field — the SPA fetches `instruments/<app>/manifest.json` after hello arrives. No URL `?app=` override; until the server announces an app, the Window view shows a "waiting for hello" placeholder. Diagnostic view already builds itself from the same hello frame, so it works across apps unchanged.

14. **Default the view toggle to Window** once both layouts are bound and visually verified. Diagnostic stays one click away.

## Files

**New (lco-instrument-web):**
- `lco-instrument-web/tools/xib2ir/` (full module tree above)
- `lco-instrument-web/instruments/adc/manifest.json` (one window: `main`)
- `lco-instrument-web/instruments/adc/bindings/main.yml`
- `lco-instrument-web/instruments/dcu/manifest.json` (one window: `main`)
- `lco-instrument-web/instruments/dcu/bindings/main.yml`
- `lco-instrument-web/generated/adc/main.layout.json`
- `lco-instrument-web/generated/dcu/main.layout.json`
- `lco-instrument-web/ws.js` — shared WS plumbing (extracted from `app.js`); includes the special-cased `logs` topic store
- `lco-instrument-web/diagnostic.js` — existing `APP_REGISTRY` form + topic dump (extracted from `app.js`) + new live log-pane subscriber
- `lco-instrument-web/renderer.js` — new Window-view renderer driven by `<window>.layout.json`
- `lco-instrument-web/window-host.js` — mounts the sub-tab strip + manages per-tab `renderer.js` instances + reference-counts subscriptions
- `.github/workflows/xib2ir-lint.yml` (CI)

State indicators (DCU lamps, `view_in`, ADC `view_eng`) are CSS-only — `.is-on` / `.is-off` / `.is-err` class swaps on a `<span class="state-icon">` or on the button itself. No `assets/` directory; no PNGs ported from the Cocoa bundles.

**Modified (lco-instrument-web):**
- [index.html](../../index.html) — add view toggle (`Window | Diagnostic`), `<div id="window-view">` host (the sub-tab strip + per-tab renderer mounts live inside it), keep the existing `topics-pane`/`commands-pane`/`log-pane` (now wrapped in `<div id="diagnostic-view">`), UI-scale slider, `<script src="ws.js">` + `<script src="diagnostic.js">` + `<script src="renderer.js">` + `<script src="window-host.js">`.
- [app.js](../../app.js) — split into `ws.js` (connection + `cmd` + `topic` store factory, with the `logs`-topic append semantics) and `diagnostic.js` (the existing topic dump + `APP_REGISTRY` form + log pane). The diagnostic surface is preserved verbatim minus the log pane which gets the live-stream upgrade; this is a file-shuffle plus the log enhancement.
- [style.css](../../style.css) — add `.window-frame`, `.box`, `.dot`, `.spinner`, `.indicator`, `.state-icon`, `.state-toggle` (with `.is-on`/`.is-off`/`.is-err` modifiers for state colours), `.subtab-strip`, `.subtab.is-active`, `.log-pane`, `.log-entry.level-warn` / `.level-error`, view-toggle button styles, focus styles.

**Modified (Cocoa apps — shared `Common/`):**
- `Common/Logger.{h,m}` — add `onAppend` callback property; `append:` and `message:` invoke it after their existing disk-write paths. Same patch on all three repos (ADC + DCU this PR, PFS in its follow-up). Existing call sites and the on-disk logfile semantics are unchanged.

**Modified (Cocoa apps — per-app):**
- [adc/src/ADC/Service/InstrumentRouter.m](../../../adc/src/ADC/Service/InstrumentRouter.m), [dcu/src/DCU/Service/InstrumentRouter.m](../../../dcu/src/DCU/Service/InstrumentRouter.m) — install `main_logger.onAppend` on `start`; own the 500-entry ring buffer and 100-entries/sec rate cap; emit `event { topic: "logs" }` frames. `_topics` and the `hello` payload gain `"logs"`; subscribe path returns the ring-buffer replay as a single `state` frame.

**Read-only (Cocoa apps):**
- [adc/src/ADC/Base.lproj/MainMenu.xib](../../../adc/src/ADC/Base.lproj/MainMenu.xib) — input to xib2ir, window id `TNc-LT-7qW`.
- [dcu/src/DCU/Base.lproj/MainMenu.xib](../../../dcu/src/DCU/Base.lproj/MainMenu.xib) — input to xib2ir, window id `371`.
- [DCUcontroller.m](../../../dcu/src/DCU/DCUcontroller.m) and [ADC_Controller.m](../../../adc/src/ADC/ADC_Controller.m) — referenced for icon paths and confirming `label_quartz` dynamic strings; not modified.

## Verification

1. **Diagnostic view unchanged (modulo logs).** Same topic dump, same `APP_REGISTRY` form. The log pane is the one part that changes — it now shows live `logs`-topic entries.
2. **Visual fidelity (Mode A).** Side-by-side screenshot of running ADC.app vs. browser Window view at `--ui-scale: 1.0`. All controls present, all `<box>` groups in the right place, decorative labels in the right place. Acceptable drift: ±2 px on font baselines, bezel widths off by 1 px. Not acceptable: misaligned groups, off-by-12-px (NSBox inset bug), missing controls.
3. **Idempotent converter.** Re-running `xib2ir extract` on an unchanged XIB produces no diff vs. the committed JSON. CI enforces this via `xib2ir build-manifest` over every `(xib, window)` pair in each app's manifest.
4. **Coverage matrix exercised end-to-end (Window view).** Every row in the Step 0 audit ([ws-migration-step0-adc.md](ws-migration-step0-adc.md), [ws-migration-step0-dcu.md](ws-migration-step0-dcu.md)) is exercisable from the Window view: read updates live as topics publish, writes deliver `ack`s with no console errors.
5. **Window/Diagnostic parity.** A command fired from the Window view produces the same `cmd` frame and `ack` as the same command fired from the Diagnostic form. Topic data displayed in the Window view matches the Diagnostic dump for the same instant.
6. **Log topic — subscribe.** Connect with WS, subscribe `logs`; the first `state` frame contains up to 500 ring-buffer entries oldest-first. Subsequent log calls arrive as `event` frames within ~50 ms. The Diagnostic log pane shows them all.
7. **Log topic — rate cap.** Plant a tight `[main_logger append:]` loop (1 kHz) on a scratch branch; confirm the SPA pane receives at most ~100 entries/sec, and that a synthetic `"… N entries dropped (rate cap)"` warn-level entry appears every second the cap is in effect. The Cocoa app does not OOM; nw_connection send queue stays bounded. The on-disk logfile receives every entry (cap applies only to the WS broadcast).
8. **Log topic — replay window.** Restart the Cocoa app; reconnect the SPA. The ring buffer is back to empty, log pane shows zero entries until new lines arrive. (Persistence is explicit non-goal — this verifies it.)
9. **Multi-window sub-tab strip — degenerate case.** With ADC's one-window manifest, the Window view renders a single "Main" sub-tab; clicking it is a no-op. With the temporary two-window dev manifest, the strip shows two tabs; clicking switches mounted renderer without a re-fetch.
10. **Multi-window — subscription survival.** Switch sub-tabs while a `state` push is in flight; topic store updates land on both the active (visible) and inactive (display:none) renderers without errors. Switch back; values match.
11. **Sub-tab focus drop.** Type in an `<input>` on tab A; switch to tab B; trigger a `state` push that would update the same outlet name on A; on switching back, the input shows the pushed value (focus was released, so the focus-guard doesn't keep blocking the update forever).
12. **Optimistic spinner.** Click `but_update` in Window view; spinner appears immediately, before the `ack`. Confirm no flicker.
13. **Focus guard.** Type in `edit_encA`; trigger an unrelated `state` push; user input survives.
14. **Unbound-outlet warning.** Add a no-op outlet to a `bindings/<window>.yml` as `unmapped`, regenerate; SPA logs warning, dev-mode border appears on the affected element.
15. **Multi-app hello → manifest.** WS hello with `app: "adc"` triggers fetch of `instruments/adc/manifest.json`, sub-tab strip rebuilds. Switching to a hello with `app: "dcu"` (e.g. by pointing the SPA at a different running instrument) tears down all ADC renderers, unsubscribes their topics, fetches DCU's manifest, mounts DCU's tabs. The `logs` subscription persists across the app switch.
16. **Scale slider.** Default 1.5× renders both windows readable on a 4K monitor; min/max bounds preserve aspect ratio; preference persists across reloads.
17. **CI lint fails on drift.** Add a control to the ADC XIB without regenerating; CI rejects the PR with a diff in the failure log.

## Future (not this PR)

- **Mode B reflowable layout.** Cluster sibling frames by Y/X proximity inside each `<box>`; emit CSS Grid tracks. Same IR, different renderer. Wait for operator feedback on Mode A before deciding row/column heuristics.
- **Svelte port.** Replace `renderer.js` with a Svelte component generator that emits `<App>MainWindow.svelte` per instrument. IR unchanged; the renderer becomes a code generator. Worth doing once 4+ instruments are on web UIs and shared interactive patterns (timeseries graphs, FITS thumbnails) need real component reuse.
- **PFS multi-window follow-up.** First real consumer of the sub-tab strip. Ships `instruments/pfs/manifest.json` with `camera | calibration | targets`, the three layout JSONs, and the three bindings YAMLs. No SPA code changes — exercises the infra landed in this PR.

- **WS → rsyslog log-centralization proxy.** A small daemon (Python or Go, ~200 LOC) running on the obs1 host that subscribes to the `logs` topic on every instrument WS and forwards each entry to a central rsyslog instance — TLS-wrapped TCP/6514 preferred, falling back to RFC 5424 over TCP/514. Mapping:
  - Entry `level` (`debug` / `info` / `warn` / `error`) → syslog severity (`LOG_DEBUG` / `LOG_INFO` / `LOG_WARNING` / `LOG_ERR`).
  - Entry `src` (controller / subsystem tag) → syslog `MSGID` or app-name suffix (`adc.CameraController`).
  - `app` (from WS `hello`) → syslog `APP-NAME` plus `HOSTNAME` synthesised from the WS endpoint (so a central viewer can split by instrument even though all proxies share an obs1 host).
  - Original JSON entry preserved as a `@cee:`-prefixed structured-data field for downstream tools that want to mmjsonparse it (Loki, Graylog, Splunk all support this).

  Why this beats the simpler "per-night JSONL on disk" option that was previously listed here: rsyslog already solves log rotation, retention windows, off-host shipping, fan-out to multiple sinks (Loki + a local file + a Slack hook), and authenticated transport. Re-implementing any of that is a worse use of time than wrapping the WS subscriber and pushing into a battle-tested daemon.

  The proxy is the *only* component that needs to know how to talk syslog — the Cocoa apps stay protocol-agnostic, and the SPA is unaffected. If a deployment doesn't want rsyslog, swap the proxy for a JSONL-to-disk variant; the WS contract is unchanged.

- **WS telemetry → time-series DB → Grafana proxy.** Sibling daemon to the rsyslog one. Subscribes to the numeric `state` topics on each instrument WS (`telescope`, `exposure`, `mechanics`, `lens`, `status` — anything the Step 0 audits flagged as a numeric readout) and writes the values to a time-series DB. Two viable targets:
  - **InfluxDB / Telegraf**: write a Telegraf input plugin that wraps the WS client; tags = `{app, topic, src=instrument-host}`, fields = the leaf numeric paths of each snapshot. Telegraf handles batching, retries, downsampling. Grafana queries InfluxQL/Flux for dashboards.
  - **Prometheus pull**: the proxy exposes `/metrics` on an HTTP port, holding the last-known snapshot per topic. Prometheus scrapes every 15 s. Simpler ops story (Prometheus is already in the observatory's monitoring stack); loses sub-second resolution.

  Field extraction is declarative — a `telemetry.yml` per app declares which JSON paths inside each topic snapshot are numeric metrics and what tag/field names they get. Strings and enums are skipped. Example:

  ```yaml
  # tools/ws-tsdb-proxy/telemetry/adc.yml
  app: adc
  metrics:
    - topic: telescope
      paths:
        - { path: elevation, field: elevation_deg }
        - { path: rotator,   field: rotator_deg }
    - topic: lens
      tags: [{ from: ., to: lens, values: [lens_a, lens_b] }]   # one row per lens
      paths:
        - { path: encoder, field: encoder_deg }
        - { path: target,  field: target_deg }
        - { path: moving,  field: moving, type: bool_as_int }
  ```

  Grafana dashboards per app, panels per topic, alerts on derived metrics (e.g. `iodine_temp < 50°C for 10m` → page oncall). Same dashboard pattern that already exists for the PLC/Galil hardhats, just sourced from the WS feed instead of bespoke pollers.

  Like the rsyslog proxy, the Cocoa apps and SPA stay unchanged — telemetry centralization is a downstream concern of the protocol.

- **Per-element accessibility.** Aria labels from outlet names + cell titles. Not addressed in v1.

## Open questions for v2 (not blockers for v1)

1. **`label_quartz` enum vs. free string.** Confirm during impl by reading [DCUcontroller.m](../../../dcu/src/DCU/DCUcontroller.m). If enum, prefer `value_map` for i18n later. If free string, `<output>` ←state.
2. **Whether the WS `hello` frame should also advertise the `manifest_url`** so a client without an app-specific config can self-configure. Today the SPA derives the manifest path from `hello.app`; explicit URL would let the server override (useful for staging deploys). Not required v1, low cost when added.
3. **Per-state glyph fidelity.** v1 renders DCU lamps and `view_in`/`view_eng` as plain CSS shapes (filled circle, ring, warning triangle). If operators ask for the original Cocoa glyphs back, the cost is a per-app `assets/` directory and an `awakeFromNib` PNG-export pass — the IR's `class_map` would gain a sibling `icon_map` and the state indicator gets an `<img src>` instead of a styled span. Not v1; revisit only on real demand.
4. **`Logger` level semantics across apps.** `Logger.message:level:` already exists, but the integer convention drifts across ADC / DCU / PFS call sites (one app's `level:2` may not mean what another's does). During impl, audit each app's call sites and pin the `int → debug/info/warn/error` mapping per app (or normalise to a single contract by editing the highest-level call sites). The bare `Logger.append:` form has no level — defaults to `info`. Until normalisation lands, the level filter in the SPA is a best-effort triage tool, not a strict severity gate.
5. **Whether `event` frames on `logs` should carry the topic name.** Today's design puts the entry directly in `data` (singular), matching the snake-case event convention used elsewhere. An alternative is `data: { entries: [...] }` for symmetry with the replay snapshot — trades wire bytes for SPA-side branching. Going with the asymmetric shape for v1; revisit if the SPA branching gets ugly.
