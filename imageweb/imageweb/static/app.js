// instrument quick look — the full viewer, assembled from @astro-ph-labs/{core,viewer} and @astro-ph-labs/chz1.
//
// This is gcamweb's viewer assembly (zwo/src/web/gcamweb/static/app.js) with the guider layer
// replaced by the header panel, and the stream gated on visibility: the frame WS exists only
// while this page is being looked at (standalone, or embedded as the SPA's Quick Look tab).
// Everything that *decides* something about pixels — the view transform, imexam, limits, the
// renderer — is the package's. What is here is wiring. Design and the "display verbatim,
// derive nothing" rule: docs/plans/image-viewer-plan.md.
//
// No worker and no build step: `createRenderer` drives a main-thread WebGPU canvas, decoding runs
// in chz1's pooled workers, and an import map resolves the packages off the bridge's mounts.

import * as A from "@astro-ph-labs/core/analysis.js";
import * as Cur from "@astro-ph-labs/core/cursor.js";
import * as M from "@astro-ph-labs/core/measure.js";
import * as Ov from "@astro-ph-labs/core/overlay/index.js";
import * as S from "@astro-ph-labs/core/surface.js";
import { COLORMAPS } from "@astro-ph-labs/core/colormaps.js";
import { GpuError, createRenderer } from "@astro-ph-labs/viewer/render/renderer.js";
import { SHAPE, mountHistogram, mountInspector } from "@astro-ph-labs/viewer/panels/inspector.js";
import { VIEWER_BINDINGS, bindingsFor } from "@astro-ph-labs/viewer/keymap.js";
import { appendShapes, svgEl } from "@astro-ph-labs/core/dom/svg.js";
import { clampToViewport, edgeToward, placeBeside } from "@astro-ph-labs/core/dom/place.js";
import { createImexam } from "@astro-ph-labs/core/imexam.js";
import { fromHistogram } from "@astro-ph-labs/core/limits.js";
import { mountBar } from "@astro-ph-labs/viewer/panels/bar.js";
import { mountControls } from "@astro-ph-labs/viewer/panels/controls.js";
import { mountHelp } from "@astro-ph-labs/viewer/panels/help.js";
import { mountViewerChrome } from "@astro-ph-labs/viewer/panels/chrome.js";
import {
  DRAG_DEADZONE, clampSensitivity, contrastBiasAt, devicePixel, isColormapDrag, wheelFactor,
} from "@astro-ph-labs/core/pointer.js";
import {
  centreOn, createView, cropRect, cropToView, fitView, magnifierFloored, magnifierZoom, panBy,
  pannerView, screenAngle, setCrop, setFlip, setRotation, toDevice, toDisplayedIndex, toDs9,
  toImage, zoomAt, zoomLabel,
} from "@astro-ph-labs/core/view.js";
import { Chz1Stream } from "@astro-ph-labs/chz1";
import { loadWasm } from "@astro-ph-labs/chz1/wasm-loader.js";
import { mountHeaderPanel } from "./header-panel.js";

// -- the furniture ---------------------------------------------------------------------------------

mountViewerChrome(document.body);

const $ = (id) => document.getElementById(id);
// Every URL is relative to this page's own directory (<prefix>/<gnum>/): the bridge decides the
// prefix, the page never sees it. `here` drops the query string; `wsUrl` keeps the page's scheme,
// so a page served over https (a tunnel, a proxy) opens wss and is not blocked as mixed content.
const here = new URL(".", location.href);
const url = (path) => new URL(path, here);
const wsUrl = (path) => {
  const u = url(path);
  u.protocol = location.protocol === "https:" ? "wss:" : "ws:";
  return u;
};
// The instrument's name is this page's directory: <prefix>/pfs/ -> "pfs".
const NAME = here.pathname.split("/").filter(Boolean).pop() ?? "";
const canvas = $("view");
const overlay = $("overlay");
const controlsEl = $("controls");
const histEl = $("histpanel");
const helpEl = $("help");
const inspectorEl = $("inspector");
const surfaceCanvas = $("surface");
const crosshair = $("crosshair");
const pannerCanvas = $("panner");
const magCanvas = $("mag");
const pannerRect = $("panner-rect");
const pannerCrop = $("panner-crop");
const magBox = $("mag-box");
const paneEls = { panner: $("pane-panner"), magnifier: $("pane-mag") };
const hdrEl = $("hdr");

const writeBar = mountBar($("bar"));
const updateHdr = mountHeaderPanel(hdrEl);

function fatal(msg) {
  $("fatal-msg").textContent = `${msg} — this viewer needs a WebGPU-capable browser.`;
  $("fatal").hidden = false;
  throw new Error(msg);
}

const dpr = () => self.devicePixelRatio || 1;
const sizeOf = () => ({
  w: Math.max(1, Math.round(canvas.clientWidth * dpr())),
  h: Math.max(1, Math.round(canvas.clientHeight * dpr())),
});
const PANE_CSS = 128;
const paneSize = () => ({ w: Math.round(PANE_CSS * dpr()), h: Math.round(PANE_CSS * dpr()) });
const surfaceSize = () => ({ w: Math.round(338 * dpr()), h: Math.round(240 * dpr()) });

// -- the device ---------------------------------------------------------------------------------------

