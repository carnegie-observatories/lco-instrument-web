// Window-view host: owns the #window-view DOM, fetches the per-app
// manifest after WS hello, builds a sub-tab strip, and lazy-mounts
// renderer.js instances into per-tab host divs.
//
// Multi-window contract (see ws-ui-conversion-plan.md § Multi-window
// per app):
//   - `instruments/<app>/manifest.json` declares the windows the SPA
//     should render: an array of { id, title, layout, default? }.
//   - Each window's layout JSON lives under `generated/<app>/<id>.json`
//     (or whatever path the manifest entry's `layout` field resolves
//     to, relative to the app dir).
//   - An entry may carry `embed` (a path, e.g. "/image/pfs/") instead
//     of `layout`: the tab lazy-mounts an iframe on that URL — the
//     imageweb Quick Look — and posts {quicklook: "active"|"inactive"}
//     to it on tab switches, so the embedded page opens its frame WS
//     only while its tab is the active one. Unlike renderer tabs
//     (kept subscribed while hidden — cheap JSON), the embed's stream
//     is the expensive part; the iframe itself stays mounted so
//     zoom/stretch and the last frame survive a tab switch. See
//     docs/plans/image-viewer-plan.md § The Quick Look tab.
//   - `?tab=<id>` pins the initial tab per page instance (overrides
//     the localStorage last-tab, which every instance shares) and is
//     written back on manual switches, so a browser window parked on
//     one tab keeps it across reloads.
//   - Tabs are lazy-mounted on first activation; once mounted, the
//     host div stays in the DOM with display:none so subsequent
//     reactivations are instant and the renderer's topic subscriptions
//     keep flowing. Each renderer tracks its own subscribers, so a
//     tab teardown only removes that tab's subs.
//   - On a different `app` in a subsequent hello, every mounted
//     renderer is destroyed and the strip is rebuilt from the new
//     manifest.
//
// Single-window apps (ADC, DCU today) still get a strip with one tab.
// This avoids a special-case branch and lets multi-window apps (PFS,
// LDSS3 follow-ups) drop in without SPA code changes.

import { onHello, url as wsUrl } from "./ws.js";
import { mountRenderer } from "./renderer.js";

const ROOT_ID = "window-view";

// Persisted last-selected-tab per app, so reload lands on the same view.
const LS_KEY = (app) => `lco_window_tab__${app}`;

// Module-level state: the currently active app, the manifest for it,
// and the per-tab mount cache. All three are torn down + rebuilt on
// an `app` change in a subsequent hello.
let currentApp     = null;
let currentTabs    = [];          // [{id, title, host, mount}]
let activeTabId    = null;

const $ = (id) => document.getElementById(id);

const showPlaceholder = (text, hint) => {
  const root = $(ROOT_ID);
  if (!root) return;
  root.innerHTML = "";
  const div = document.createElement("div");
  div.className = "placeholder";
  const p = document.createElement("p");
  p.textContent = text;
  div.appendChild(p);
  if (hint) {
    const h = document.createElement("p");
    h.className = "hint muted";
    h.textContent = hint;
    div.appendChild(h);
  }
  root.appendChild(div);
};

// Drop every mounted renderer and clear the host. Called before an
// app switch and on initial reset.
const teardownTabs = () => {
  for (const t of currentTabs) {
    if (t.mount) {
      try { t.mount.destroy(); } catch (e) { console.error("window-host: destroy failed", e); }
      t.mount = null;
    }
  }
  currentTabs = [];
  activeTabId = null;
  const root = $(ROOT_ID);
  if (root) root.innerHTML = "";
};

// Read the last-selected tab id for `app` from localStorage. Returns
// null if nothing stored or the storage call fails (private mode).
const readLastTab = (app) => {
  try { return localStorage.getItem(LS_KEY(app)); }
  catch (e) { return null; }
};

// The embed URL for an `embed` manifest entry. Behind the tunnel (https)
// instruments are paths on the page's own hostname, so the path stands as-is;
// in local/VPN dev the gateway is its own origin on the *instrument* host
// (it runs beside the Cocoa app), port 8766. Cross-origin the iframe still
// works — the embedded page falls back to inline decode without
// crossOriginIsolated; the tunnel path gets the full decode pool.
const embedUrl = (path) => {
  if (window.location.protocol === "https:") return path;
  let host = "127.0.0.1";
  try { host = new URL(wsUrl).hostname || host; } catch (e) { /* keep default */ }
  return `http://${host}:8766${path}`;
};

const postQuicklook = (tab, state) => {
  try { tab.mount?.iframe?.contentWindow?.postMessage({ quicklook: state }, "*"); }
  catch (e) { /* iframe gone mid-switch */ }
};

const writeLastTab = (app, id) => {
  try { localStorage.setItem(LS_KEY(app), id); } catch (e) { /* ignore */ }
};

