# Guider ROI by selection, and full-frame coordinates

Status: plan, 2026-09-18. Two changes to the guider viewer
(`viewer/guider.html` + `viewer/app.js`) and to gcamweb (zwo `src/web`).
Follows [guider-viewer-plan.md](guider-viewer-plan.md) § Decision 3, which
kept the `roi` centre-crop as the bandwidth lever; this replaces *how* the
region is chosen and fixes what the readout says while it is in force.

## What is wrong today

1. **The region is a fraction, not a place.** `roi = N` sends the central
   `1/N` of each side. A guide star that is not in the centre cannot be
   isolated: the operator either sends the full frame or loses the star.
   The zwo `MAX_ROI = 16`, `centre ½ / ¼` select is the whole interface.
2. **The readout lies while cropped.** The bar's `x, y` are the served
   frame's pixel coordinates: with `roi 4, bin 2` on a 1000×1000 guider the
   cursor over the guide box reads `62.5, 62.5` while the panel's `GDBOXX`
   says `500.0`. The guide box itself is drawn right (`drawGuideBox()`
   subtracts `crop.x0/y0` and divides by `bin`), so the picture is correct
   and only the numbers are in the wrong frame. Same on the quick-look at
   `bin 4`: the readout shows binned pixels, the FITS cards do not.

## Frames of reference, once

Three pixel frames exist and the code must name them:

