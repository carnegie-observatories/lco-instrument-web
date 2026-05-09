// SPA shell: hosts the view toggle (Window | Diagnostic), persists the
// active view in localStorage, and imports both views as side-effect
// modules. The actual UI is owned by diagnostic.js and (later) renderer.js.
//
// Default view: diagnostic. ws-ui-conversion-plan phase 12 flips the
// default to window once ADC + DCU layouts are bound and verified.

import "./diagnostic.js";
// import "./renderer.js";  // landed in a later phase

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
