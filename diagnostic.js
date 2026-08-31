// Diagnostic view: topic dump + APP_REGISTRY-driven command form + log pane.
// Generic surface for raw-protocol exploration; remains available alongside
// the position-faithful main-window view (renderer.js — future phase).
//
// Usage: open in browser with ?host=&port= query params, e.g.
//        app.html?host=localhost&port=52403

import {
  url, connect, cmd, subscribe,
  onHello, onState, onEvent, onAck, onSend, onConn, onLog,
} from "./ws.js";

// Reserved topic — see ws-ui-conversion-plan.md § Streamed log topic.
// Subscribe is implicit (advertised in hello.topics), but the diagnostic
// pane treats the topic specially: state frames carry a ring-buffer
// replay ({entries: [...]}) and live updates arrive as event frames.
const LOGS_TOPIC = "logs";
const LOG_MAX_DOM_ENTRIES = 500;

// ---------------- per-app registry ----------------

const APP_REGISTRY = {
  ADC: {
    commands: [
      { name: "version" },
      { name: "set_adc_insertion",     args: [ { name: "pos",      kind: "select", options: ["in", "out"] } ] },
      { name: "set_other_insertion",   args: [ { name: "pos",      kind: "select", options: ["in", "out"] } ] },
      { name: "set_control_mode",      args: [ { name: "mode",     kind: "select", options: ["auto", "off"] } ] },
      { name: "set_auto_update",       args: [ { name: "enabled",  kind: "bool" } ] },
      { name: "trigger_manual_update" },
      { name: "set_running",           args: [ { name: "running",  kind: "bool" } ] },
      { name: "move_lens",             args: [ { name: "lens",     kind: "select", options: ["A", "B"] },
                                               { name: "angle",    kind: "number", step: 0.1 } ] },
    ]
  },
  DCU: {
    commands: [
      { name: "version" },
      { name: "set_lamp",              args: [ { name: "idx",      kind: "number", min: 1, max: 8, default: 1 },
                                               { name: "on",       kind: "bool" } ] },
      { name: "set_quartz_level",      args: [ { name: "level",    kind: "number", min: 0, default: 0 } ] },
      { name: "set_ffs_position",      args: [ { name: "pos",      kind: "select", options: ["in", "out"] } ] },
      { name: "set_ffs_brake",         args: [ { name: "on",       kind: "bool" } ] },
      { name: "set_mcal_position",     args: [ { name: "pos",      kind: "select", options: ["in", "out"] } ] },
      { name: "set_mcal_brake",        args: [ { name: "on",       kind: "bool" } ] },
    ]
  },
};

// ---------------- DOM helpers ----------------

const $ = (id) => document.getElementById(id);
const el = (tag, attrs = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children) {
    if (c == null) continue;
    node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return node;
};

// ---------------- header / connection state ----------------

$("conn-url").textContent = url;

const setConn = (state, cls) => {
  $("conn-state").textContent = state;
  $("conn-dot").className = "dot " + cls;
};

onConn(setConn);

// ---------------- log ----------------

const logEl = $("log");
$("log-clear").addEventListener("click", () => { logEl.innerHTML = ""; });

// SPA-internal entries (sent frames, transport-layer messages) keep the
// old shape: simple `<li class="send|ok|err|evt">` with a leading ts.
const log = (cls, ...parts) => {
  const ts = new Date().toLocaleTimeString();
  const li = el("li", { class: cls, "data-kind": "spa" });
  li.appendChild(el("span", { class: "ts", text: ts }));
  li.appendChild(document.createTextNode(parts.map(p =>
    typeof p === "string" ? p : JSON.stringify(p)
  ).join(" ")));
  insertLogEntry(li);
};

onSend(frame => log("send", "→", frame));
onLog((level, ...parts) => log(level, ...parts));

// Server log entries arrive via the `logs` topic. Replay (state frame
// with {entries: [...]}) populates the pane on subscribe; live updates
// (event frames) append a single entry each.
//
// Render format: `<HH:MM:SS> [SRC] msg` with the entire <li> carrying
// `data-level="info|warn|…"` so the level filter can hide rows via
// a single CSS rule on the parent <ol>.
const formatLogTs = (iso) => {
  if (!iso) return "";
  // Server emits ISO-8601 UTC with ms. Convert to LOCAL time for
  // display so server rows line up with the SPA-internal rows
  // (which use toLocaleTimeString). A previous revision regexed
  // HH:MM:SS straight out of the ISO string — that showed UTC next
  // to local timestamps and read as a clock inconsistency
  // ("00:06:07" beside "5:06:09 PM" for the same instant).
  const d = new Date(iso);
  if (!Number.isNaN(d.getTime())) return d.toLocaleTimeString();
  // Unparseable ts (shouldn't happen with a compliant server):
  // fall back to the raw HH:MM:SS slice rather than hiding the row.
  const m = /T(\d{2}:\d{2}:\d{2})/.exec(iso);
  return m ? m[1] : iso;
};