let renderer = null;
try {
  renderer = await createRenderer(canvas);
  renderer.resize(sizeOf().w, sizeOf().h);
  renderer.addTarget("panner", pannerCanvas, paneSize().w, paneSize().h);
  renderer.addTarget("magnifier", magCanvas, paneSize().w, paneSize().h);
  renderer.initSurface(surfaceCanvas, surfaceSize().w, surfaceSize().h);
  await renderer.checkShaders();
  await renderer.surface.checkShaders();
} catch (err) {
  fatal(err instanceof GpuError ? err.message : `${err.message ?? err}`);
}

new ResizeObserver(() => {
  const { w, h } = sizeOf();
  renderer.resize(w, h);
  viewDirty = true;
  probeDirty = true;
}).observe(canvas);

// -- the frame on screen -------------------------------------------------------------------------------
//
// `frame` is the geometry and the stats of the frame being shown; `pixels` is a copy of its 16-bit
// values, taken from the decoder's shared memory so the probe, imexam and the bar can read it after
// the next decode has reused that memory.

let frame = null;   // { name, w, h, stats, header }
let pixels = null;  // Uint16Array, w*h
let img = null;     // { w, h }
let fitsHdr = null; // the header's `fits` object of the frame on screen
let bin = 1;        // the header's bin factor
let srcDims = null; // { w, h } of the source frame, before binning

function adoptFrame(header, geom, src) {
  const w = geom.w, h = geom.h;
  if (!pixels || pixels.length !== w * h) pixels = new Uint16Array(w * h);
  pixels.set(src);
  const newGeometry = !img || img.w !== w || img.h !== h;
  frame = { name: header.name, w, h, stats: header.stats, header };
  fitsHdr = header.fits ?? null;
  bin = header.bin ?? 1;
  srcDims = { w: header.src_w ?? w * bin, h: header.src_h ?? h * bin };
  img = { w, h };

  if (newGeometry) {
    // Pixels are already pixels (chz1 un-shuffled and dequantized them on the CPU path), so the
    // honest zeros: no tiles, no pedestal, no quantization for the GPU to undo.
    renderer.setGeometry({ w, h, npix: w * h, nTiles: 0, tileRows: h, shift: 0 });
    view = createView(sizeOf(), img);
    renderer.setView(view);
    localLimits = null;
    imexam.closeAll();
    lastHist = null;
    paneDirty = true;
  }
  applyLimits();
  const slot = renderer.uploadPixels(new Uint8Array(pixels.buffer), 0);
  renderer.draw(slot, false);
  viewDirty = true;
  probeDirty = true;
  refreshControls();
  if (!histEl.hidden) void requestHistogram();
}

// -- the stretch ---------------------------------------------------------------------------------------

let limitsName = "zscale";
let localLimits = null;
let cuts = { lo: null, hi: null };

function applyLimits() {
  const all = localLimits?.limits ?? frame?.stats?.limits;
  if (!all) return;
  const pair = all[limitsName] ?? all.zscale;
  if (!pair) return;
  cuts = { lo: pair[0], hi: pair[1] };
  renderer.setStretch(pair[0] / 65535, pair[1] / 65535);
}

async function recomputeLimits() {
  if (!frame) return;
  const r = await renderer.histogram(cropRect(view, img));
  const st = r && fromHistogram(r.hist);
  if (!st) return;
  localLimits = { ...st, region: r.region };
  applyLimits();
  renderer.redraw();
  refreshControls();
}
const limitsScope = () => (localLimits ? "crop" : "frame");

// -- pan, zoom and the pointer -----------------------------------------------------------------------

const SENSITIVITY_KEY = "imageweb.viewer.zoomSensitivity";
let sensitivity = clampSensitivity(Number(localStorage.getItem(SENSITIVITY_KEY)));
function setSensitivity(s) {
  sensitivity = clampSensitivity(s);
  localStorage.setItem(SENSITIVITY_KEY, String(sensitivity));
  probeDirty = true;
}

let view = null;
const cursor = { dx: 0, dy: 0, inside: false };
let viewDirty = false;
let probeDirty = false;
let paneDirty = true;

const devicePos = (e) => devicePixel(canvas.getBoundingClientRect(), e, dpr());

function setView(next) {
  const before = view?.crop;
  view = next;
  viewDirty = true;
  probeDirty = true;
  if (localLimits && cropKey(before) !== cropKey(view.crop)) void recomputeLimits();
}
const cropKey = (c) => (c ? `${c.x},${c.y},${c.w},${c.h}` : "");

canvas.addEventListener("wheel", (e) => {
  e.preventDefault();
  if (!view) return;
  const { dx, dy } = devicePos(e);
  setView(zoomAt(view, sizeOf(), img, dx, dy, wheelFactor(e, sensitivity)));
}, { passive: false });

canvas.addEventListener("pointermove", (e) => {
  Object.assign(cursor, devicePos(e), { inside: true });
  probeDirty = true;
});
canvas.addEventListener("pointerleave", () => { cursor.inside = false; probeDirty = true; });
canvas.addEventListener("contextmenu", (e) => e.preventDefault());

canvas.addEventListener("pointerdown", (e) => {
  if (!isColormapDrag(e)) return;
  e.preventDefault();
  const { w, h } = sizeOf();
  const apply = (ev) => {
    const { dx, dy } = devicePos(ev);
    const { contrast, bias } = contrastBiasAt(dx, dy, w, h);
    renderer.setContrastBias(contrast, bias);
    viewDirty = true;
    refreshControls();
  };
  apply(e);
  const up = () => { removeEventListener("pointermove", apply); removeEventListener("pointerup", up); };
  addEventListener("pointermove", apply);
  addEventListener("pointerup", up);
});

