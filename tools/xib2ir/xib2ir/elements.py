"""XIB element → IR (kind, subkind) mapping table.

Encodes the rules from the plan's element-type mapping section. Subkind carries
cell-style nuance (push / check / bevel-toggle / pulldown / readout / etc.).
"""

import xml.etree.ElementTree as ET


def classify(el: ET.Element, outlet: str | None) -> tuple[str, str | None]:
    """Map a single XIB subview element to (kind, subkind) for the IR."""
    tag = el.tag

    if tag == "box":
        if el.get("boxType") == "separator":
            return ("separator", None)
        return ("box", None)

    if tag == "button":
        cell = el.find("./buttonCell")
        cell_type = cell.get("type") if cell is not None else "push"
        # `enabled="NO"` lives on the buttonCell in these XIBs, not the outer
        # button — checking the outer would always return YES.
        cell_enabled = cell.get("enabled") if cell is not None else None
        enabled = cell_enabled if cell_enabled is not None else el.get("enabled", "YES")
        # Disabled radio buttons are pure status indicators in these XIBs.
        if cell_type == "radio" and enabled == "NO":
            return ("indicator", "radio")
        if cell_type == "check":
            return ("button", "check")
        if cell_type == "bevel":
            # Bevel buttons (DCU lamps) carry their state via icon swaps in
            # Cocoa; the web IR encodes that as a CSS-class toggle.
            return ("button", "bevel-toggle")
        return ("button", "push")

    if tag == "popUpButton":
        cell = el.find("./popUpButtonCell")
        if cell is not None and cell.get("pullsDown") == "YES":
            return ("popup", "pulldown")
        return ("popup", "popup")

    if tag == "textField":
        cell = el.find("./textFieldCell")
        border = cell.get("borderStyle") if cell is not None else None
        editable = cell.get("editable") if cell is not None else None
        state = cell.get("state") if cell is not None else None
        # Cocoa "editable" textField = web input.
        if border == "bezel" and editable == "YES":
            return ("textfield", "input")
        # "Bordered" non-editable readout = web <output>.
        if border == "border" and state == "on":
            return ("textfield", "readout")
        # Anything else (no border, no state) is a decorative label.
        return ("label", None)

    if tag == "progressIndicator":
        if el.get("style") == "spinning":
            return ("progress", "spinner")
        return ("progress", "bar")

    if tag == "levelIndicator":
        return ("levelindicator", None)

    if tag == "colorWell":
        return ("indicator", "swatch")

    if tag == "imageView":
        return ("imageview", None)

    return (tag, None)


def extract_title(el: ET.Element) -> str | None:
    """Pull the display title from the element.

    Two storage conventions in XIBs:
      * ``<box>`` carries its legend directly: ``<box title="Telescope">``.
      * Most other controls stash it under a *Cell child:
        ``<textField><textFieldCell title="Elevation"/></textField>``.

    Trailing whitespace ("00.00 ", "+000.00 ") is stripped — the XIB pads
    these for visual room in IB but the renderer handles its own padding.
    """
    if el.tag == "box":
        t = el.get("title")
        if t is not None:
            stripped = t.rstrip()
            return stripped if stripped else None
        return None
    for child in el:
        if isinstance(child.tag, str) and child.tag.endswith("Cell"):
            t = child.get("title")
            if t is not None:
                stripped = t.rstrip()
                return stripped if stripped else None
    return None


def _coerce_num(s: str) -> float | int:
    """Numeric strings round-trip as int when integral, else float — keeps the
    IR free of stray ``.0`` suffixes on whole-number bounds like ``max=100``."""
    try:
        f = float(s)
        i = int(f)
        return i if i == f else f
    except ValueError:
        return s  # leave non-numeric attributes as-is


def extract_bounds(el: ET.Element) -> dict | None:
    """Numeric range / threshold values for determinate progress bars and
    level indicators.

    Returns ``None`` (so the caller omits the field entirely) for elements
    with no meaningful bounds — indeterminate progress spinners and everything
    that isn't a progress / level indicator. That keeps the schema *opt-in*:
    pre-PFS XIBs (ADC, DCU) only contain spinning progress indicators, so
    their committed IR JSON is unaffected by these additions.

      progress (style != "spinning")  →  {min, max}
      levelIndicator                  →  {min, max, warning, critical, style}
    """
    if el.tag == "progressIndicator":
        if el.get("style") == "spinning":
            return None
        out = {}
        for attr, key in [("minValue", "min"), ("maxValue", "max")]:
            v = el.get(attr)
            if v is not None:
                out[key] = _coerce_num(v)
        return out or None

    if el.tag == "levelIndicator":
        cell = el.find("./levelIndicatorCell")
        if cell is None:
            return None
        out = {}
        for attr, key in [
            ("minValue", "min"),
            ("maxValue", "max"),
            ("warningValue", "warning"),
            ("criticalValue", "critical"),
        ]:
            v = cell.get(attr)
            if v is not None:
                out[key] = _coerce_num(v)
        style = cell.get("levelIndicatorStyle")
        if style is not None:
            out["style"] = style
        return out or None

    return None


def extract_columns(el: ET.Element) -> list[dict] | None:
    """Pull a tableView's column descriptors from <tableColumns><tableColumn>.

    Returns a list of ``{id, title, width}`` dicts. ``id`` is the XIB's
    ``identifier`` attribute (typically a small int as string — the
    CalibrationController uses these as ``enum column_ids`` indices),
    ``title`` comes from the column's ``<tableHeaderCell title>``, and
    ``width`` is the XIB's pixel width rounded to int.

    The column→record-field path mapping is NOT extracted here — it
    lives in bindings.yml as ``read.column_paths`` (positional list).
    Keeping it out of the IR lets the same xib2ir output drive
    multiple bindings (e.g. a development fixture vs. the real
    calibration topic) without re-running the converter.
    """
    cols_root = el.find("./tableColumns")
    if cols_root is None:
        return None
    out: list[dict] = []
    for col in cols_root.findall("./tableColumn"):
        d: dict = {"id": col.get("identifier", "")}
        header = col.find("./tableHeaderCell")
        if header is not None:
            t = header.get("title")
            if t is not None:
                d["title"] = t
        width = col.get("width")
        if width is not None:
            d["width"] = int(round(float(width)))
        out.append(d)
    return out or None


def extract_popup_options(el: ET.Element) -> list[dict] | None:
    """Pull a popup's option list from <popUpButtonCell><menu><items><menuItem>.

    Returns a list of ``{value, label}`` dicts (value == label since the XIB
    only carries the display title). bindings.yml in a later phase will be
    where wire-value-vs-display divergence is encoded.
    """
    cell = el.find("./popUpButtonCell")
    if cell is None:
        return None
    menu = cell.find("./menu")
    if menu is None:
        return None
    items = menu.find("./items")
    if items is None:
        return None
    opts = []
    for item in items.findall("./menuItem"):
        title = item.get("title", "")
        opts.append({"value": title, "label": title})
    return opts or None