const appendServerLogEntry = (entry) => {
  if (!entry) return;
  // Defensive coercion: a future intermediary (rsyslog proxy etc.) or
  // a buggy emitter could ship `level` as an integer, `ts` as a Date,
  // etc. We never want a stringifying error to take out the pane.
  const level = String(entry.level ?? "info").toLowerCase();
  const ts    = typeof entry.ts === "string" ? entry.ts : "";
  const src   = entry.src == null ? "" : String(entry.src);
  const msg   = entry.msg == null ? "" : String(entry.msg);
  const li = el("li", { class: `log-server log-level-${level}`,
                        "data-kind": "server",
                        "data-level": level });
  li.appendChild(el("span", { class: "ts", text: formatLogTs(ts) }));
  if (src) li.appendChild(el("span", { class: "src", text: `[${src}]` }));
  li.appendChild(document.createTextNode(" " + msg));
  insertLogEntry(li);
};

// Newest-on-top to match the existing SPA-internal entry behaviour.
// Cap DOM size at LOG_MAX_DOM_ENTRIES so a long-running session can't
// blow up memory with thousands of <li>s.
const insertLogEntry = (li) => {
  logEl.insertBefore(li, logEl.firstChild);
  while (logEl.children.length > LOG_MAX_DOM_ENTRIES) {
    logEl.removeChild(logEl.lastChild);
  }
};

// Level filter — selecting `warn+` hides any <li data-level> below
// the threshold. SPA-internal entries (data-kind="spa") are always
// shown; they're transport-debug, not graded log severity. The
// thresholding is done in CSS (style.css ol#log[data-min-level=…])
// so this side just stamps the chosen min onto the <ol>.
const applyLevelFilter = (min) => {
  logEl.setAttribute("data-min-level", min);
};
const levelSelect = $("log-level");
if (levelSelect) {
  levelSelect.addEventListener("change", () => applyLevelFilter(levelSelect.value));
  applyLevelFilter(levelSelect.value || "warn");
}

// ---------------- topic UI ----------------

const topicNodes = new Map();   // topic -> {section, body}

const buildTopics = (topics) => {
  const root = $("topics");
  root.innerHTML = "";
  topicNodes.clear();
  for (const t of topics) {
    // `logs` has its own pane (#log) and a different schema; skip the
    // generic key/value renderer.
    if (t === LOGS_TOPIC) continue;
    const body = el("div", { class: "kv", text: "(no data yet)" });
    const sec = el("div", { class: "topic" },
      el("div", { class: "topic-head" },
        el("strong", { text: t }),
        el("span", { text: "live" })),
      body);
    root.appendChild(sec);
    topicNodes.set(t, { section: sec, body });
  }
};

const formatValue = (v) => {
  if (v == null) return "—";
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "number")  return Number.isInteger(v) ? String(v) : v.toFixed(3);
  if (Array.isArray(v))       return "[" + v.map(formatValue).join(", ") + "]";
  if (typeof v === "object")  return "{" + Object.entries(v)
    .map(([k, val]) => `${k}:${formatValue(val)}`).join(", ") + "}";
  return String(v);
};

const renderObject = (obj) => {
  const grid = el("div", { class: "kv" });
  if (obj == null || typeof obj !== "object") {
    grid.appendChild(el("span", { class: "k", text: "value" }));
    grid.appendChild(el("span", { class: "v", text: String(obj) }));
    return grid;
  }
  for (const [k, v] of Object.entries(obj)) {
    grid.appendChild(el("span", { class: "k", text: k }));
    if (v !== null && typeof v === "object" && !Array.isArray(v)) {
      const wrap = el("div", { class: "v" });
      const nested = renderObject(v);
      nested.classList.add("nested");
      wrap.appendChild(nested);
      grid.appendChild(wrap);
    } else {
      grid.appendChild(el("span", { class: "v", text: formatValue(v) }));
    }
  }
  return grid;
};

