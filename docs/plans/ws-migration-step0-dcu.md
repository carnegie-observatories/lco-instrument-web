# Step 0 audit — DCU main expose window

Per [ws-migration-plan.md](ws-migration-plan.md) § Step 0. Audited 2026-05-07 against `/Users/william/workspace/dcu/src/DCU/`.

## Window scope

The "main expose window" is `DCUcontroller`'s window (the `dcuWindow` IBOutlet on `AppDelegate`). The preferences/setup window (`window` IBOutlet on `AppDelegate`, with `popup_telname`/`combo_dcuname`/`popup_telstat`/`popup_dcustat`/`check_engmode`/`check_ffs`/`check_mcal`) is engineering/setup and **excluded**.

## Coverage matrix

| UI element | Type | Action method | Today's TCP | New `InstrumentService` method |
|---|---|---|---|---|
| `but_lamp1` … `but_lamp8` | 8 toggle buttons | `action_lamp:` | summary read via `ffsLamps` | `setLamp:index:on:error:` (per-lamp) |
| `popup_quartz` | popup, quartz lamp level | `action_lamp:` | **gap** | `setQuartzLevel:level:error:` |
| `edit_quartz` | text field, quartz value | `action_lamp:` (via FFvarLamp) | **gap** | `setQuartzValue:value:error:` |
| `drop_ffs` | popup, FFS position | `action_ffs:` | **gap** (no setter) | `setFFSPosition:position:error:` |
| `popup_ffbrake` | popup, FFS brake | `action_ffs:` | **gap** | `setFFSBrake:state:error:` |
| `check_ffins`, `check_ffret`, `check_ffbrake` | checkboxes, FFS indicators | (display-only, KVO via FFscreen) | partial via `ffsState` | included in `ffs` topic |
| `prog_screen` | progress, FFS motion | (display-only) | **gap** | `moving` flag in `ffs` topic |
| `drop_mcal` | popup, Mcal position | `action_mcal:` | **gap** (no `mcalState` cmd at all) | `setMcalPosition:position:error:` |
| `popup_mcbrake` | popup, Mcal brake | `action_mcal:` | **gap** | `setMcalBrake:state:error:` |
| `check_mcins`, `check_mcret`, `check_mcbrake` | checkboxes, Mcal indicators | (display-only) | **gap** | included in `mcal` topic |
| `prog_mcal` | progress, Mcal motion | (display-only) | **gap** | `moving` flag in `mcal` topic |
| `edit_air` | text field, air pressure | (display-only, FFsensor → KVO) | **gap** | included in `pressure` topic |
| `view_in` | status icon | (rendering only) | n/a | rendering — frontend concern |

## Topic snapshots

```
status     → {running:bool, deployed:bool, error_state:int}
lamps      → {lamps:[{idx:1, on:bool, err:bool}, …(8)],
              quartz:{level:int, value:float}}
ffs        → {position:"in"|"out"|"moving", brake:"on"|"off",
              inserted:bool, retracted:bool}
mcal       → {position:"in"|"out"|"moving", brake:"on"|"off",
              inserted:bool, retracted:bool}
pressure   → {air:float, min:float, ok:bool}
```

`version` is delivered in the `hello` frame; not a topic.

## Coverage delta vs. legacy TCP

Legacy TCP exposes 3 read paths: `version`, `ffsState`, `ffsLamps`. Both `ffsState` and `ffsLamps` are *read-only* getters (verified at `AppDelegate.m:347-358` — the handler calls `[dcuController ffsState]` / `[dcuController ffsLamps]` and returns the string; no setters). The main window needs **7 write commands** plus **5 topics** to be a real equivalent. **Legacy TCP covers <15% of operator-visible surface.**

Most prominent gaps:

- **Mcal is entirely invisible to scripting.** No `mcalState`, no `mcalLamps`, no setter. The whole monocular calibration subsystem is operator-only today.
- No per-lamp control (`ffsLamps` is a summary string; can't toggle a single lamp).
- No quartz variable lamp (level or value).
- No FFS or Mcal setters (insert/retract, brake on/off).
- No air pressure read.
- No error-state read.

## RUN_SERVER gate state

`#define RUN_SERVER` at `AppDelegate.m:13` — defined by default. Legacy handler is active in normal builds. **No ungating needed.**

## Files referenced

- [dcu/src/DCU/AppDelegate.h](../../../dcu/src/DCU/AppDelegate.h)
- [dcu/src/DCU/AppDelegate.m:329-367](../../../dcu/src/DCU/AppDelegate.m) — `tcpip_handler:` (3 cmds, all read-only)
- [dcu/src/DCU/DCUcontroller.h:34-118](../../../dcu/src/DCU/DCUcontroller.h) — outlets, properties, IBActions
- [dcu/src/DCU/DCUcontroller.m](../../../dcu/src/DCU/DCUcontroller.m) — action method implementations
