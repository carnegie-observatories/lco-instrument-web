# xib2ir

One-shot converter that walks a Cocoa XIB main expose window and emits the IR
JSON consumed by the SPA's [renderer.js](../../renderer.js). Stdlib-only
(no external deps); see the IR schema in `ws-ui-conversion-plan.md`.

## Run

```sh
# from this directory:
python3 -m xib2ir extract /path/to/MainMenu.xib \
    --window <id-or-title> \
    --app <app> \
    -o ../../generated/<app>.layout.json
```

For the lead instruments:

```sh
python3 -m xib2ir extract /Users/william/workspace/adc/src/ADC/Base.lproj/MainMenu.xib \
    --window TNc-LT-7qW --app adc \
    -o ../../generated/adc.layout.json

python3 -m xib2ir extract /Users/william/workspace/dcu/src/DCU/Base.lproj/MainMenu.xib \
    --window 371 --app dcu \
    -o ../../generated/dcu.layout.json
```

Phase 4 of [ws-ui-conversion-plan.md](../../../lco-ansible/docs/plans/ws-ui-conversion-plan.md)
emits IR with `binding: null` everywhere — every element gets a frame, kind,
subkind, outlet, and a title_default but no read/write. Phase 5 adds
`bindings.yml` support so the converter populates the binding objects.

## Layout

| File          | Role                                                        |
|---------------|-------------------------------------------------------------|
| `parser.py`   | XML walking; outlet table; window lookup                    |
| `coords.py`   | Cocoa→top-left y-flip; NSBox content-inset accumulation     |
| `elements.py` | XIB tag → IR `(kind, subkind)`; title and popup-option pulls|
| `cli.py`      | argparse + glue: `walk` recursion, layout assembly, JSON I/O|