canvas.addEventListener("pointerdown", (e) => {
  if (e.button !== 0 || e.ctrlKey || !view) return;
  let prev = devicePos(e);
  let travelled = 0;
  canvas.style.cursor = "grabbing";
  const move = (ev) => {
    const now = devicePos(ev);
    const ddx = now.dx - prev.dx, ddy = now.dy - prev.dy;
    travelled += Math.abs(ddx) + Math.abs(ddy);
    prev = now;
    Object.assign(cursor, now, { inside: true });
    probeDirty = true;
    if (travelled > DRAG_DEADZONE) setView(panBy(view, sizeOf(), img, ddx, ddy));
  };
  const up = () => {
    removeEventListener("pointermove", move);
    removeEventListener("pointerup", up);
    canvas.style.cursor = "";
    if (travelled <= DRAG_DEADZONE) {
      if (cursorMode) seatCursor();
      probeDirty = true;
    }
  };
  addEventListener("pointermove", move);
  addEventListener("pointerup", up);
});

let flushing = false;
function flush() {
  flushing = true;
  const moved = viewDirty, probed = probeDirty;
  if (viewDirty && view) { viewDirty = false; renderer.setView(view); renderer.redraw(); }
  if (probeDirty) {
    probeDirty = false;
    updateBar();
    updateMagnifier();
    imexam.follow(cursorAt(), { frozen: surfaceView.frozen });
  }
  if (moved || probed || paneDirty) { paneDirty = false; updatePanner(); }
  if (moved || probed) {
    drawOverlay();
    for (const ch of imexam.open()) placeInspector(ch);
  }
  if (moved) refreshControls();
  flushing = false;
}
function pump() { requestAnimationFrame(pump); flush(); }

// -- the panes -----------------------------------------------------------------------------------------

const paneOn = { panner: true, magnifier: true };
let magFactor = 4;
let magPos = null;

function updateMagnifier() {
  if (!paneOn.magnifier || !view) return;
  const at = cursorMode ? pos : cursor.inside ? toImage(view, sizeOf(), cursor.dx, cursor.dy) : null;
  if (at) magPos = { col: at.col, row: at.row };
  if (!magPos) return;
  renderer.setMagnifier({ col: magPos.col, row: magPos.row });
  renderer.drawAux("magnifier");
  const z = magnifierZoom(view, magFactor);
  place(magBox, magCanvas, { dx: paneSize().w / 2, dy: paneSize().h / 2 }, Math.max(7, z / dpr()), screenAngle(view));
  $("mag-label").textContent =
    `${zoomLabel(z)}${magnifierFloored(view, magFactor) ? " (floored)" : ""} · ${magFactor}× · ${(paneSize().w / z).toFixed(0)} px`;
}

function updatePanner() {
  if (!paneOn.panner || !view) return;
  const pane = paneSize(), size = sizeOf();
  const pv = pannerView(view, pane, img);
  const corners = [[0, 0], [size.w, 0], [size.w, size.h], [0, size.h]].map(([dx, dy]) => {
    const im = toImage(view, size, dx, dy);
    return toDevice(pv, pane, im.col, im.row);
  });
  const xs = corners.map((c) => c.dx), ys = corners.map((c) => c.dy);
  pannerRect.hidden = false;
  pannerRect.style.left = `${Math.min(...xs) / dpr()}px`;
  pannerRect.style.top = `${Math.min(...ys) / dpr()}px`;
  pannerRect.style.width = `${(Math.max(...xs) - Math.min(...xs)) / dpr()}px`;
  pannerRect.style.height = `${(Math.max(...ys) - Math.min(...ys)) / dpr()}px`;
  if (view.crop) {
    const c = cropRect(view, img);
    const mid = toDevice(pv, pane, c.x + c.w / 2, c.y + c.h / 2);
    pannerCrop.hidden = false;
    pannerCrop.style.left = `${mid.dx / dpr()}px`;
    pannerCrop.style.top = `${mid.dy / dpr()}px`;
    pannerCrop.style.width = `${(c.w * pv.zoom) / dpr()}px`;
    pannerCrop.style.height = `${(c.h * pv.zoom) / dpr()}px`;
    pannerCrop.style.transform = `translate(-50%, -50%) rotate(${screenAngle(view).toFixed(3)}deg)`;
  } else {
    pannerCrop.hidden = true;
  }
  $("panner-label").textContent = zoomLabel(view.zoom);
}

function setPane(name, on) {
  paneOn[name] = on;
  paneEls[name].hidden = !on;
  renderer.setTargetEnabled(name, on);
  if (on) renderer.drawAux(name);
  paneDirty = true;
  probeDirty = true;
  refreshControls();
}
function setMagFactor(f) {
  magFactor = Math.max(1, Math.min(32, f));
  renderer.setMagnifier({ factor: magFactor });
  probeDirty = true;
  refreshControls();
}

pannerCanvas.addEventListener("pointerdown", (e) => {
  if (e.button !== 0 || !view) return;
  e.preventDefault();
  const pane = paneSize();
  const go = (ev) => {
    const r = pannerCanvas.getBoundingClientRect();
    const pv = pannerView(view, pane, img);
    const at = toImage(pv, pane, (ev.clientX - r.left) * dpr(), (ev.clientY - r.top) * dpr());
    setView(centreOn(view, sizeOf(), img, at.col, at.row));
  };
  go(e);
  const up = () => { pannerCanvas.removeEventListener("pointermove", go); pannerCanvas.removeEventListener("pointerup", up); };
  pannerCanvas.addEventListener("pointermove", go);
  pannerCanvas.addEventListener("pointerup", up);
});

