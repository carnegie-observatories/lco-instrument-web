"""Coordinate translation: Cocoa (bottom-left) → top-left, with NSBox content-inset.

The two non-obvious traps from the plan are encoded here so the renderer never
has to know about either:

1. Cocoa frames are bottom-left origin; HTML/CSS is top-left.
2. NSBox children are positioned relative to the box's *inner* contentView,
   not the outer frame; the outer frame includes a title-bar gap at its top.
   The converter accumulates that inset when descending so child frames in
   the IR are already in coordinates relative to the box's outer top-left.
"""


def flip_y(parent_height: float, y: float, h: float, inset_dy: float = 0.0) -> float:
    """Convert a Cocoa y to a top-left y in the parent's coordinate space.

    ``parent_height`` is the height of the *immediate* parent view in which
    the child is positioned (the inner contentView for NSBox children, the
    window contentView for top-level controls). ``inset_dy`` is added so the
    result is expressed relative to the *outer* parent (e.g., NSBox outer
    frame including title-bar gap), letting the renderer position children
    inside the box element with no further math.
    """
    return parent_height - y - h + inset_dy


def box_inset_dy(outer_h: float, inner_h: float) -> int:
    """Title-bar gap on an NSBox: outer height minus inner contentView height."""
    return int(round(outer_h - inner_h))
