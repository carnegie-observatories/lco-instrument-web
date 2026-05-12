"""xib2ir CLI: walk a chosen XIB window and emit an IR JSON document.

  python -m xib2ir extract <xib> --window <id-or-title> --app <app> [-o <out>]

Phase 4: emits IR with no bindings (binding: null on every element). Phase 5
will layer bindings.yml on top via xib2ir/bindings.py.
"""

import argparse
import difflib
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from . import __version__, coords, elements, parser as xib_parser


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="xib2ir",
        description="Convert a Cocoa XIB main expose window into renderer IR JSON.",
    )
    ap.add_argument("--version", action="version", version=f"xib2ir {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("extract", help="Walk an XIB and emit IR JSON.")
    e.add_argument("xib", help="Path to the .xib file")
    e.add_argument("--window", required=True, help="Window id or title")
    e.add_argument("--app", required=True, help="App identifier for the IR's \"app\" field")
    e.add_argument("--bindings", help="Optional bindings.yml to populate binding objects")
    e.add_argument("-o", "--output", default="-", help="Output path (default: stdout)")
    e.set_defaults(func=cmd_extract)

    ln = sub.add_parser(
        "lint",
        help="Re-extract an XIB and assert no drift against a committed IR JSON.",
    )
    ln.add_argument("xib", help="Path to the .xib file")
    ln.add_argument("--window", required=True, help="Window id or title")
    ln.add_argument("--app", required=True, help="App identifier")
    ln.add_argument("--bindings", required=True, help="bindings.yml to apply")
    ln.add_argument("--expected", required=True, help="Committed IR JSON to compare against")
    ln.set_defaults(func=cmd_lint)

    args = ap.parse_args(argv)
    return args.func(args)


def cmd_extract(args: argparse.Namespace) -> int:
    src = Path(args.xib)
    if not src.exists():
        print(f"xib2ir: XIB not found: {src}", file=sys.stderr)
        return 2

    root = xib_parser.parse(src)
    outlets = xib_parser.outlet_map(root)
    window = xib_parser.find_window(root, args.window)
    if window is None:
        print(f"xib2ir: window not found: {args.window!r}", file=sys.stderr)
        return 2

    layout = build_layout(window, outlets, args.app)

    if args.bindings:
        from . import bindings as bindings_mod
        bindings_path = Path(args.bindings)
        if not bindings_path.exists():
            print(f"xib2ir: bindings file not found: {bindings_path}", file=sys.stderr)
            return 2
        spec = bindings_mod.load(bindings_path)
        layout["warnings"].extend(bindings_mod.apply(layout, spec))

    text = json.dumps(layout, indent=2) + "\n"
    if args.output == "-":
        sys.stdout.write(text)
    else:
        Path(args.output).write_text(text)
        print(
            f"xib2ir: wrote {args.output} — "
            f"{len(layout['elements'])} elements, {len(layout['warnings'])} warnings",
            file=sys.stderr,
        )
    return 0


def cmd_lint(args: argparse.Namespace) -> int:
    """Re-run extract and diff against the committed JSON. Exit 0 on match,
    1 on drift. Designed for a future GitHub Action: the same invocation
    runs locally before pushing."""
    src = Path(args.xib)
    expected_path = Path(args.expected)
    bindings_path = Path(args.bindings)
    for p, label in [(src, "XIB"), (expected_path, "expected JSON"), (bindings_path, "bindings")]:
        if not p.exists():
            print(f"xib2ir lint: {label} not found: {p}", file=sys.stderr)
            return 2

    root = xib_parser.parse(src)
    outlets = xib_parser.outlet_map(root)
    window = xib_parser.find_window(root, args.window)
    if window is None:
        print(f"xib2ir lint: window not found: {args.window!r}", file=sys.stderr)
        return 2

    layout = build_layout(window, outlets, args.app)
    from . import bindings as bindings_mod
    spec = bindings_mod.load(bindings_path)
    layout["warnings"].extend(bindings_mod.apply(layout, spec))

    fresh = json.dumps(layout, indent=2) + "\n"
    committed = expected_path.read_text()
    if fresh == committed:
        print(f"xib2ir lint: OK — {args.app} matches {expected_path}", file=sys.stderr)
        return 0

    diff = difflib.unified_diff(
        committed.splitlines(keepends=True),
        fresh.splitlines(keepends=True),
        fromfile=f"{expected_path} (committed)",
        tofile=f"{args.xib} (fresh)",
        n=3,
    )
    sys.stderr.write("xib2ir lint: drift detected — committed JSON is stale.\n")
    sys.stderr.write("Run `python3 -m xib2ir extract ...` to regenerate.\n\n")
    sys.stdout.writelines(diff)
    return 1


def build_layout(window: ET.Element, outlets: dict[str, str], app: str) -> dict:
    cv = window.find("./view[@key='contentView']")
    if cv is None:
        raise RuntimeError("window has no contentView")
    rect = cv.find("./rect[@key='frame']")
    if rect is None:
        raise RuntimeError("window contentView has no frame")
    win_w = float(rect.get("width", 0))
    win_h = float(rect.get("height", 0))

    elements_out: list[dict] = []
    warnings: list[dict] = []
    walk(
        cv,
        parent_id=None,
        parent_h=win_h,
        inset_dy=0,
        outlets=outlets,
        out=elements_out,
        warnings=warnings,
    )

    return {
        "_generator": f"xib2ir@{__version__}",
        "app": app,
        "window_id": window.get("id"),
        "window": {
            "title": window.get("title", ""),
            "width": int(round(win_w)),
            "height": int(round(win_h)),
        },
        "elements": elements_out,
        "warnings": warnings,
    }


def walk(
    parent_view: ET.Element,
    parent_id: str | None,
    parent_h: float,
    inset_dy: float,
    outlets: dict[str, str],
    out: list[dict],
    warnings: list[dict],
) -> None:
    """Depth-first walk of a view's <subviews>, emitting IR elements as we go."""
    subviews = parent_view.find("./subviews")
    if subviews is None:
        return
    for child in subviews:
        if not isinstance(child.tag, str):
            continue  # comment / processing instruction
        rect = child.find("./rect[@key='frame']")
        if rect is None:
            continue

        x = float(rect.get("x", 0))
        y = float(rect.get("y", 0))
        w = float(rect.get("width", 0))
        h = float(rect.get("height", 0))
        ir_y = coords.flip_y(parent_h, y, h, inset_dy)

        outlet = outlets.get(child.get("id"))
        kind, subkind = elements.classify(child, outlet)

        ir: dict = {
            "id": child.get("id"),
            "parent_id": parent_id,
            "kind": kind,
        }
        if subkind:
            ir["subkind"] = subkind
        if outlet:
            ir["outlet"] = outlet
        ir["frame"] = {
            "x": int(round(x)),
            "y": int(round(ir_y)),
            "w": int(round(w)),
            "h": int(round(h)),
        }

        title = elements.extract_title(child)

        custom_class = child.get("customClass")
        if custom_class:
            ir["custom_class"] = custom_class
            warnings.append({
                "kind": "custom_class",
                "id": child.get("id"),
                "outlet": outlet,
                "custom_class": custom_class,
                "base_class": child.tag,
            })

        if kind == "box":
            if title:
                ir["title"] = title
            inner = child.find("./view[@key='contentView']")
            if inner is not None:
                inner_rect = inner.find("./rect[@key='frame']")
                if inner_rect is not None:
                    inner_h = float(inner_rect.get("height", 0))
                    dy = coords.box_inset_dy(h, inner_h)
                    ir["content_inset"] = {"dx": 0, "dy": dy}
                    out.append(ir)
                    walk(inner, child.get("id"), inner_h, dy, outlets, out, warnings)
                    continue
            # Box with no inner contentView (e.g., separator boxType) — emit as-is.
            ir["binding"] = None
            out.append(ir)
            continue

        if title:
            ir["title_default"] = title

        if kind == "popup":
            opts = elements.extract_popup_options(child)
            if opts:
                ir["options"] = opts

        bounds = elements.extract_bounds(child)
        if bounds is not None:
            ir["bounds"] = bounds

        ir["binding"] = None
        out.append(ir)