// -- the readout ----------------------------------------------------------------------------------------

const STRETCHES = ["linear", "asinh", "log"];

function updateBar() {
  updateCrosshair();
  if (!view || !img) { writeBar({ idle: true }); return; }
  writeBar({ zoom: zoomLabel(view.zoom), stretch: STRETCHES[renderer.view.mode] ?? "linear", sensitivity });
  const active = cursorMode ? pos !== null : cursor.inside;
  if (!active) { writeBar({ idle: true, x: null, y: null, value: null }); return; }
  const { col, row } = cursorMode ? pos : toImage(view, sizeOf(), cursor.dx, cursor.dy);
  const ds9 = toDs9(col, row);
  const idx = toDisplayedIndex(view, img, col, row);
  writeBar({
    idle: false,
    x: ds9.x.toFixed(1),
    y: ds9.y.toFixed(1),
    value: idx ? pixels[idx.r * img.w + idx.c] : null,
  });
}

// -- the measurement bridge ------------------------------------------------------------------------------

const pixelSource = () =>
  frame && {
    readValue: (c, r) => (c < 0 || c >= img.w || r < 0 || r >= img.h ? null : pixels[r * img.w + c]),
    reader: () => (c, r) => pixels[r * img.w + c],
    geom: img,
    crop: cropRect(view, img),
  };

function measure(m) {
  const r = M.measure(pixelSource(), m);
  if (r?.mode === "d") {
    renderer.surface.setPatch(r.patch);
    renderer.surface.setContours(A.contours(r.patch, r.levels), renderer.lutData());
    renderer.drawSurface();
  }
  return r;
}

// -- imexam -----------------------------------------------------------------------------------------------

const cursorAt = () =>
  cursorMode ? pos : cursor.inside && view ? toImage(view, sizeOf(), cursor.dx, cursor.dy) : null;

const imexam = createImexam({
  // One shared point channel (r/c/d take turns), as this page's single popover
  // inspector + surface assumes; the monorepo core defaults to "each".
  pointChannels: "one",
  request: (msg) => { if (view && img) imexam.accept(msg, measure(msg)); },
  changed: (ch) => {
    if (ch.id === "point") { surfaceHover = null; popoverPos = null; }
    document.body.classList.toggle("cut-column", imexam.columnCut());
    if (!flushing) probeDirty = true;
    drawOverlay();
    updateInspector(ch);
  },
});
const channels = imexam.channels;
const panels = {
  x: { el: $("inspector-x"), update: null },
  y: { el: $("inspector-y"), update: null },
  point: { el: inspectorEl, update: null },
};

function updateInspector(ch) {
  panels[ch.id].update?.({
    mode: ch.mode, result: ch.analysis, arcsecPerPx: null, frozen: surfaceView.frozen, hover: surfaceHover,
    ...stripMapping(ch),
  });
  placeInspector(ch);
}

function stripMapping(ch) {
  if (!ch.mode || !view || SHAPE[ch.mode] !== "strip") return {};
  const c = sizeOf();
  if (ch.mode === "x") {
    return { mapX: {
      width: Math.round(c.w / dpr()), height: 74,
      toIndex: (plotX) => Math.floor(toImage(view, c, plotX * dpr(), c.h / 2).col),
      toPlot: (col) => toDevice(view, c, col, view.cy).dx / dpr(),
    } };
  }
  const height = Math.round(c.h / dpr());
  return { stripHeight: height, mapY: {
    height,
    toIndex: (plotY) => Math.floor(toImage(view, c, c.w / 2, plotY * dpr()).row),
    toPlot: (row) => toDevice(view, c, view.cx, row).dy / dpr(),
  } };
}

// -- what is drawn over the frame ---------------------------------------------------------------------------

const add = (tag, attrs, text) => overlay.appendChild(svgEl(tag, attrs, text));
function P(col, row) {
  const d = toDevice(view, sizeOf(), col, row);
  return [d.dx / dpr(), d.dy / dpr()];
}
function stroked(d, cls) { add("path", { d, class: "halo" }); add("path", { d, class: cls }); }

let grid = "none"; // "none" | "pixel"
function pixelProjection() {
  if (!view) return null;
  const size = sizeOf();
  return {
    toPix(col, row) { const d = toDevice(view, size, col, row); return { x: d.dx / dpr(), y: d.dy / dpr() }; },
    fromPix: (x, y) => { const im = toImage(view, size, x * dpr(), y * dpr()); return { lon: im.col, lat: im.row }; },
    name: "pixel", axes: ["x", "y"], lonWrap: 0,
  };
}

function drawOverlay() {
  overlay.replaceChildren();
  if (!view || !img) return;
  const { w, h } = sizeOf();
  const box = { x: 0, y: 0, w: w / dpr(), h: h / dpr() };
  overlay.setAttribute("viewBox", `0 0 ${box.w} ${box.h}`);
  if (grid === "pixel") {
    const g = Ov.graticule(pixelProjection(), box, { tolerance: 0.5, format: Ov.angleFormatter({ frame: "image" }) });
    appendShapes(overlay, Ov.gridShapes(g, { className: "ov-grid-image" }));
  }
  for (const ch of imexam.open()) drawModeMarkers(ch);
  if (channels.point.mode) drawLeaderIfPlaced();
}

