// Window-view renderer. Builds an absolutely-positioned DOM tree from
// an xib2ir layout JSON and wires each element to WS topic stores +
// command dispatch via the shared ws.js plumbing.
//
// Multi-window: this module exports `mountRenderer(hostEl, layout)`
// which renders into the supplied host element and returns
// `{ destroy }`. window-host.js owns the host-element lifecycle and
// the per-app manifest loading. Multiple renderers can be mounted
// concurrently in sibling host divs (one per sub-tab); each tracks
// its own set of topic subscribers so a tab teardown only removes
// that tab's subscribers — others stay live.

import { topic, cmd } from "./ws.js";

// ---------------- stateless helpers ----------------

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

const positionAbs = (node, f, extraY = 0) => {
  if (!f) return;
  node.style.position = "absolute";
  node.style.left   = `${f.x}px`;
  node.style.top    = `${f.y + extraY}px`;
  node.style.width  = `${f.w}px`;
  node.style.height = `${f.h}px`;
};

const createNode = (el) => {
  switch (el.kind) {
    case "box": {
      const div = document.createElement("div");
      div.className = `wf-box wf-${el.subkind || "box"}`;
      if (el.title) {
        const lg = document.createElement("div");
        lg.className = "wf-legend";
        lg.textContent = el.title;
        div.appendChild(lg);
      }
      return div;
    }
    case "separator": {
      const div = document.createElement("div");
      div.className = "wf-separator";
      return div;
    }
    case "button": {
      if (el.subkind === "check") {
        const inp = document.createElement("input");
        inp.type = "checkbox";
        inp.className = "wf-checkbox";
        return inp;
      }
      const b = document.createElement("button");
      b.className = `wf-button wf-${el.subkind || "push"}`;
      b.textContent = el.title_default || "";
      return b;
    }
    case "popup": {
      const sel = document.createElement("select");
      sel.className = `wf-popup wf-${el.subkind || "popup"}`;
      const isPulldown = el.subkind === "pulldown";
      let firstOption = true;
      for (const opt of (el.options || [])) {
        const o = document.createElement("option");
        if (typeof opt === "object") {
          o.value = opt.value; o.textContent = opt.label ?? opt.value;
        } else {
          o.value = String(opt); o.textContent = String(opt);
        }
        // For NSPopUpButton pullsDown="YES", the first menu item is the
        // title row — non-selectable and only visible until state defines
        // a real position. Mark it disabled so the user can never pick it,
        // selected so it shows initially; once a state push sets
        // select.value to a real wire enum, the disabled placeholder is
        // automatically de-selected and hidden from the displayed value.
        if (isPulldown && firstOption) {
          o.disabled = true;
          o.selected = true;
        }
        sel.appendChild(o);
        firstOption = false;
      }
      if (!isPulldown && el.title_default != null) {
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
    case "indicator": {
      const span = document.createElement("span");
      span.className = `wf-indicator wf-${el.subkind || "indicator"}`;
      // Cocoa NSButton type=radio carries its label on the cell; show
      // it next to the colour dot (CSS provides the dot via ::before).
      if (el.title_default) span.textContent = el.title_default;
      return span;
    }
    case "progress": {
      if (el.subkind === "bar") {
        // Determinate progress bar — wrapper + inner fill div whose
        // width is driven from state (see writeBoundedFill). Bounds
        // (min/max) come from el.bounds at extract time.
        const wrap = document.createElement("div");
        wrap.className = "wf-progress wf-bar";
        const fill = document.createElement("div");
        fill.className = "wf-bar-fill";
        wrap.appendChild(fill);
        return wrap;
      }
      const span = document.createElement("span");
      span.className = `wf-progress wf-${el.subkind || "spinner"}`;
      return span;
    }
    case "levelindicator": {
      // NSLevelIndicator continuousCapacity — segmented bar with
      // green / yellow / red threshold zones. The fill width and a
      // .is-warn / .is-crit class are driven from state; the threshold
      // *values* (warning, critical) live in el.bounds and are baked
      // in at extract time (layout-time data, not runtime).
      const wrap = document.createElement("div");
      wrap.className = "wf-levelindicator";
      const fill = document.createElement("div");
      fill.className = "wf-level-fill";
      wrap.appendChild(fill);
      return wrap;
    }
    case "imageview": {
      const span = document.createElement("span");
      span.className = "wf-imageview";
      return span;
    }
    case "list": {
      // List/table: scrollable column of records, optionally rendered
      // as a multi-column <table> when `el.columns` is declared.
      // First consumer: PFS calTable in callist.xib (9 columns,
      // multi-select). See ws-migration-step0-pfs-camera.md
      // § Planned follow-on: calibration window.
      //
      // Topology: wrapper div (absolute-positioned by positionAbs)
      // → <table> with sticky <thead> (when header && columns) and
      // <tbody>. Rows are added/removed by the read-binding
      // subscriber; this only sets up the empty scaffold.
      const wrap = document.createElement("div");
      wrap.className = `wf-list wf-${el.subkind || "multi-select"}`;
      const table = document.createElement("table");
      table.className = "wf-list-table";
      if (el.header !== false && Array.isArray(el.columns) && el.columns.length) {
        const thead = document.createElement("thead");
        const tr = document.createElement("tr");
        for (const c of el.columns) {
          const th = document.createElement("th");
          th.textContent = c.title || c.id || "";
          th.dataset.col = c.id || "";
          if (c.width) th.style.width = `${c.width}px`;
          tr.appendChild(th);
        }
        thead.appendChild(tr);
        table.appendChild(thead);
      }
      const tbody = document.createElement("tbody");
      table.appendChild(tbody);
      wrap.appendChild(table);
      const empty = document.createElement("div");
      empty.className = "wf-list-empty";
      empty.textContent = el.empty_text || "no entries";
      wrap.appendChild(empty);
      return wrap;
    }
    default: {
      const div = document.createElement("div");
      div.className = "wf-unknown";
      div.textContent = `[${el.kind}/${el.subkind || ""}]`;
      return div;
    }
  }
};

// Rebuild a <select>'s option list from a topic-supplied array. Each
// entry is {name, encoder} (or {label, value}); the wire-side value is
// the encoder/value, the operator-facing string is the name/label.
// Used for runtime-populated pulldowns (PFS drop_slit / drop_cell /
// drop_hart, where the slot list comes from per-telescope XML files
// loaded into Cocoa's CameraController at windowDidLoad and shipped on
// the mechanics topic as slit_options / cell_options / hart_options).
const populateOptions = (selectNode, opts, isPulldown, placeholder) => {
  const currentVal = selectNode.value;
  selectNode.innerHTML = "";
  if (isPulldown) {
    const o = document.createElement("option");
    o.value = "_placeholder";
    o.textContent = placeholder || "?";
    o.disabled = true;
    o.selected = true;
    selectNode.appendChild(o);
  }
  for (const opt of opts) {
    const o = document.createElement("option");
    o.value = String(opt.encoder ?? opt.value ?? "");
    o.textContent = String(opt.name ?? opt.label ?? "");
    selectNode.appendChild(o);
  }
  // Restore the previously-selected value if it's still in range; the
  // subsequent writeNodeValue from the same state push will overwrite
  // it from the topic anyway, but this avoids a brief visual jump to
  // the placeholder on every options refresh.
  if (currentVal) selectNode.value = currentVal;
};

// Determinate-fill writer for progress/bar and levelindicator widgets.
// Both compute a 0..100 fill percentage from `val` and the element's
// declared bounds. Level indicators additionally swap a CSS class
// (is-warn / is-crit) when the value crosses the threshold values
// extracted from the XIB cell.
const writeBoundedFill = (node, val, el) => {
  const b = el.bounds || {};
  const min = (typeof b.min === "number") ? b.min : 0;
  const max = (typeof b.max === "number") ? b.max : 100;
  const v = (typeof val === "number") ? val : Number(val);
  const span = max - min;
  const pct = (Number.isFinite(v) && span > 0)
    ? Math.max(0, Math.min(100, ((v - min) / span) * 100))
    : 0;
  const fill = node.querySelector(el.kind === "levelindicator"
    ? ".wf-level-fill" : ".wf-bar-fill");
  if (fill) fill.style.width = pct.toFixed(2) + "%";

  if (el.kind === "levelindicator") {
    // Threshold colouring: critical wins, warning is the middle band,
    // ok is everything below warning. Each is a CSS class on the
    // wrapper so the fill colour comes from a single source.
    node.classList.remove("is-warn", "is-crit");
    if (typeof b.critical === "number" && v >= b.critical) {
      node.classList.add("is-crit");
    } else if (typeof b.warning === "number" && v >= b.warning) {
      node.classList.add("is-warn");
    }
  }
};

const writeNodeValue = (node, val, fmt, label_map, class_map) => {
  // class_map: state-driven CSS class swap (lamps, swatches, spinners, status icons).
  if (class_map) {
    for (const cls of Object.values(class_map)) node.classList.remove(cls);
    // Try the value as-is, plus bool↔int aliases. NSJSONSerialization
    // sometimes ships @(int_expr) booleans as JSON 1/0 rather than
    // true/false; mapping both forms here means YAML keyed on "true"/
    // "false" still hits when the wire payload arrives as 1/0.
    const candidates = [String(val)];
    if (val === true)  candidates.push("1");
    if (val === false) candidates.push("0");
    if (val === 1)     candidates.push("true");
    if (val === 0)     candidates.push("false");
    for (const key of candidates) {
      if (class_map[key]) { node.classList.add(class_map[key]); break; }
    }
    // class-only bindings don't write text — return early so we don't clobber a static title.
    if (!label_map && !fmt) return;
  }

  let display = val;
  if (label_map) {
    const key = String(val);
    if (key in label_map) display = label_map[key];
  } else if (fmt) {
    display = formatValue(val, fmt);
  }

  if (node.tagName === "OUTPUT")  node.textContent = display ?? "";
  else if (node.tagName === "BUTTON") node.textContent = display ?? "";
  // LOAD-BEARING: for SELECT we write `display` (post-label_map) into
  // node.value, NOT textContent. Some bindings.yml entries
  // (e.g. PFS popup_pmt) rely on this — they use label_map to remap
  // the incoming wire value (e.g. mechanics.pmt.position: "on") to
  // the option value (e.g. "true"). If you change this to textContent,
  // popup_pmt's read binding stops selecting the right option.
  else if (node.tagName === "SELECT") node.value = (display ?? "");
  else if (node.tagName === "INPUT") {
    if (node.type === "checkbox") { node.checked = !!val; return; }
    // Focus guard: don't clobber the user's in-progress edit.
    if (document.activeElement === node) return;
    node.value = display ?? "";
  } else {
    node.textContent = display ?? "";
  }
};

// Loose equality used by enabled_if so YAML boolean predicates still
// hit when the server sent the value as a JSON int / JSON string
// (NSJSONSerialization turns @(BOOL_expr) into 1/0 rather than
// true/false unless the call site uses ?@YES:@NO; older custom
// stacks ship bools as the literal strings "true"/"false"/"1"/"0").
// Mirrors the candidate set used by class_map in writeNodeValue.
const _eqLoose = (a, b) => {
  if (a === b) return true;
  // Bool ↔ int aliases.
  if (a === true  && b === 1) return true;
  if (b === true  && a === 1) return true;
  if (a === false && b === 0) return true;
  if (b === false && a === 0) return true;
  // Bool ↔ string aliases (defensive against non-NSJSON-style encoders).
  const trueish  = (v) => v === "true"  || v === "1";
  const falseish = (v) => v === "false" || v === "0";
  if (a === true  && trueish(b))  return true;
  if (b === true  && trueish(a))  return true;
  if (a === false && falseish(b)) return true;
  if (b === false && falseish(a)) return true;
  return false;
};

// ---------------- per-instance entry point ----------------

export const mountRenderer = (hostEl, layout) => {
  if (!hostEl) throw new Error("mountRenderer: hostEl required");

  // Subscribe handles produced during this render. Without retaining
  // the unsubscribe callbacks, a tab destroy / app switch would leak
  // each subscriber — they'd keep firing into detached DOM, growing
  // the topic-store subscriber set unbounded. destroy() drains the
  // list before clearing the host.
  const unsubs = [];
  const subscribeTracked = (topicName, cb) => {
    unsubs.push(topic(topicName).subscribe(cb));
  };

  // --- closures over hostEl + subscribeTracked ---

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
      // For controls that carry their own state (popups, checkboxes,
      // inputs), the user's current selection is the value to send.
      // For plain push buttons (no inherent toggle state), fall back to
      // the read binding's topic value if any. Without this distinction
      // a popup change would send the *previous* topic value instead of
      // the option the user just picked.
      const isPushButton = el.kind === "button" && el.subkind !== "check";
      if (!isPushButton) return readControl(node);
      const v = readBoundState(node, el);
      return v == null ? readControl(node) : v;
    }
    if (sigil === "$not_state") {
      const v = readBoundState(node, el);
      return !(v == null ? readControl(node) : v);
    }
    if (sigil === "$selection") {
      // List/table selection — the wrapper carries an internal
      // Set<int> on a non-enumerable property. Returns a plain
      // sorted array for the wire. Defined only on kind: list;
      // other widgets return null (and the renderer doesn't bind
      // $selection on them in practice).
      if (el.kind !== "list") return null;
      const sel = node._wfSelection;
      return sel ? [...sel].sort((a, b) => a - b) : [];
    }
    return sigil;
  };

  const buildArgs = (writeSpec, node, el) => {
    const out = {};
    for (const [k, v] of Object.entries(writeSpec.args || {})) {
      if (typeof v === "string" && v.startsWith("$")) {
        out[k] = resolveSigil(v, node, el);
      } else if (v && typeof v === "object"
                 && typeof v.topic === "string" && typeof v.path === "string") {
        // Topic-state lookup: read another outlet's current value via its
        // topic + path. Used when one control's write needs a sibling's
        // value — e.g., PFS's set_binning takes both axes, but the user
        // only twiddled one popup, so the other comes from readout state.
        const data = topic(v.topic).get();
        out[k] = data == null ? null : pluck(data, v.path);
      } else {
        out[k] = v;
      }
    }
    return out;
  };

  const applyListBinding = (el, node) => {
    // List/table widget — different shape from the generic read/write
    // path: `read` returns an array of records via `rows_path`, and
    // the optional `current_path` highlights one row. `write` fires
    // on selection change (debounced) rather than on click. Bound
    // sigil $selection resolves to the current row-index set.
    const b = el.binding;
    if (!b) {
      if (el.outlet) node.classList.add("wf-unbound");
      return;
    }
    if (b.ignore) return;
    const tbody = node.querySelector("tbody");
    const empty = node.querySelector(".wf-list-empty");
    if (!tbody || !empty) return;
    // Selection state lives on the wrapper as a non-enumerable Set<int>
    // so $selection in resolveSigil can read it; the DOM is the source
    // of truth for "what classes are on which row" via .is-selected.
    node._wfSelection = new Set();
    // Last anchor for shift-range — index of the most recent
    // single-click selection. Reset on row-count change or clear.
    node._wfAnchor = null;

    const cols = Array.isArray(el.columns) ? el.columns : null;
    // bindings.yml's read.column_paths is positional — entry i maps
    // to columns[i]. The IR's columns themselves carry only layout
    // (id, title, width) per the xib2ir contract; the path-to-record
    // mapping lives in the binding so the same generated layout can
    // drive different bindings without re-running the converter.
    const colPaths = (b.read && Array.isArray(b.read.column_paths))
                       ? b.read.column_paths : null;

    const renderRow = (entry, idx) => {
      const tr = document.createElement("tr");
      tr.dataset.index = String(idx);
      if (cols) {
        for (let i = 0; i < cols.length; i++) {
          const c = cols[i];
          const td = document.createElement("td");
          const path = (colPaths && colPaths[i]) || c.path || c.id;
          const v = pluck(entry, path);
          td.textContent = formatValue(v, c.format);
          tr.appendChild(td);
        }
      } else {
        const td = document.createElement("td");
        td.textContent = formatValue(entry);
        tr.appendChild(td);
      }
      return tr;
    };

    // Hash of the entries array — when this matches the last seen
    // signature, the read-subscriber skips a tbody rebuild (the
    // current_index flip can fire without rebuilding all rows).
    const entriesSig = (rows) => JSON.stringify(rows);

    let pendingFire = null;
    const fireSelectionChange = () => {
      if (pendingFire) clearTimeout(pendingFire);
      // 50 ms debounce so a Shift-range-click via click→mouseup
      // chain produces one cmd, not one per intermediate row. Same
      // value the plan calls out.
      pendingFire = setTimeout(() => {
        pendingFire = null;
        if (b.write) {
          cmd(b.write.cmd, buildArgs(b.write, node, el)).catch(() => {});
        }
      }, 50);
    };

    const setSelected = (indices) => {
      node._wfSelection = new Set(indices);
      for (const tr of tbody.querySelectorAll("tr")) {
        const idx = parseInt(tr.dataset.index, 10);
        tr.classList.toggle("is-selected", node._wfSelection.has(idx));
      }
    };

    // Selection handlers — applied to tbody (event delegation so a
    // rebuild doesn't need to re-attach per-row listeners).
    tbody.addEventListener("mousedown", (ev) => {
      const tr = ev.target.closest("tr[data-index]");
      if (!tr) return;
      const idx = parseInt(tr.dataset.index, 10);
      const isMulti = el.subkind !== "single-select" &&
                      el.subkind !== "readonly";
      if (el.subkind === "readonly") return;

      if (isMulti && (ev.metaKey || ev.ctrlKey)) {
        // Toggle this row.
        if (node._wfSelection.has(idx)) node._wfSelection.delete(idx);
        else                            node._wfSelection.add(idx);
        node._wfAnchor = idx;
      } else if (isMulti && ev.shiftKey && node._wfAnchor != null) {
        // Range from anchor to idx (inclusive). Replaces selection.
        const a = Math.min(node._wfAnchor, idx);
        const b2 = Math.max(node._wfAnchor, idx);
        node._wfSelection = new Set();
        for (let i = a; i <= b2; i++) node._wfSelection.add(i);
      } else {
        // Plain click — replace selection with this row.
        node._wfSelection = new Set([idx]);
        node._wfAnchor = idx;
      }
      // Repaint .is-selected classes.
      for (const row of tbody.querySelectorAll("tr")) {
        const i = parseInt(row.dataset.index, 10);
        row.classList.toggle("is-selected", node._wfSelection.has(i));
      }
      fireSelectionChange();
    });

    // Read binding: rebuild rows when entries[] signature changes,
    // always re-apply current_index highlight.
    if (b.read) {
      const r = b.read;
      subscribeTracked(r.topic, (data) => {
        const rows = pluck(data, r.rows_path) || [];
        const sig = entriesSig(rows);
        if (node.dataset.lastRowsSig !== sig) {
          tbody.innerHTML = "";
          for (let i = 0; i < rows.length; i++) {
            tbody.appendChild(renderRow(rows[i], i));
          }
          node.dataset.lastRowsSig = sig;
          // Row count or content changed — selection indices may no
          // longer be meaningful. Clear and notify the server so the
          // selected[] in the topic resets too.
          if (node._wfSelection && node._wfSelection.size > 0) {
            node._wfSelection = new Set();
            node._wfAnchor = null;
            fireSelectionChange();
          }
          empty.style.display = (rows.length === 0) ? "" : "none";
        }
        if (r.current_path) {
          const cur = pluck(data, r.current_path);
          for (const tr of tbody.querySelectorAll("tr")) {
            const idx = parseInt(tr.dataset.index, 10);
            tr.classList.toggle("is-running", idx === cur);
          }
        }
        // Honour a server-side selection if the SPA hasn't picked one
        // locally yet (first connect, or after a tab switch). Once the
        // local user picks rows we let the local set win.
        if (Array.isArray(r.selected_path ? pluck(data, r.selected_path) : null)
            && (!node._wfSelection || node._wfSelection.size === 0)) {
          const remote = pluck(data, r.selected_path);
          if (remote.length) setSelected(remote);
        }
      });
    }
  };

  const applyBinding = (el, node) => {
    if (el.kind === "list") return applyListBinding(el, node);
    const b = el.binding;
    if (!b) {
      // Unbound outlet — flag in dev-mode so layout drift (XIB outlet added
      // without a bindings.yml entry) is visible at a glance. Outlets in the
      // bindings.yml ignore_outlets list carry binding: { ignore: true } and
      // are filtered out by the caller (see below).
      if (el.outlet) node.classList.add("wf-unbound");
      return;
    }
    if (b.ignore) {
      // Intentionally unbound (ignore_outlets); don't warn, don't wire.
      return;
    }

    if (b.write) {
      const handler = (event) => {
        cmd(b.write.cmd, buildArgs(b.write, node, el)).catch(() => {});
        // Optimistic spinner: an outlet whose write spec names a sibling
        // outlet via `optimistic_spinner` gets that sibling's `is-moving`
        // class set immediately on cmd send. Cocoa's WSServer publishes
        // moving=true via a 1 Hz coalesce, so without this the user sees
        // "click → silence → spinner appears 1s later" for short moves.
        // The class is cleared by the spinner's own read binding on the
        // next state push that resolves the moving flag.
        //
        // Scope the selector to this renderer's host so a tab in
        // display:none doesn't accidentally win the lookup.
        const sp = b.write.optimistic_spinner;
        if (sp) {
          const target = hostEl.querySelector(
            `[data-outlet="${CSS.escape(sp)}"]`
          );
          if (target) target.classList.add("is-moving");
        }
      };
      if (el.kind === "button") {
        if (el.subkind === "check") node.addEventListener("change", handler);
        else                        node.addEventListener("click",  handler);
      }
      else if (el.kind === "popup") node.addEventListener("change", handler);
      else if (el.kind === "textfield" && el.subkind === "input") {
        node.addEventListener("change", handler);            // commit on blur / Enter
      }
    }

    if (b.read) {
      const r = b.read;
      subscribeTracked(r.topic, (data) => {
        // Dynamic options must run first — populateOptions may rewrite
        // the select's children, and writeNodeValue below sets
        // node.value, which only sticks if the matching <option> exists.
        // Used for popups whose option list is runtime data shipped on
        // the topic (PFS drop_slit / drop_cell / drop_hart loaded from
        // slits.xml / iodinecell.xml / hartmann.xml at windowDidLoad).
        if (r.options_path) {
          const opts = pluck(data, r.options_path);
          if (Array.isArray(opts) && opts.length > 0) {
            const sig = JSON.stringify(opts);
            if (node.dataset.lastOptsSig !== sig) {
              populateOptions(node, opts, el.subkind === "pulldown",
                              el.title_default);
              node.dataset.lastOptsSig = sig;
            }
          }
        }

        const val = pluck(data, r.path);
        // Bounded-fill widgets (determinate progress bar, level indicator)
        // take a dedicated writer because their value is a numeric ratio
        // against el.bounds, not a label/text/class swap.
        if (el.kind === "levelindicator" ||
            (el.kind === "progress" && el.subkind === "bar")) {
          writeBoundedFill(node, val, el);
        } else {
          writeNodeValue(node, val, r.format, r.label_map, r.class_map);
        }
        // Optional sibling-path that drives the element's text content
        // independently of `path`. Used for state-driven labels — lamp
        // button names ("ThAr"/"Ne"/...) and the var-quartz indicator
        // text ("Var.Q"/"Cal") that the Cocoa controller swaps at
        // runtime from the per-telescope XML resource. The web SPA
        // expects the WSServer to publish these in the topic snapshot.
        if (r.label_path) {
          const labelVal = pluck(data, r.label_path);
          if (labelVal != null) {
            if (node.tagName === "BUTTON" || node.tagName === "OUTPUT" ||
                node.tagName === "SPAN")  node.textContent = String(labelVal);
          }
        }
      });
    }

    // Conditional visibility: subscribe to a topic path and hide the
    // element when its value matches the supplied predicate. Mirrors the
    // Cocoa setHidden: calls in DCUcontroller (e.g. var-quartz
    // controls hidden when varLamp_Name is "-"). Predicate forms:
    //   { equals: <value> }   hide when path === value
    //   { absent: true }      hide when path is null/undefined
    if (b.hidden_if) {
      const h = b.hidden_if;
      subscribeTracked(h.topic, (data) => {
        const val = pluck(data, h.path);
        let hide = false;
        if ("equals" in h) hide = (val === h.equals);
        else if (h.absent === true) hide = (val == null);
        node.classList.toggle("wf-hidden", hide);
      });
    }

    // Conditional enable: subscribe to a topic path (or several) and
    // toggle the control's `disabled` attribute. Single-condition form
    // is { topic, path, equals|not_equals|absent|present }. Multi-
    // condition form is { all: [<cond>, ...] } — every condition must
    // hold for the control to be enabled. Used for cross-topic
    // interlocks (DCU FFS / Mcal brake gating, where drop_ffs needs
    // mcal.brake==true AND mcal.position=="out" AND neither side
    // currently moving).
    if (b.enabled_if) {
      const e = b.enabled_if;
      const evalCond = (c) => {
        const data = topic(c.topic).get();
        if (data == null) return false;
        const val = pluck(data, c.path);
        if      ("equals"     in c) return  _eqLoose(val, c.equals);
        else if ("not_equals" in c) return !_eqLoose(val, c.not_equals);
        else if ( c.absent === true) return (val == null);
        else if ( c.present === true) return (val != null);
        return true;
      };
      if (Array.isArray(e.all)) {
        const recompute = () => { node.disabled = !e.all.every(evalCond); };
        const seen = new Set();
        for (const c of e.all) {
          if (typeof c.topic === "string" && !seen.has(c.topic)) {
            seen.add(c.topic);
            subscribeTracked(c.topic, recompute);
          }
        }
      } else {
        subscribeTracked(e.topic, () => { node.disabled = !evalCond(e); });
      }
    }
  };

  const renderEl = (el, childrenOf, yShift) => {
    const node = createNode(el);
    const extraY = (yShift && yShift.get(el.id)) || 0;
    positionAbs(node, el.frame, extraY);
    node.dataset.outlet = el.outlet || "";
    node.dataset.id = el.id;

    // Only top-level shifts apply to box wrappers themselves; children
    // are positioned relative to their parent box and ride along with
    // the shift automatically, so we don't propagate yShift into the
    // recursion.
    for (const child of (childrenOf.get(el.id) || [])) {
      // yShift is intentionally null at the recursive call — only the
      // top-level boxes get the inter-box-gap shift; children are
      // positioned relative to their parent and ride along automatically.
      node.appendChild(renderEl(child, childrenOf, null));
    }

    applyBinding(el, node);
    return node;
  };

  // --- build DOM into hostEl ---

  hostEl.innerHTML = "";

  // Inflate inter-box gaps when sibling boxes-with-titles sit too
  // close. The .wf-legend pokes 8 px above its box (CSS top:-8px) to
  // mimic Cocoa's notched-border title look; if the previous box's
  // bottom is closer than that, the legend overlaps the previous
  // box's last row of content. PFS stacks its top-level boxes with
  // ~2 px gaps; ADC/DCU have ~20 px gaps already and get the no-op
  // path below.
  // MIN_GAP > LEGEND_PROTRUSION (currently 8px in style.css's .wf-legend
  // top:-8px) so the protruding label has clear space above its own box.
  const MIN_GAP = 12;
  const yShift = new Map();   // element id → cumulative y shift to apply
  const topTitledBoxes = layout.elements
    .filter(e => e.parent_id === null && e.kind === "box" && e.title)
    .sort((a, b) => a.frame.y - b.frame.y);
  let totalShift = 0;
  for (let i = 1; i < topTitledBoxes.length; i++) {
    const prev = topTitledBoxes[i - 1];
    const cur  = topTitledBoxes[i];
    const prevBottom = prev.frame.y + prev.frame.h + (yShift.get(prev.id) || 0);
    const curTop     = cur.frame.y + totalShift;
    const gap = curTop - prevBottom;
    if (gap < MIN_GAP) {
      const add = MIN_GAP - gap;
      totalShift += add;
    }
    if (totalShift > 0) yShift.set(cur.id, totalShift);
  }

  // Wrapper absorbs the scaled dimensions so neighbours (header, log) flow
  // around the scaled frame instead of overlapping it. The wrap's box
  // dimensions are scale × natural frame size; the frame inside is
  // transformed but keeps its natural width/height for absolute children.
  const wrap = document.createElement("div");
  wrap.className = "window-scale-wrap";
  wrap.style.setProperty("--frame-w", `${layout.window.width}px`);
  wrap.style.setProperty("--frame-h", `${layout.window.height + totalShift}px`);
  hostEl.appendChild(wrap);

  const frame = document.createElement("div");
  frame.className = "window-frame";
  frame.style.width  = `${layout.window.width}px`;
  frame.style.height = `${layout.window.height + totalShift}px`;
  if (layout.window.title) frame.dataset.title = layout.window.title;
  wrap.appendChild(frame);

  // Bucket elements by parent_id for tree reconstruction.
  const childrenOf = new Map();
  for (const el of layout.elements) {
    const list = childrenOf.get(el.parent_id) ?? [];
    list.push(el);
    childrenOf.set(el.parent_id, list);
  }

  for (const el of (childrenOf.get(null) || [])) {
    frame.appendChild(renderEl(el, childrenOf, yShift));
  }

  // --- destroy handle ---

  return {
    destroy() {
      for (const u of unsubs) {
        try { u(); } catch (e) { console.error("renderer: unsub failed", e); }
      }
      unsubs.length = 0;
      hostEl.innerHTML = "";
    },
  };
};