onState((msg) => {
  if (msg.topic === LOGS_TOPIC) {
    // Ring-buffer replay on subscribe. Entries are oldest-first; we
    // insert each at the TOP of the list so the newest one ends up
    // visually at the top after the loop completes.
    //
    // Auto-reconnect (ws.js) will re-fire onHello → resubscribe →
    // server resends the full ring. To avoid duplicating the previous
    // replay's contents into the pane, drop the existing server rows
    // first. SPA-internal rows (data-kind="spa") survive — they're
    // transport debug and not redelivered by the server.
    for (const n of logEl.querySelectorAll('li[data-kind="server"]')) n.remove();
    const entries = (msg.data && Array.isArray(msg.data.entries)) ? msg.data.entries : [];
    for (const e of entries) appendServerLogEntry(e);
    return;
  }
  const node = topicNodes.get(msg.topic);
  if (!node) return;
  node.body.innerHTML = "";
  node.body.appendChild(renderObject(msg.data));
});

// ---------------- commands UI ----------------

const buildArgInput = (a) => {
  if (a.kind === "select") {
    const sel = el("select");
    for (const o of a.options) {
      const opt = el("option", { value: o, text: o });
      if (o === a.default) opt.selected = true;
      sel.appendChild(opt);
    }
    return sel;
  }
  if (a.kind === "bool") {
    const sel = el("select");
    sel.appendChild(el("option", { value: "true",  text: "true" }));
    sel.appendChild(el("option", { value: "false", text: "false" }));
    sel.value = a.default === false ? "false" : "true";
    return sel;
  }
  if (a.kind === "number") {
    const inp = el("input", { type: "number" });
    if (a.step != null) inp.step = String(a.step);
    if (a.min  != null) inp.min  = String(a.min);
    if (a.max  != null) inp.max  = String(a.max);
    inp.value = a.default != null ? String(a.default) : "";
    return inp;
  }
  return el("input", { type: "text", value: a.default ?? "" });
};

const readArg = (spec, input) => {
  if (spec.kind === "bool")   return input.value === "true";
  if (spec.kind === "number") return Number(input.value);
  return input.value;
};

const buildCommandRow = (c) => {
  const row = el("div", { class: "cmd-row" });
  row.appendChild(el("span", { class: "cmd-name", text: c.name }));
  const argsEl = el("div", { class: "cmd-args" });
  const inputs = (c.args || []).map(a => {
    const input = buildArgInput(a);
    argsEl.appendChild(el("label", { class: "muted", text: a.name + ":" }));
    argsEl.appendChild(input);
    return { spec: a, input };
  });
  row.appendChild(argsEl);
  row.appendChild(el("button", { class: "run",
    onclick: () => {
      const args = {};
      for (const { spec, input } of inputs) args[spec.name] = readArg(spec, input);
      cmd(c.name, args).catch(() => {});
    },
    text: "run"
  }));
  return row;
};

const buildCommands = (appName) => {
  const root = $("commands");
  root.innerHTML = "";
  const reg = APP_REGISTRY[appName];
  if (!reg) {
    root.appendChild(el("div", { class: "muted",
      text: `No command registry for app "${appName}". Edit APP_REGISTRY in diagnostic.js to add one.` }));
    return;
  }
  for (const c of reg.commands) {
    root.appendChild(buildCommandRow(c));
  }
};

// ---------------- hello / events / acks ----------------

onHello((msg) => {
  setConn(`connected · protocol v${msg.protocol_version}`, "ok");
  const app  = msg.app     || "(unknown)";
  const ver  = msg.version || "?";
  const bld  = msg.build   || "?";
  $("app-name").textContent    = app;
  $("app-version").textContent = `v${ver}  (${bld})`;
  // Reflect the same identity in the browser tab title so multi-window
  // operators can tell ADC/DCU/etc. apart at a glance.
  document.title = `${app} v${ver} (${bld})`;
  log("ok", "hello", msg);

  buildTopics(msg.topics || []);
  buildCommands(msg.app);

  if (msg.topics && msg.topics.length) subscribe(...msg.topics);
});

onEvent((msg) => {
  if (msg.topic === LOGS_TOPIC) {
    appendServerLogEntry(msg.data);
    return;
  }
  // Lifecycle events (PFS exposure_started, exposure_complete, …) keep
  // the legacy compact rendering — these are rare and structurally
  // distinct from log entries, so they live alongside in the same pane
  // as data-kind="spa" SPA-internal rows.
  log("evt", "event", msg.name || msg.topic, msg.data || {});
});

onAck((msg) => {
  if (msg.ok) log("ok", "✓", msg.id, msg.result || {});
  else        log("err", "✗", msg.id, msg.error || {});
});

// ---------------- go ----------------

connect();
