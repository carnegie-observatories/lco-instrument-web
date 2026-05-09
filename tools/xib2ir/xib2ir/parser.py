"""XIB XML walking: find a window, build the outlet table.

Stays on stdlib's ElementTree — no external deps. The XIBs we ingest don't use
namespaces, so plain `find`/`findall` with @-attribute predicates is enough.
"""

import xml.etree.ElementTree as ET
from pathlib import Path


def parse(path: Path) -> ET.Element:
    """Parse an XIB file and return the document root."""
    return ET.parse(path).getroot()


def outlet_map(root: ET.Element) -> dict[str, str]:
    """Aggregate outlets across every <customObject> in the XIB.

    Returns ``{ destination_id: property_name }``. The "owning" controller for
    a given window isn't strictly needed — outlets are unique per destination
    id, and combining all controllers' outlets gives the converter a single
    lookup table.
    """
    m: dict[str, str] = {}
    for co in root.iter("customObject"):
        for outlet in co.iter("outlet"):
            prop = outlet.get("property")
            dest = outlet.get("destination")
            if prop and dest:
                m[dest] = prop
    return m


def find_window(root: ET.Element, key: str) -> ET.Element | None:
    """Locate a <window> by its XIB id or title (whichever the caller supplied)."""
    for w in root.iter("window"):
        if w.get("id") == key or w.get("title") == key:
            return w
    return None