function drawModeMarkers(ch) {
  const { mode, analysis } = ch;
  if (!analysis) return;
  if (mode === "x" || mode === "y") {
    const c = analysis.cut;
    const [a, b] = c.axis === "x"
      ? [P(c.start, c.index + 0.5), P(c.start + c.values.length, c.index + 0.5)]
      : [P(c.index + 0.5, c.start), P(c.index + 0.5, c.start + c.values.length)];
    stroked(`M${a[0].toFixed(1)} ${a[1].toFixed(1)}L${b[0].toFixed(1)} ${b[1].toFixed(1)}`, "cut");
    return;
  }
  const cc = analysis.centre;
  const scale = view.zoom / dpr();
  const [x, y] = P(cc.col, cc.row);
  if (mode === "r") {
    add("circle", { cx: x, cy: y, r: (analysis.profile.rMax * scale).toFixed(2), class: "aperture outer" });
    add("circle", { cx: x, cy: y, r: Math.max(3, 5.5 * scale).toFixed(2), class: "aperture" });
    return;
  }
  if (mode === "c") {
    const p = analysis.patch;
    const corners = [P(p.x0, p.y0), P(p.x0 + p.w, p.y0), P(p.x0 + p.w, p.y0 + p.h), P(p.x0, p.y0 + p.h)];
    add("polygon", { points: corners.map((c) => c.join(",")).join(" "), class: "box" });
    for (const c of analysis.contours) {
      let d = "";
      for (let i = 0; i < c.segments.length; i += 4) {
        const a = P(c.segments[i], c.segments[i + 1]);
        const b = P(c.segments[i + 2], c.segments[i + 3]);
        d += `M${a[0].toFixed(1)} ${a[1].toFixed(1)}L${b[0].toFixed(1)} ${b[1].toFixed(1)}`;
      }
      if (d) add("path", { d, class: "iso" });
    }
  }
}

// -- the 3D surface, and where the inspector sits -------------------------------------------------------------

let surfaceView = S.defaults();
let surfaceHover = null;
function pushSurfaceView() { renderer.setSurfaceView(surfaceView); renderer.drawSurface(); updateInspector(channels.point); }
const surfaceApi = {
  toggleFreeze: () => { surfaceView = { ...surfaceView, frozen: !surfaceView.frozen }; pushSurfaceView(); },
  resetView: () => { surfaceView = S.reset(surfaceView); pushSurfaceView(); },
  setMesh: (on) => { renderer.surface.meshLines = on ? 1 : 0; renderer.drawSurface(); },
};
surfaceCanvas.addEventListener("pointerdown", (e) => {
  if (e.button !== 0) return;
  e.preventDefault();
  surfaceCanvas.classList.add("dragging");
  let prev = { x: e.clientX, y: e.clientY };
  const move = (ev) => {
    surfaceView = S.orbitBy(surfaceView, (ev.clientX - prev.x) * 0.4, -(ev.clientY - prev.y) * 0.4);
    prev = { x: ev.clientX, y: ev.clientY };
    pushSurfaceView();
  };
  const up = () => { removeEventListener("pointermove", move); removeEventListener("pointerup", up); surfaceCanvas.classList.remove("dragging"); };
  addEventListener("pointermove", move);
  addEventListener("pointerup", up);
});
surfaceCanvas.addEventListener("wheel", (e) => {
  e.preventDefault();
  surfaceView = S.zoomBy(surfaceView, Math.exp(-e.deltaY * 0.002));
  pushSurfaceView();
}, { passive: false });
surfaceCanvas.addEventListener("pointermove", (e) => {
  const analysis = channels.point.analysis;
  if (!analysis?.patch || channels.point.mode !== "d") return;
  const r = surfaceCanvas.getBoundingClientRect();
  const size = surfaceSize();
  surfaceHover = S.pick(surfaceView, analysis.patch, size.w, size.h, (e.clientX - r.left) * dpr(), (e.clientY - r.top) * dpr());
  updateInspector(channels.point);
});
surfaceCanvas.addEventListener("pointerleave", () => { surfaceHover = null; updateInspector(channels.point); });

let popoverPos = null;
inspectorEl.addEventListener("pointerdown", (e) => {
  const header = e.target.closest("header");
  if (!header || e.button !== 0 || e.target.closest("button")) return;
  e.preventDefault();
  const box = inspectorEl.getBoundingClientRect();
  const grabX = e.clientX - box.left, grabY = e.clientY - box.top;
  inspectorEl.classList.add("dragging");
  const move = (ev) => { popoverPos = { left: ev.clientX - grabX, top: ev.clientY - grabY }; placePopover(); };
  const up = () => { removeEventListener("pointermove", move); removeEventListener("pointerup", up); inspectorEl.classList.remove("dragging"); };
  addEventListener("pointermove", move);
  addEventListener("pointerup", up);
});

