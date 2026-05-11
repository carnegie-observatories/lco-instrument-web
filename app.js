// SPA shell: hosts the view toggle (Window | Diagnostic), persists the
// active view in localStorage, and imports both views as side-effect
// modules. The actual UI is owned by diagnostic.js and (later) renderer.js.
//
// Default view: diagnostic. ws-ui-conversion-plan phase 12 flips the
// default to window once ADC + DCU layouts are bound and verified.

import "./diagnostic.js";
import "./renderer.js";

const VIEW_KEY = "lco_view";
const DEFAULT_VIEW = "diagnostic";
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
// ADC is 480×294 and DCU is 677×144; at 1× both windows are tiny on a
// modern 4K display, so default to 1.5×.
const SCALE_KEY     = "lco_ui_scale";
const DEFAULT_SCALE = 1.5;

const applyScale = (scale) => {
  const s = Number(scale);
  const clamped = Number.isFinite(s) ? Math.min(3, Math.max(1, s)) : DEFAULT_SCALE;
  document.documentElement.style.setProperty("--ui-scale", String(clamped));
  const readout = document.getElementById("ui-scale-readout");
  if (readout) readout.textContent = `${clamped.toFixed(1)}×`;
  const slider = document.getElementById("ui-scale");
  if (slider && Number(slider.value) !== clamped) slider.value = String(clamped);
  try { localStorage.setItem(SCALE_KEY, String(clamped)); } catch (e) { /* ignore */ }
};

const scaleSlider = document.getElementById("ui-scale");
if (scaleSlider) {
  scaleSlider.addEventListener("input", () => applyScale(scaleSlider.value));
}

let initialScale = null;
try { initialScale = localStorage.getItem(SCALE_KEY); } catch (e) { /* ignore */ }
applyScale(initialScale != null ? initialScale : DEFAULT_SCALE);
