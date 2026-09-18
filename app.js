// SPA shell: hosts the view toggle (Window | Diagnostic), persists the
// active view in localStorage, and imports both views as side-effect
// modules. The actual UI is owned by diagnostic.js and renderer.js.
//
// Default view: window. ADC and DCU layouts are bound and verified;
// Diagnostic stays one click away for protocol-level debugging.

import "./diagnostic.js";
import "./window-host.js";   // owns #window-view; mounts renderer.js per sub-tab

const VIEW_KEY = "lco_view";
const DEFAULT_VIEW = "window";
const VIEWS = ["window", "diagnostic"];

const setView = (view) => {
  if (!VIEWS.includes(view)) view = DEFAULT_VIEW;
  for (const v of VIEWS) {
    document.getElementById(`${v}-view`).hidden = (v !== view);
    document.querySelector(`.view-toggle button[data-view="${v}"]`)
      .classList.toggle("active", v === view);
  }
  try { localStorage.setItem(VIEW_KEY, view); } catch (e) { /* private mode etc. */ }
};

for (const btn of document.querySelectorAll(".view-toggle button[data-view]")) {
  btn.addEventListener("click", () => setView(btn.dataset.view));
}

let initial = null;
try { initial = localStorage.getItem(VIEW_KEY); } catch (e) { /* ignore */ }
setView(initial || DEFAULT_VIEW);

// UI scale slider — drives a single CSS variable (--ui-scale) on the root,
// which renderer.js's .window-scale-wrap / .window-frame rules read.
// Starts at 1× -- the window at the size its XIB gives it -- and the
// operator's own setting is kept per browser. The key was renamed when the
// default went from 1.5 to 1: the old code stored the default on every load,
// so every browser held a "chosen" 1.5 that was never chosen.
const SCALE_KEY     = "lco_ui_scale_v2";
const DEFAULT_SCALE = 1.0;

const applyScale = (scale, { persist = false } = {}) => {
  const s = Number(scale);
  const clamped = Number.isFinite(s) ? Math.min(3, Math.max(1, s)) : DEFAULT_SCALE;
  document.documentElement.style.setProperty("--ui-scale", String(clamped));
  const readout = document.getElementById("ui-scale-readout");
  if (readout) readout.textContent = `${clamped.toFixed(1)}×`;
  const slider = document.getElementById("ui-scale");
  if (slider && Number(slider.value) !== clamped) slider.value = String(clamped);
  if (persist) { try { localStorage.setItem(SCALE_KEY, String(clamped)); } catch (e) { /* ignore */ } }
};

const scaleSlider = document.getElementById("ui-scale");
if (scaleSlider) {
  scaleSlider.addEventListener("input", () => applyScale(scaleSlider.value, { persist: true }));
}

let initialScale = null;
try { initialScale = localStorage.getItem(SCALE_KEY); } catch (e) { /* ignore */ }
applyScale(initialScale != null ? initialScale : DEFAULT_SCALE);