| frame | origin | who uses it |
|---|---|---|
| **camera** — gcam's full, unbinned frame (`NAXIS1 × NAXIS2`) | `GDBOXX/Y`, `GDDX/Y`, every card in the panel, the ROI setting, the readout after this plan | operator |
| **served** — what gcamweb sends: the ROI cut out of the camera frame, then binned by chz1 (`w × h` in the header) | the decoder, the renderer, `view`, `toImage`, `imexam`, `pixels[]` | viewer internals |
| **viewer crop** — `view.crop`, a display-only sub-rectangle of the served frame (`cropToView`, the panner's shadow) | the viewer package | nothing here changes it |

Conversion, served → camera, with `roi = {x0, y0}` from the header's
`crop` and `bin` from the header:

    X = x0 + col * bin        Y = y0 + row * bin        (continuous positions)

and DS9 display adds the usual `+0.5`. The header's `crop` already carries
`x0, y0, w, h, src_w, src_h`; nothing new has to cross the wire for change 2.

## Change 1 — the ROI is a rectangle the operator drags

### gcamweb (zwo `src/web/gcamweb`)

`GcamSource.roi` becomes a rectangle in camera pixels, or `None` for the
full frame:

    roi: dict | None = None          # {"x0", "y0", "w", "h"} in camera px

- `POST /guider/<name>/roi?x0=&y0=&w=&h=` sets it; `POST …/roi` with no
  parameters (or `?full=1`) clears it; `?n=N` keeps working as "centre
  1/N" for one release so the current page keeps working during the
  rollout. `GET` returns `{"roi": {…} | null}`. Bad or missing integers →
  400, as now.
- **Clamp and round at parse time, not at set time.** gcamweb does not
  know the frame size until a frame arrives, and gcam can change it
  (binning) under a running setting. `_parse` clips the rectangle to
  `[0, src_w) × [0, src_h)`, rounds `x0, y0` down and `w, h` up to
  multiples of 4 (so chz1's `bin 2`/`bin 4` never has to trim a
  remainder), and enforces a 16 px minimum. The header's `crop` reports
  the rectangle *as applied*, which is what the viewer draws and offsets
  by; `status.roi` reports the rectangle *as requested*.
- Header `crop` loses `n` (the viewer's rate text is the only reader);
  `status` gains `roi` as the object. `MAX_ROI` goes.
- Still **source-level and shared** by every viewer of the guider, for the
  same reason as before (the crop happens before encode; a per-client crop
  is a chz1 `config` extension, not a gcamweb change). The status channel
  carries the rectangle at 1 Hz, so a second viewer sees the ROI change
  within a second.
- `test_gcam.py` (or a new one): centre `n=4` on 1000×1000 → `{375, 375,
  250, 250}`; a rectangle hanging off the edge is clipped; odd sizes are
  rounded to ×4; a request smaller than 16 px grows to 16.

### Viewer (`viewer/app.js`, `guider.html`)

- **Rename** the module variable `crop` to `roi` and the header field read
  to `header.crop` → `roi`, so `view.crop` (the viewer package's display
  crop) and the served rectangle can never be confused in this file again.
- **Selection gesture: `shift` + left drag on the canvas.** Unbound today:
  plain left drag pans, `ctrl`/middle/right drag is the colormap
  (`isColormapDrag`), the wheel zooms, and `shift` is only read by the
  cursor-step keys. The `pointerdown` handler that pans gets one early
  branch: with `e.shiftKey`, start a rubber band instead. While dragging,
  the rectangle is drawn on the existing SVG overlay (`add("rect", …)`,
  class `roi-select`, same halo treatment as the guide box); the readout
  bar shows its size in camera pixels. On release, if the box exceeds
  `DRAG_DEADZONE`, convert its two served-frame corners to camera pixels
  with the conversion above and `POST …/roi?x0=&y0=&w=&h=`; the response
  (or the next status frame) updates the strip. Escape during the drag
  cancels.
- Because the operator can only drag over pixels that were served, a
  selection while an ROI is in force is a *sub*-ROI. Getting back out is
  the **`full` button** on the strip, which replaces the `<select id="roi">`
  and its `centre ½ / ¼` options: one button, plus a label with the
  rectangle in force (`ROI 250×250 at 375,375` or `full frame`). `every`
  keeps its select; the `SHARED` mirroring loop special-cases `roi` (it
  fills the label, not a select value).
- **The panner shows where the ROI sits.** Today the panner draws only the
  served frame. Draw the camera frame's extent (`src_w × src_h` from the
  header) as the panner's outer box, the served ROI at its place inside it,
  and the guide box from the cards — all known without pixels. This is
  what tells the operator "you are looking at the lower-left quarter" at a
  glance, and it is display geometry only. *Dragging a new ROI on the
  panner, outside the served pixels, is possible with the same code and is
  left as a follow-on if `full → select` proves clumsy in use.*
- Rate text: `250×250 at 375,375 of 1000×1000, bin 2` instead of
  `centre 1/4 of 1000×1000, bin 2`.
- Keep the URL free of the ROI (Decision 3's reasoning stands: a saved
  link must not re-crop everyone's stream).

## Change 2 — the readout counts camera pixels

- `updateBar()` (`viewer/app.js` ~L395): after `toImage(...)` gives
  served `col, row`, convert with `X = roi.x0 + col * bin`,
  `Y = roi.y0 + row * bin`, then `toDs9`. The pixel *value* stays what it
  is (the served, binned sample). The seated cursor (`cursorMode`, `pos`)
  goes through the same line.
- The bar gets a short frame label next to `x y` — `cam` — with a title
  saying "camera pixels, full frame, unbinned", so the coordinates and the
  panel's `GDBOXX/Y` are visibly the same thing. On the quick-look the same
  conversion applies with `roi = {0, 0}` and the label reads `full`; at
  `bin 4` the readout stops showing binned pixels there too. This is a
  behaviour change for the quick-look as well; it is the right one (the
  FITS cards and `fits_path` are the operator's reference, and both are in
  unbinned pixels), and it is one code path.
- **imexam's numbers stay in served pixels for now.** The inspector
  popover (`@astro-ph-labs/viewer/panels/inspector.ts`) formats its own
  hover/centroid coordinates via `indexToDs9`, in package code. Making it
  offset-aware is a small astro-ph change (an optional `toDs9` on the
  inspector's state); it goes as an upstream PR after this lands, and until
  then the popover's coordinates are labelled `served` so the two readouts
  cannot be mistaken for each other. The cut/aperture *drawings* are in
  served coordinates by construction and are right.
- Nothing changes in the guide box code: it already lives in camera pixels
  and converts the other way.

## Staging

1. **Readout in camera pixels** (change 2, lco-instrument-web only). Uses
   the header fields that already exist; ships and deploys on its own.
   *Exit:* on `sbs`, with `roi 4 · bin 2`, the cursor at the centre of the
   drawn guide box reads `GDBOXX, GDBOXY` to within a pixel; at `full ·
   lossless` it reads what it did before.
2. **gcamweb rectangle ROI** (zwo PR, after #35). Keeps `?n=`, so the
   current page keeps working. *Exit:* `POST …/roi?x0=100&y0=200&w=300&h=300`
   → header `crop` `{100, 200, 300, 300, …}`, status `roi` the same, frames
   300×300 at `lossless`; `POST …/roi` → `null`, full frames; the tests
   above pass. Release as zwo v1.1.2 (pre-release, CI attaches the assets)
   and bump the gcamweb install in lco-ansible.
3. **Selection gesture, `full` button, panner context** (change 1 viewer
   side). Requires 2 on the host; `sbs` is the only deployment, so the two
   deploy together and no compatibility shim is written. *Exit:* shift-drag
   a box around the guide star → the strip shows the rectangle, the stream
   shrinks to it, a second browser on the same guider follows within 1 s,
   the panner shows the box inside the full frame; `full` restores
   everything; Escape mid-drag leaves the ROI alone.
4. **imexam coordinates** (astro-ph, optional). Upstream PR; the viewer
   adopts it by passing the same conversion.

## Not in this plan

- Per-client ROI (a chz1 `config` extension) — Decision 3's reasoning
  still holds: one guider, one operator, one region.
- Moving the ROI with the guide box automatically. gcam already keeps the
  star in its box; a region that followed it would be a bandwidth feature
  looking for a use.
- Rotations/flips of the served frame: `view.flip`/`rotation` act after
  `toImage`, so the conversion above is unaffected.