function placeInspector(ch) {
  if (!ch.mode) return;
  panels[ch.id].el.dataset.mode = ch.mode;
  if (SHAPE[ch.mode] === "strip") placeStrip(ch);
  else placePopover();
}
function placeStrip(ch) {
  const r = canvas.getBoundingClientRect();
  const el = panels[ch.id].el;
  el.classList.toggle("vertical", ch.mode === "y");
  if (ch.mode === "x") {
    Object.assign(el.style, { left: `${r.left}px`, width: `${r.width}px`, right: "", top: "", bottom: "56px", height: "" });
  } else {
    Object.assign(el.style, { right: "12px", left: "", top: `${r.top}px`, bottom: "", height: `${r.height}px`, width: "168px" });
  }
}
function placePopover() {
  inspectorEl.classList.remove("vertical");
  inspectorEl.style.width = "";
  inspectorEl.style.height = "";
  const at = anchorDevice();
  if (!at) return;
  const r = canvas.getBoundingClientRect();
  const anchor = { x: r.left + at.dx / dpr(), y: r.top + at.dy / dpr() };
  const box = inspectorEl.getBoundingClientRect();
  const size = { width: box.width || 360, height: box.height || 300 };
  const viewport = { width: window.innerWidth, height: window.innerHeight };
  if (!popoverPos) popoverPos = placeBeside(anchor, size, viewport);
  const { left, top } = clampToViewport(popoverPos, size, viewport);
  Object.assign(inspectorEl.style, { left: `${left}px`, top: `${top}px`, right: "", bottom: "" });
}
function anchorDevice() {
  const ch = channels.point;
  const c = ch.analysis?.centre ?? ch.anchor;
  return view && c ? toDevice(view, sizeOf(), c.col, c.row) : null;
}
function drawLeaderIfPlaced() {
  const at = anchorDevice();
  if (!at || !popoverPos) return;
  const r = canvas.getBoundingClientRect();
  const box = inspectorEl.getBoundingClientRect();
  const anchor = { x: r.left + at.dx / dpr(), y: r.top + at.dy / dpr() };
  const end = edgeToward(anchor, { left: box.left, top: box.top, width: box.width, height: box.height });
  add("path", { d: `M${anchor.x - r.left} ${anchor.y - r.top}L${end.x - r.left} ${end.y - r.top}`, class: "leader" });
  add("circle", { cx: anchor.x - r.left, cy: anchor.y - r.top, r: 1.6, class: "leader-dot" });
}

// -- the keyboard cursor -----------------------------------------------------------------------------------

let cursorMode = false;
let pos = null;
function seatCursor() { pos = Cur.seat(cursor.inside ? toImage(view, sizeOf(), cursor.dx, cursor.dy) : null, view, img); }
function stepCursor(dcol, drow, big) {
  if (!view) return;
  if (!pos) seatCursor();
  pos = Cur.step(pos, dcol, drow, img, big);
  const size = sizeOf();
  const { ddx, ddy } = Cur.keepVisible(toDevice(view, size, pos.col, pos.row), size);
  if (ddx || ddy) setView(panBy(view, size, img, ddx, ddy));
  probeDirty = true;
}
function toggleCursor() { cursorMode = !cursorMode; if (cursorMode && !pos) seatCursor(); probeDirty = true; refreshControls(); }
function updateCrosshair() {
  if (!cursorMode || !pos || !view) { crosshair.hidden = true; return; }
  place(crosshair, canvas, toDevice(view, sizeOf(), pos.col, pos.row), Cur.crosshairSize(view, dpr()), screenAngle(view));
}
function place(el, over, at, sizeCss, angleDeg) {
  const r = over.getBoundingClientRect();
  el.hidden = false;
  el.style.left = `${r.left + at.dx / dpr()}px`;
  el.style.top = `${r.top + at.dy / dpr()}px`;
  el.style.width = `${sizeCss}px`;
  el.style.height = `${sizeCss}px`;
  el.style.transform = `translate(-50%, -50%) rotate(${angleDeg.toFixed(3)}deg)`;
}

// -- the panels ----------------------------------------------------------------------------------------------

panels.point.update = mountInspector(inspectorEl, { onClose: () => imexam.close(channels.point), surface: surfaceApi, canvas: surfaceCanvas });
panels.x.update = mountInspector(panels.x.el, { onClose: () => imexam.close(channels.x) });
panels.y.update = mountInspector(panels.y.el, { onClose: () => imexam.close(channels.y) });

const updateHistogramPanel = mountHistogram(histEl, { onClose: () => toggleHistogram() });
let lastHist = null;
function toggleHistogram() { histEl.hidden = !histEl.hidden; if (!histEl.hidden) void requestHistogram(); }
async function requestHistogram() {
  if (histEl.hidden || !frame) return;
  const m = await renderer.histogram(cropRect(view, img));
  if (!m || histEl.hidden) return;
  lastHist = m;
  updateHistogramPanel({ ...m, lo: cuts.lo, hi: cuts.hi, total: m.region.w * m.region.h });
}

const updateControls = mountControls(controlsEl, {
  send: (m) => {
    if (m.type === "colormap") renderer.setColormap(m.name, m.invert);
    else if (m.type === "contrastBias") renderer.setContrastBias(m.contrast, m.bias);
    else if (m.type === "limits") { limitsName = m.name; applyLimits(); }
    viewDirty = true;
    refreshControls();
    if (!histEl.hidden && lastHist) updateHistogramPanel({ ...lastHist, lo: cuts.lo, hi: cuts.hi });
  },
  local: {
    setStretch: (name) => { renderer.cycleMode(name); viewDirty = true; probeDirty = true; refreshControls(); },
    toggleCursor,
    fit: () => view && setView(fitView(view, sizeOf(), img)),
    oneToOne: () => view && setView({ ...view, zoom: 1 }),
    setFlip: (name) => view && setView(setFlip(view, name)),
    setRotation: (deg) => view && setView(setRotation(view, deg)),
    northUp: () => {},
    setCrop: (rect) => view && setView(setCrop(view, rect)),
    cropToView: () => view && setView(cropToView(view, sizeOf(), img)),
    setPane,
    setMagFactor,
  },
});