// Activate a tab by id. Lazy-mounts the renderer if this is the
// first activation; subsequent activations just toggle display.
const activateTab = (id) => {
  const tab = currentTabs.find(t => t.id === id);
  if (!tab) return;

  // The outgoing tab, if it is an embed, stops transferring.
  if (activeTabId && activeTabId !== id) {
    const prev = currentTabs.find(t => t.id === activeTabId);
    if (prev && prev.embed) postQuicklook(prev, "inactive");
  }

  // Toggle display for all hosts; only the active one is visible.
  for (const t of currentTabs) {
    if (t.host) t.host.style.display = (t.id === id) ? "" : "none";
  }
  // Reflect active state on the tab buttons.
  for (const t of currentTabs) {
    if (t.button) t.button.classList.toggle("is-active", t.id === id);
  }
  // Drop focus from any input inside the previously-active tab so a
  // pending state push doesn't get blocked by document.activeElement
  // still pointing into a now-hidden subtree (the focus-guard rule in
  // writeNodeValue: an `<input>` with activeElement === this skips
  // incoming state). Browser behaviour around focus inside
  // display:none is inconsistent; explicitly dropping focus on tab
  // switch makes the guard predictable.
  if (activeTabId && activeTabId !== id && document.activeElement
      && typeof document.activeElement.blur === "function") {
    document.activeElement.blur();
  }
  activeTabId = id;
  writeLastTab(currentApp, id);
  // Pin this instance's tab in the URL, so a reload lands here even if
  // another instance rewrote the shared localStorage entry meanwhile.
  const q = new URLSearchParams(window.location.search);
  if (q.get("tab") !== id) {
    q.set("tab", id);
    try { history.replaceState(null, "", `${window.location.pathname}?${q}`); }
    catch (e) { /* ignore */ }
  }

  if (tab.embed) {
    if (!tab.mount) {              // first activation: mount the iframe
      const iframe = document.createElement("iframe");
      iframe.className = "embed-frame";
      iframe.src = embedUrl(tab.embed);
      tab.host.classList.add("embed-pane");
      tab.host.appendChild(iframe);
      tab.mount = { iframe, destroy: () => iframe.remove() };
      // No "active" post here: the page starts active and only pauses on
      // an explicit "inactive" (or its own visibilitychange).
    } else {
      postQuicklook(tab, "active");
    }
    return;
  }

  if (tab.mount) return;            // already mounted; nothing more to do.

  // First activation: fetch the layout JSON and mount the renderer.
  // Relative URLs land under the app's generated/ subdir.
  const url = `./generated/${currentApp}/${tab.layout}`;
  fetch(url)
    .then((r) => {
      if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
      return r.json();
    })
    .then((layout) => {
      // The tab may have been torn down between fetch start and
      // resolution (app switch). Bail if so — host is detached and
      // the manifest no longer lists this tab.
      if (!currentTabs.find(t => t === tab) || !tab.host) return;
      tab.mount = mountRenderer(tab.host, layout);
    })
    .catch((e) => {
      console.error(`window-host: failed to load ${url}:`, e);
      if (tab.host) {
        tab.host.innerHTML = "";
        const p = document.createElement("p");
        p.className = "placeholder muted";
        p.textContent = `Layout not available: ${url}`;
        tab.host.appendChild(p);
      }
    });
};

const buildTabs = (manifest) => {
  const root = $(ROOT_ID);
  if (!root) return;
  root.innerHTML = "";

  // Sub-tab strip — one button per window.
  const strip = document.createElement("nav");
  strip.className = "subtab-strip";
  strip.setAttribute("role", "tablist");

  // Each tab's host is a sibling div under #window-view. We keep all
  // of them in the DOM after mount and just toggle display, so
  // subscriptions stay live across tab switches.
  for (const w of manifest.windows) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "subtab";
    button.dataset.tabId = w.id;
    button.textContent = w.title || w.id;
    button.setAttribute("role", "tab");
    button.addEventListener("click", () => activateTab(w.id));
    strip.appendChild(button);

    const host = document.createElement("div");
    host.className = "subtab-pane";
    host.dataset.tabId = w.id;
    host.style.display = "none";

    currentTabs.push({
      id: w.id,
      title: w.title || w.id,
      layout: w.layout,
      embed: w.embed || null,
      isDefault: !!w.default,
      host,
      button,
      mount: null,
    });
  }

  root.appendChild(strip);
  for (const t of currentTabs) root.appendChild(t.host);

  // Choose initial tab: the ?tab= URL param first (per-instance pin),
  // then the stored last-selected if it still exists, then the
  // manifest's `default: true`, else the first entry.
  let initial = null;
  const urlTab = new URLSearchParams(window.location.search).get("tab");
  const stored = readLastTab(currentApp);
  if (urlTab && currentTabs.find(t => t.id === urlTab)) {
    initial = urlTab;
  } else if (stored && currentTabs.find(t => t.id === stored)) {
    initial = stored;
  } else {
    const def = currentTabs.find(t => t.isDefault);
    initial = (def || currentTabs[0]).id;
  }
  activateTab(initial);
};

const loadManifestFor = (app) => {
  const url = `./instruments/${app}/manifest.json`;
  fetch(url)
    .then((r) => {
      if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
      return r.json();
    })
    .then((manifest) => {
      // Guard against a stale fetch resolving after a subsequent app
      // switch — only act if we're still on the app this fetch was
      // initiated for.
      if (currentApp !== app) return;
      if (!manifest || !Array.isArray(manifest.windows) || manifest.windows.length === 0) {
        showPlaceholder(
          `Empty manifest for app "${app}".`,
          `${url}: expected a non-empty "windows" array.`,
        );
        return;
      }
      buildTabs(manifest);
    })
    .catch((e) => {
      console.error(`window-host: failed to load ${url}:`, e);
      showPlaceholder(
        `Manifest not available for app "${app}".`,
        `${url}: ${e.message}`,
      );
    });
};

// Initial state: nothing until the WS hello announces an app.
showPlaceholder(
  "Waiting for WebSocket hello…",
  "The Window view populates once the instrument app announces its identity over the control WS.",
);

onHello((msg) => {
  const app = msg && msg.app && String(msg.app).toLowerCase();
  if (!app) return;
  if (app === currentApp) return;        // same app, no need to rebuild.
  teardownTabs();
  currentApp = app;
  loadManifestFor(app);
});
