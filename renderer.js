// Window-view renderer. Loads <app>.layout.json (the IR produced by xib2ir)
// and builds an absolutely-positioned DOM tree, with each element bound to
// WS topic stores and command dispatch via the shared ws.js plumbing.
//
// Phase 3: consumes a hand-authored fixture (generated/adc.layout.json).
// Future phases: the converter generates the same IR shape; multi-app
// routing picks the file by ?app= or by the WS hello frame.

import { topic, cmd } from "./ws.js";

const params = new URLSearchParams(window.location.search);
const APP = (params.get("app") || "adc").toLowerCase();
const LAYOUT_URL = `./generated/${APP}.layout.json`;

// ---------------- IR → DOM ----------------

const renderWindow = (layout) => {
  const root = document.getElementById("window-view");
  root.innerHTML = "";

  const frame = document.createElement("div");
  frame.className = "window-frame";
  frame.style.width  = `${layout.window.width}px`;
  frame.style.height = `${layout.window.height}px`;
  if (layout.window.title) frame.dataset.title = layout.window.title;
  root.appendChild(frame);

  // Bucket elements by parent_id for tree reconstruction.
  const childrenOf = new Map();
  for (const el of layout.elements) {
    const list = childrenOf.get(el.parent_id) ?? [];
    list.push(el);
    childrenOf.set(el.parent_id, list);
  }

  for (const el of (childrenOf.get(null) || [])) {
    frame.appendChild(renderEl(el, childrenOf));
  }
};

const renderEl = (el, childrenOf) => {
  const node = createNode(el);
  positionAbs(node, el.frame);
  node.dataset.outlet = el.outlet || "";
  node.dataset.id = el.id;

  for (const child of (childrenOf.get(el.id) || [])) {
    node.appendChild(renderEl(child, childrenOf));
  }

  applyBinding(el, node);
  return node;
};

const createNode = (el) => {
  switch (el.kind) {
    case "box": {
      const div = document.createElement("div");
      div.className = "wf-box";
      if (el.title) {
        const lg = document.createElement("div");
        lg.className = "wf-legend";
        lg.textContent = el.title;
        div.appendChild(lg);
      }
      return div;
    }
    case "button": {
      const b = document.createElement("button");
      b.className = `wf-button wf-${el.subkind || "push"}`;
      b.textContent = el.title_default || "";
      return b;
    }
    case "popup": {
      const sel = document.createElement("select");
      sel.className = `wf-popup wf-${el.subkind || "popup"}`;
      for (const opt of (el.options || [])) {
        const o = document.createElement("option");
        if (typeof opt === "object") {
          o.value = opt.value; o.textContent = opt.label ?? opt.value;
        } else {
          o.value = String(opt); o.textContent = String(opt);
        }
        sel.appendChild(o);
      }
      if (el.title_default != null) {
        const match = [...sel.options].find(o => o.textContent === el.title_default);
        if (match) sel.value = match.value;
      }
      return sel;
    }
    case "textfield": {
      if (el.subkind === "readout") {
        const out = document.createElement("output");
        out.className = "wf-readout";
        out.textContent = el.title_default || "";
        return out;
      }
      // input / default
      const inp = document.createElement("input");
      inp.className = "wf-input";
      inp.type = "text";
      if (el.title_default != null) inp.value = el.title_default;
      return inp;
    }
    case "label": {
      const span = document.createElement("span");
      span.className = "wf-label";
      span.textContent = el.title_default || "";
      return span;
    }
    default: {
      const div = document.createElement("div");
      div.className = "wf-unknown";
      div.textContent = `[${el.kind}/${el.subkind || ""}]`;
      return div;
    }
  }
};

const positionAbs = (node, f) => {
  if (!f) return;
  node.style.position = "absolute";
  node.style.left   = `${f.x}px`;
  node.style.top    = `${f.y}px`;
  node.style.width  = `${f.w}px`;
  node.style.height = `${f.h}px`;
};

// ---------------- bindings ----------------

const pluck = (obj, path) => {
  if (obj == null) return obj;
  if (!path) return obj;
  return path
    .split(/[.[\]]+/).filter(Boolean)
    .reduce((o, k) => (o == null ? o : o[k]), obj);
};

const formatValue = (v, fmt) => {
  if (v == null) return "";
  if (!fmt) return typeof v === "object" ? JSON.stringify(v) : String(v);
  if (typeof v !== "number") return String(v);
  const m = /%\.(\d+)f/.exec(fmt);
  if (m) return v.toFixed(parseInt(m[1], 10));
  return String(v);
};

const readControl = (node) => {
  if (node.tagName === "INPUT" && node.type === "checkbox") return node.checked;
  if ("value" in node) return node.value;
  return null;
};

const readBoundState = (node, el) => {
  // Most-recent read-binding topic value, if any; else the control's value.
  const r = el.binding && el.binding.read;
  if (!r) return readControl(node);
  const data = topic(r.topic).get();
  if (data == null) return null;
  return pluck(data, r.path);
};

const resolveSigil = (sigil, node, el) => {
  if (sigil === "$value") return readControl(node);
  if (sigil === "$state") {
    const v = readBoundState(node, el);
    return v == null ? readControl(node) : v;
  }
  if (sigil === "$not_state") {
    const v = readBoundState(node, el);
    return !(v == null ? readControl(node) : v);
  }
  return sigil;
};

const buildArgs = (writeSpec, node, el) => {
  const out = {};
  for (const [k, v] of Object.entries(writeSpec.args || {})) {
    out[k] = (typeof v === "string" && v.startsWith("$"))
      ? resolveSigil(v, node, el)
      : v;
  }
  return out;
};

const writeNodeValue = (node, val, fmt, label_map) => {
  let display = val;
  if (label_map) {
    const key = String(val);
    if (key in label_map) display = label_map[key];
  } else if (fmt) {
    display = formatValue(val, fmt);
  }
  if (node.tagName === "OUTPUT")  node.textContent = display ?? "";
  else if (node.tagName === "BUTTON") node.textContent = display ?? "";
  else if (node.tagName === "SELECT") node.value = (display ?? "");
  else if (node.tagName === "INPUT") {
    // Focus guard: don't clobber the user's in-progress edit.
    if (document.activeElement === node) return;
    node.value = display ?? "";
  } else {
    node.textContent = display ?? "";
  }
};

const applyBinding = (el, node) => {
  const b = el.binding;
  if (!b) return;

  if (b.write) {
    const handler = (event) => {
      cmd(b.write.cmd, buildArgs(b.write, node, el)).catch(() => {});
    };
    if (el.kind === "button") node.addEventListener("click", handler);
    else if (el.kind === "popup") node.addEventListener("change", handler);
    else if (el.kind === "textfield" && el.subkind === "input") {
      node.addEventListener("change", handler);            // commit on blur / Enter
    }
  }

  if (b.read) {
    const r = b.read;
    topic(r.topic).subscribe((data) => {
      const val = pluck(data, r.path);
      writeNodeValue(node, val, r.format, r.label_map);
    });
  }
};

// ---------------- go ----------------

fetch(LAYOUT_URL)
  .then((r) => {
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json();
  })
  .then(renderWindow)
  .catch((e) => {
    console.error(`renderer: failed to load ${LAYOUT_URL}:`, e);
    const root = document.getElementById("window-view");
    if (root) {
      root.innerHTML = "";
      const div = document.createElement("div");
      div.className = "placeholder";
      div.innerHTML =
        `<p>Layout not available for app "${APP}".</p>` +
        `<p class="hint muted">${LAYOUT_URL} could not be loaded: ${e.message}.</p>`;
      root.appendChild(div);
    }
  });