function refreshControls() {
  const v = renderer.view;
  updateControls({
    view: { cmap: v.cmap, invert: v.invert, contrast: v.contrast, bias: v.bias, lo: cuts.lo, hi: cuts.hi, limits: limitsName, limitsScope: limitsScope() },
    mode: STRETCHES[v.mode] ?? "linear",
    zoomLabel: view ? zoomLabel(view.zoom) : null,
    sensitivity, cursorMode, transform: view, image: img, hasWcs: false, northAligned: false,
    paneOn, magFactor,
    magLabel: view ? `${zoomLabel(magnifierZoom(view, magFactor))}${magnifierFloored(view, magFactor) ? " floored" : ""}` : null,
  });
}

mountHelp(helpEl, {
  title: "instrument quick look — every control",
  groups: [
    ...bindingsFor(VIEWER_BINDINGS, "stream").filter(([title]) => title !== "sky"),
    ["this page", [
      ["k", "the header panel"],
      ["shift + G", "the pixel grid over the frame"],
    ]],
  ],
});

// -- the keyboard --------------------------------------------------------------------------------------------

addEventListener("keydown", (e) => {
  if (e.key === "?" && !e.metaKey && !(e.target instanceof HTMLInputElement)) {
    e.preventDefault();
    helpEl.hidden = !helpEl.hidden;
    return;
  }
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.target instanceof HTMLInputElement || e.target instanceof HTMLSelectElement) return;
  if (!helpEl.hidden) { if (e.key === "Escape") helpEl.hidden = true; return; }
  const step = Cur.CURSOR_KEYS[e.key];
  if (step && cursorMode) { e.preventDefault(); stepCursor(step[0], step[1], e.shiftKey); return; }
  switch (e.key) {
    case "f": if (view) setView(fitView(view, sizeOf(), img)); break;
    case "z": if (view) setView({ ...view, zoom: 1 }); break;
    case "s": renderer.cycleMode(); viewDirty = true; probeDirty = true; refreshControls(); break;
    case "i": renderer.setColormap(undefined, !renderer.view.invert); viewDirty = true; refreshControls(); break;
    case ",": case ".": cycleColormap(e.key === "." ? 1 : -1); break;
    case "R": renderer.setContrastBias(1, 0.5); viewDirty = true; refreshControls(); break;
    case "x": case "y": case "r": case "c": case "d": imexam.setMode(e.key, cursorAt()); break;
    case "Escape": imexam.closeTop(); break;
    case "C": toggleCursor(); break;
    case "h": toggleHistogram(); break;
    case "p": controlsEl.hidden = !controlsEl.hidden; if (!controlsEl.hidden) refreshControls(); break;
    case "k": toggleHdr(); break;
    case "G": grid = grid === "pixel" ? "none" : "pixel"; drawOverlay(); break;
    case "n": setPane("panner", !paneOn.panner); break;
    case "g": setPane("magnifier", !paneOn.magnifier); break;
    case "<": setMagFactor(magFactor / 2); break;
    case ">": setMagFactor(magFactor * 2); break;
    case "[": setSensitivity(sensitivity / 2); break;
    case "]": setSensitivity(sensitivity * 2); break;
    default: return;
  }
  e.preventDefault();
});
function cycleColormap(dir) {
  const i = COLORMAPS.indexOf(renderer.view.cmap);
  renderer.setColormap(COLORMAPS[(i + dir + COLORMAPS.length) % COLORMAPS.length]);
  viewDirty = true;
  refreshControls();
}
function toggleHdr() {
  hdrEl.hidden = !hdrEl.hidden;
  $("hdr-toggle").classList.toggle("on", !hdrEl.hidden);
}
$("hdr-toggle").addEventListener("click", toggleHdr);
$("help-toggle").addEventListener("click", () => { helpEl.hidden = !helpEl.hidden; });

// -- the stream -------------------------------------------------------------------------------------------------

const TIERS = {
  lossless: { bin: 1, q: 0 },
  bin2: { bin: 2, q: 0.5, dither: true },
  bin4: { bin: 4, q: 0.5, dither: true },
};
const qp = new URLSearchParams(location.search);
if (qp.has("workers")) $("workers").value = qp.get("workers");
if (qp.has("tier")) $("tier").value = qp.get("tier");

const wasm = await loadWasm(url("../pkg/chz1/ts/src/pkg/decoder.wasm"));
const stream = new Chz1Stream({
  wasm,
  spawnDecoder: () => new Worker(url("../pkg/chz1/ts/src/decode-worker.js"), { type: "module" }),
});
stream.unshuffle = "cpu"; // the pixels arrive reconstructed; the renderer takes them as pixels

let socket = null;
const arrivals = []; // [t_ms, bytes] of the last 20 frames
let lastFrameAt = null;
const stripState = (s) => ($("strip-state").textContent = s);

// -- transfer gating: the frame WS exists only while this page is being looked at ---------------
//
// Two signals, ANDed: the browser tab's own visibility, and — when embedded as the SPA's Quick
// Look tab — the host's postMessage. The viewer state and the last frame stay on screen; only
// the socket comes and goes. Latest-frame replay on the bridge makes reactivation cost one
// reconnect, never a stale view. Design: image-viewer-plan.md § The Quick Look tab.
let tabActive = true;   // the embedding host's tab state (standalone: always true)
const isActive = () => tabActive && !document.hidden;

function disconnect() {
  if (retry) { clearTimeout(retry); retry = null; }
  if (socket) { socket.onclose = socket.onerror = socket.onmessage = null; socket.close(); socket = null; }
  stripState("paused (not visible)");
}
function applyActive() {
  if (isActive()) { if (!socket) connect(); }
  else disconnect();
}
addEventListener("visibilitychange", applyActive);
addEventListener("message", (e) => {
  const q = e.data?.quicklook;
  if (q === "active" || q === "inactive") { tabActive = q === "active"; applyActive(); }
});

