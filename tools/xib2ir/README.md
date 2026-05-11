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
    --bindings ../../instruments/adc/bindings.yml \
    -o ../../generated/adc.layout.json

python3 -m xib2ir extract /Users/william/workspace/dcu/src/DCU/Base.lproj/MainMenu.xib \
    --window 371 --app dcu \
    --bindings ../../instruments/dcu/bindings.yml \
    -o ../../generated/dcu.layout.json
```

## Lint (drift check)

`lint` re-extracts and diffs against a committed JSON; exits non-zero on
drift. Designed for a future CI workflow but useful locally before
pushing to make sure `generated/*.layout.json` matches the XIB +
bindings.

```sh
python3 -m xib2ir lint /Users/william/workspace/adc/src/ADC/Base.lproj/MainMenu.xib \
    --window TNc-LT-7qW --app adc \
    --bindings ../../instruments/adc/bindings.yml \
    --expected ../../generated/adc.layout.json

python3 -m xib2ir lint /Users/william/workspace/dcu/src/DCU/Base.lproj/MainMenu.xib \
    --window 371 --app dcu \
    --bindings ../../instruments/dcu/bindings.yml \
    --expected ../../generated/dcu.layout.json
```

## Layout

| File          | Role                                                        |
|---------------|-------------------------------------------------------------|
| `parser.py`   | XML walking; outlet table; window lookup                    |
| `coords.py`   | Cocoa→top-left y-flip; NSBox content-inset accumulation     |
| `elements.py` | XIB tag → IR `(kind, subkind)`; title and popup-option pulls|
| `cli.py`      | argparse + glue: `walk` recursion, layout assembly, JSON I/O|
