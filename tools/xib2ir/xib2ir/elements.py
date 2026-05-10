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
