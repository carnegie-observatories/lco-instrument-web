# Step 0 audit — ADC main expose window

Per [ws-migration-plan.md](ws-migration-plan.md) § Step 0. Audited 2026-05-07 against `/Users/william/workspace/adc/src/ADC/`.

## Window scope

The "main expose window" is `ADC_Controller`'s own window (`ADC_Controller` is itself an `NSWindowController`). The calibration window (separate `IBOutlet NSPanel *panel_calibrate` in `AppDelegate`, with `pop_lens`/`but_calibrate`/`but_plus`/`but_minus`/`but_stop`/`edit_phase`/`edit_angle`/`edit_sin`/`edit_cos` and friends) is engineering-only and **excluded** from this audit. Configuration (`window_config`) and Preferences (`panel_prefs`) panels are also excluded.

## Coverage matrix

| UI element | Type | Action method | Today's TCP | New `InstrumentService` method |
|---|---|---|---|---|
| `popup_adc` | popup, in/out | `action_slide:` (sender = `popup_adc`) → `moveSlide:SLIDE_ADC to:` | read via `status` | `setADCInsertion:position:error:` |
| `popup_other` | popup, in/out | `action_slide:` (sender = `popup_other`) → `moveSlide:SLIDE_OTHER to:` | read via `status` | `setOtherInsertion:position:error:` |
| `popup_control` | popup, auto/off | `action_control:` | read via `status` | `setControlMode:state:error:` |
| `chk_update` | checkbox, auto-update telescope | `action_telescope:` (sender = `chk_update`) | **gap** | `setAutoUpdate:enabled:error:` |
| `but_update` | button, manual update | `action_telescope:` (sender = `but_update`) | **gap** | `triggerManualUpdate:error:` |
| `but_run` | toggle button (Run/Stop combined) | `action_run:` | **gap** | `setRunning:state:error:` |
| `edit_encA`, `edit_encB` | text field (writable when ¬control) | `action_lens:` → `moveLens:lens to:angle` | read via `angles` | `moveLens:angle:error:` |
| `edit_targetA`, `edit_targetB` | text field, display-only | (computed by `updateLensTarget:`) | **gap** | derived state in `lens` topic |
| `edit_elevation`, `edit_rotator` | text field (DEBUG-only edit when ¬update) | `action_telescope:` (DEBUG branch) | **gap** | DEBUG-only override; **defer to v2** |
| `prog_adc`, `prog_lensA`, `prog_lensB`, `prog_other` | progress indicators | (read-only, `enableGUI:` toggles) | **gap** | `*_moving` flags in topics |
| `col_adcIn`, `col_adcOut`, `col_otherIn`, `col_otherOut` | color wells | (rendering only) | n/a | rendering — frontend concern |

## Topic snapshots

```
status     → {adc:"in"|"out"|"moving", control:"auto"|"off",
              other:"in"|"out"|"moving", update:bool, running:bool}
lens       → {lens_a:{encoder:float, target:float, moving:bool},
              lens_b:{encoder:float, target:float, moving:bool}}
telescope  → {elevation:float, rotator:float}   # from TCS, read-only
```

`version` is delivered in the `hello` frame; not a topic.

## Coverage delta vs. legacy TCP

Legacy TCP exposes 3 read paths: `version`, `status` (3 booleans: adc/control/other), `angles` (2 floats). The main window needs **7 write commands** plus **3 topics** to be a real equivalent. **Legacy TCP covers ~25% of operator-visible surface.**

Specifically missing from legacy TCP:

- All write actions (slide, control mode, auto-update, manual update, run/stop, lens move).
- `update_state` flag (read).
- Per-axis `_moving` flags (lens A, lens B, ADC slide, other slide).
- `target` values for both lenses (computed from elevation/rotator by `updateLensTarget:`).
- Telescope inputs (`elevation`, `rotator`) — readable via WS topic if useful, but only mutable in DEBUG.

## RUN_SERVER gate state

`#define RUN_SERVER` at `AppDelegate.m:13` — defined by default. Legacy handler is active in normal builds. **No ungating needed.**

## Files referenced

- [adc/src/ADC/AppDelegate.h](../../../adc/src/ADC/AppDelegate.h)
- [adc/src/ADC/AppDelegate.m:351-392](../../../adc/src/ADC/AppDelegate.m) — `tcpip_handler:` (3 cmds)
- [adc/src/ADC/ADC_Controller.h:49-88](../../../adc/src/ADC/ADC_Controller.h) — outlets, properties, IBActions
- [adc/src/ADC/ADC_Controller.m:414-636](../../../adc/src/ADC/ADC_Controller.m) — action method implementations
