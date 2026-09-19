# Guider ROI by selection, and full-frame coordinates

Status: plan, 2026-09-18, revised the same day to put the knobs per client
in chz1. Two changes to the guider viewer (`viewer/guider.html` +
`viewer/app.js`), one addition to chz1 (astro-ph), one deletion in gcamweb
(zwo `src/web`).
Follows [guider-viewer-plan.md](guider-viewer-plan.md) § Decision 3, which
kept the `roi` centre-crop as the bandwidth lever; this replaces *how* the
region is chosen and fixes what the readout says while it is in force.

## What is wrong today

1. **The region is a fraction, not a place.** `roi = N` sends the central
   `1/N` of each side. A guide star that is not in the centre cannot be
   isolated: the operator either sends the full frame or loses the star.
   The zwo `MAX_ROI = 16`, `centre ½ / ¼` select is the whole interface —
   and it is shared, so one operator's crop is every viewer's crop.
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
| **served** — what this client receives: the ROI cut out of the camera frame, then binned, by chz1's per-client pipeline (`w × h` in the header) | the decoder, the renderer, `view`, `toImage`, `imexam`, `pixels[]` | viewer internals |
| **viewer crop** — `view.crop`, a display-only sub-rectangle of the served frame (`cropToView`, the panner's shadow) | the viewer package | nothing here changes it |

Conversion, served → camera, with `roi = {x0, y0}` and `bin` from the
frame header:

    X = x0 + col * bin        Y = y0 + row * bin        (continuous positions)

and DS9 display adds the usual `+0.5`. Today's header carries the rectangle
as `crop` (from gcamweb); after this plan it is `roi` (from chz1). The
readout reads whichever is present, so change 2 can ship first.

## Where the knobs live: per client, in chz1

Today `roi` and `every` are gcamweb source settings, shared by every
viewer of a guider, set over two HTTP routes the gateway proxies and
mirrored back through the status channel. chz1 already runs everything
*else* per connection: `ws_handler` clones `Settings` per client, builds a
`Pipeline` per client, takes a `config` message per client and tracks a
per-client `last_seq`. ROI and rate become two more fields of that shape,
and the shared machinery goes away. Decided 2026-09-18: less code, and the
two viewers of one guider stop re-cropping each other.

### chz1 (`packages/chz1/python/stream.py`, astro-ph)

- `Settings` gains `roi: tuple[int, int, int, int] | None = None` (camera
  pixels, `x0, y0, w, h`) and `every: int = 1`; `configure()` accepts both
  (`roi` as a 4-list or `null`, `every` clamped to `[1, 64]`).
- `_prepare()` slices `data[y0:y0+h, x0:x0+w]` before `bin_forward`, after
  clamping the rectangle to the frame, rounding `x0, y0` down and `w, h`
  up to multiples of 4 (so `bin 2`/`bin 4` never trim a remainder) and
  enforcing 16 px. The rectangle *as applied* goes in `wire` as
  `roi: {x0, y0, w, h}` and so into the header next to `bin`; `src_w` /
  `src_h` are already there. Clamping per frame means a rebin at gcam
  cannot leave a stale rectangle hanging off the edge.
- `next_message()` strides with the source's own seq: `source.get(
  self.last_seq + self.settings.every - 1)`. The seq-keyed `get` already
  waits for "newer than N"; no timer, no counter.
- `test_stream.py` gains three checks: a client with `roi` gets the
  rectangle (header `roi` and `w×h` as applied, an edge-hanging request
  clipped); a client with `every: 3` sees every third source seq while a
  plain client sees all; two clients with different rectangles decode
  different frames from the same source frame.
- Ships as the second commit of PR #1 or its own PR on top; `docs/stream.md`
  documents both fields under *Per-client operating points*.

### gcamweb (zwo `src/web/gcamweb`)

Deletion only. `set_every`, `set_roi`, `MAX_EVERY`/`MAX_ROI`, the crop
and `crop` header in `_parse`, the `every` gate in the pull loop, the
`/every` and `/roi` routes and the two status fields go. gcamweb parses
every frame gcam sends (astropy + one copy, ~10 ms, at 2–5 Hz) and
publishes it whole; every client cuts and strides its own. ~40 lines out.

### Gateway (`gateway.py`)

`mount_guiders()` stops proxying `every` and `roi`; the channel list is
`ws` and `status`. The health check is unchanged.

### Viewer (`viewer/app.js`, `guider.html`)

- The `SHARED` block, the `GET every` probe, the "needs a gcamweb with
  runtime setters" fallback and the status-mirroring loop go (~20 lines).
  `every` becomes a select that calls `stream.configure({ every })`, next
  to the tier select, which already calls `configure(TIERS[name])`; the
  ROI goes the same way.
- **Rename** the module variable `crop` to `roi`, read from `header.roi`,
  so `view.crop` (the viewer package's display crop) and the served
  rectangle can never be confused in this file again.
- **Selection gesture: `shift` + left drag on the canvas.** Unbound today:
  plain left drag pans, `ctrl`/middle/right drag is the colormap
  (`isColormapDrag`), the wheel zooms, and `shift` is only read by the
  cursor-step keys. The pan `pointerdown` handler gets one early branch:
  with `e.shiftKey`, start a rubber band. While dragging, the rectangle is
  drawn on the existing SVG overlay (`add("rect", …)`, class `roi-select`,
  the guide box's halo treatment) and the bar shows its size in camera
  pixels. On release past `DRAG_DEADZONE`, convert the two served-frame
  corners to camera pixels (conversion above) and
  `stream.configure({ roi: [x0, y0, w, h] })`; the next frame's header
  updates the strip. Escape mid-drag cancels.
- A drag can only refine what was served, so the way out is the **`full`
  button** on the strip (`configure({ roi: null })`), replacing the
  `centre ½ / ¼` select, beside a label with the rectangle in force
  (`ROI 250×250 at 375,375` or `full frame`).
- **The panner shows where the ROI sits**: the camera frame's extent
  (`src_w × src_h`) as the outer box, the served rectangle inside it, and
  the guide box from the cards — all known without pixels. *A drag on the
  panner to pick a region outside the served pixels reuses the same code
  and is a follow-on if `full → select` proves clumsy.*
- Rate text: `250×250 at 375,375 of 1000×1000, bin 2`.
- **The ROI and `every` may go in the URL again.** Decision 3 forbade
  `?roi=` only because the setting was shared; per client, a saved
  `/guider/pfs-sv/?roi=375,375,250,250&every=2` is one operator's own
  link. Read on load, sent with the opening `config`, and written back
  with `history.replaceState` when either changes, so the address bar is
  always a link to what is on screen.
- imageweb inherits both knobs for nothing (same handler); the quick-look
  page does not expose them for now.

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

1. **Readout in camera pixels** (change 2, lco-instrument-web only). Reads
   `header.roi ?? header.crop`; ships and deploys on its own.
   *Exit:* on `sbs`, with `roi 4 · bin 2`, the cursor at the centre of the
   drawn guide box reads `GDBOXX, GDBOXY` to within a pixel; at `full ·
   lossless` it reads what it did before.
2. **chz1 per-client `roi` and `every`** (astro-ph). *Exit:* the three new
   `test_stream.py` checks pass with the existing nine; `stream_host.py`
   with two browsers at different rectangles shows different frames.
3. **gcamweb deletion + gateway routes** (zwo PR, then lco-instrument-web).
   gcamweb's `pyproject` pins the chz1 ref that carries 2. *Exit:* status
   has no `roi`/`every`; frames arrive whole for a client that sends no
   `roi`; zwo v1.1.2 pre-release, lco-ansible bumps the install.
4. **Selection gesture, `full` button, panner context, URL params**
   (change 1 viewer side). Deploys together with 3 on `sbs`, the only
   deployment; no compatibility shim. *Exit:* shift-drag a box around the
   guide star → the strip shows the rectangle, the stream shrinks to it,
   the address bar carries it, a second browser on the same guider is
   unaffected; `full` restores everything; Escape mid-drag leaves the ROI
   alone; reload of the saved link comes up cropped.
5. **imexam coordinates** (astro-ph, optional). Upstream PR; the viewer
   adopts it by passing the same conversion.

## Not in this plan

- A shared ROI. The per-client one replaces it; if two operators want to
  see the same box they send the same link.
- Moving the ROI with the guide box automatically. gcam already keeps the
  star in its box; a region that followed it would be a bandwidth feature
  looking for a use.
- Rotations/flips of the served frame: `view.flip`/`rotation` act after
  `toImage`, so the conversion above is unaffected.