function connect() {
  if (!isActive()) return;   // a stale retry must not reconnect a hidden page
  if (retry) { clearTimeout(retry); retry = null; }
  if (socket) { socket.onclose = socket.onerror = socket.onmessage = null; socket.close(); }
  const want = Number($("workers").value);
  stream.workers = crossOriginIsolated ? want : 0;
  stream.preview = TIERS[$("tier").value];
  socket = stream.connect(wsUrl("ws").href);
  let chain = Promise.resolve(); // decodes strictly one at a time
  socket.onmessage = (e) => {
    const buf = e.data;
    chain = chain.then(async () => {
      const t0 = performance.now();
      const { header, geom, decoded } = await stream.decode(buf);
      const src = new Uint16Array(decoded.bytes.buffer, decoded.base + geom.pixelsOff, geom.npix);
      adoptFrame(header, geom, src);
      const ms = performance.now() - t0;
      stream.finished();
      stream.ack(header.seq, { decode_ms: Math.round(ms * 10) / 10 }); // `client` is an object, per protocol
      lastFrameAt = performance.now();
      arrivals.push([lastFrameAt, buf.byteLength]);
      while (arrivals.length > 20) arrivals.shift();
      reportRate(header, geom, buf.byteLength);
      stripState("connected");   // clear the "no image yet" notice on the first frame
      updateHdr({ fits: fitsHdr, status, ageS: 0 });
    }).catch((err) => {
      stripState(`decode failed: ${err?.message ?? err}`);
      console.error(err);
      stream.ack(-1, {});
    });
  };
}

function reportRate(header, geom, bytes) {
  const n = arrivals.length;
  let rate = "";
  if (n > 1) {
    const dt = (arrivals[n - 1][0] - arrivals[0][0]) / 1e3;
    let sum = 0;
    for (let i = 1; i < n; i++) sum += arrivals[i][1];
    rate = `${((n - 1) / dt).toFixed(1)} fps · ${(sum / dt / 1e6).toFixed(2)} MB/s · `;
  }
  const c = header.crop;
  const of = c && c.n > 1 ? ` (centre 1/${c.n} of ${c.src_w}×${c.src_h}${header.bin > 1 ? `, bin ${header.bin}` : ""})`
    : header.bin > 1 ? ` (bin ${header.bin})` : "";
  $("strip-rate").textContent =
    `${rate}${geom.w}×${geom.h}${of} · ${(bytes / 1e6).toFixed(2)} MB/frame` +
    `${header.qstep ? ` · q step ${header.qstep}` : " · lossless"}`;
}

let retry = null;
let retryDelay = 1000;
stream.on("status", (s) => {
  // Before the first readout the canvas is black by design — say so, or the
  // page reads as broken.
  stripState(s.error ?? (s.connected
    ? (frame ? "connected" : "connected — no image yet; waiting for a readout")
    : "disconnected — retrying"));
  $("strip-state").classList.toggle("bad", !!s.error);
  if (s.connected) { retryDelay = 1000; return; }
  if (!isActive()) return;   // paused: no reconnect while hidden
  if (retry) return;
  retry = setTimeout(() => { retry = null; connect(); }, retryDelay);
  retryDelay = Math.min(retryDelay * 2, 15000);
});
// WebGPU's default maxTextureDimension2D is 8192, and a full unbinned PFS frame is 10560 wide:
// `lossless` cannot be uploaded for such frames. The viewer creates its device with default
// limits, so the page refuses the tier with a notice instead of failing mid-decode (raising the
// device limit is an upstream viewer item — image-viewer-plan.md § Sizing).
const MAX_TEX = 8192;
function tierFits(name) {
  if (!srcDims) return true;
  const b = Math.max(1, TIERS[name].bin);
  return srcDims.w / b <= MAX_TEX && srcDims.h / b <= MAX_TEX;
}
$("tier").addEventListener("change", () => {
  const name = $("tier").value;
  if (!tierFits(name)) {
    stripState(`frame too large for ${name} (WebGPU limit ${MAX_TEX}px)`);
    $("tier").value = Object.keys(TIERS).find((t) => TIERS[t].bin === bin) ?? "bin4";
    return;
  }
  stream.configure(TIERS[name]);
});
$("workers").addEventListener("change", () => socket && connect());
addEventListener("error", (e) => stripState(`error: ${e.message}`));
addEventListener("unhandledrejection", (e) => stripState(`error: ${e.reason?.message ?? e.reason}`));

// -- /status: what the bridge knows when frames stop -----------------------------------------------------------

let status = null;
let statusSocket = null;
function connectStatus() {
  statusSocket = new WebSocket(wsUrl("status"));
  statusSocket.onmessage = (e) => {
    status = JSON.parse(e.data);
    const ageS = lastFrameAt === null ? null : (performance.now() - lastFrameAt) / 1e3;
    updateHdr({ fits: fitsHdr, status, ageS });
  };
  statusSocket.onclose = () => setTimeout(connectStatus, 3000);
}

// -- go --------------------------------------------------------------------------------------------------------

globalThis.__viewer = { flush, renderer: () => renderer, frame: () => frame, view: () => view, fits: () => fitsHdr, status: () => status };

if (NAME) { $("strip-title").textContent = NAME; document.title = `${NAME} — quick look`; }
refreshControls();
updateHdr({ fits: null, status: null, ageS: null });
pump();
applyActive();
connectStatus();
