// LCO instrument web SPA — vanilla JS, no build step.
// Connects to a Cocoa app's WSServer (port = 50001 + PROJECT_ID*100 + 2)
// and renders topic snapshots + command forms per the v1 protocol.
//
// Usage: open in browser with ?host=&port= query params, e.g.
//        index.html?host=localhost&port=52403

(() => {
  "use strict";

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

  // ---------------- URL params ----------------

  const params = new URLSearchParams(window.location.search);
  const host = params.get("host") || "localhost";
  const port = parseInt(params.get("port") || "52403", 10);
  const url  = `ws://${host}:${port}/`;
  $("conn-url").textContent = url;

  // ---------------- log ----------------

  const logEl = $("log");
  $("log-clear").addEventListener("click", () => { logEl.innerHTML = ""; });
  const log = (cls, ...parts) => {
    const ts = new Date().toLocaleTimeString();
    const li = el("li", { class: cls });
    li.appendChild(el("span", { class: "ts", text: ts }));
    li.appendChild(document.createTextNode(parts.map(p =>
      typeof p === "string" ? p : JSON.stringify(p)
    ).join(" ")));
    logEl.insertBefore(li, logEl.firstChild);
    while (logEl.children.length > 200) logEl.removeChild(logEl.lastChild);
  };

  // ---------------- WS plumbing ----------------

  let ws = null;
  let pendingAcks = new Map();           // cmd id -> { resolve, reject }
  let helloReceived = false;
  let reconnectTimer = null;

  const setConn = (state, cls) => {
    $("conn-state").textContent = state;
    $("conn-dot").className = "dot " + cls;
  };

  const connect = () => {
    setConn("connecting…", "warn");
    ws = new WebSocket(url);

    ws.addEventListener("open", () => {
      setConn("waiting for hello…", "warn");
    });

    ws.addEventListener("message", (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); }
      catch { log("err", "non-json frame:", ev.data); return; }
      dispatch(msg);
    });

    ws.addEventListener("close", () => {
      setConn("disconnected", "err");
      // Reject all pending acks.
      for (const [, { reject }] of pendingAcks) reject(new Error("connection closed"));
      pendingAcks.clear();
      helloReceived = false;
      // Reconnect after a short delay.
      if (!reconnectTimer) {
        reconnectTimer = setTimeout(() => { reconnectTimer = null; connect(); }, 2000);
      }
    });

    ws.addEventListener("error", () => {
      setConn("error", "err");
    });
  };

  const send = (frame) => {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      log("err", "send while not open:", frame);
      return;
    }
    ws.send(JSON.stringify(frame));
    log("send", "→", frame);
  };

  let cmdCounter = 0;
  const cmd = (name, args = {}) => {
    const id = `c${++cmdCounter}`;
    return new Promise((resolve, reject) => {
      pendingAcks.set(id, { resolve, reject });
      send({ type: "cmd", id, name, args });
      setTimeout(() => {
        if (pendingAcks.delete(id)) reject(new Error(`ack timeout: ${name}`));
      }, 10000);
    });
  };

  const subscribe = (...topics) => send({ type: "subscribe", topics });

  // ---------------- dispatch ----------------

  const topicNodes = new Map();   // topic -> {section, body}

  const dispatch = (msg) => {
    switch (msg.type) {
      case "hello":     onHello(msg); break;
      case "state":     onState(msg); break;
      case "event":     onEvent(msg); break;
      case "ack":       onAck(msg); break;
      default:          log("err", "unknown frame type:", msg);
    }
  };

  const onHello = (msg) => {
    helloReceived = true;
    setConn(`connected · protocol v${msg.protocol_version}`, "ok");
    $("app-name").textContent = msg.app || "(unknown)";
    $("app-version").textContent = `${msg.version || "?"}  (${msg.build || "?"})`;
    log("ok", "hello", msg);

    buildTopics(msg.topics || []);
    buildCommands(msg.app);

    // Auto-subscribe to all advertised topics.
    if (msg.topics && msg.topics.length) subscribe(...msg.topics);
  };

  const onState = (msg) => {
    const node = topicNodes.get(msg.topic);
    if (!node) return;
    node.body.innerHTML = "";
    node.body.appendChild(renderObject(msg.data));
  };

  const onEvent = (msg) => log("evt", "event", msg.name, msg.data || {});

  const onAck = (msg) => {
    const p = pendingAcks.get(msg.id);
    if (msg.ok) {
      log("ok", "✓", msg.id, msg.result || {});
      if (p) p.resolve(msg.result);
    } else {
      log("err", "✗", msg.id, msg.error || {});
      if (p) p.reject(msg.error);
    }
    pendingAcks.delete(msg.id);
  };

  // ---------------- topic UI ----------------

  const buildTopics = (topics) => {
    const root = $("topics");
    root.innerHTML = "";
    topicNodes.clear();
    for (const t of topics) {
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

  const formatValue = (v) => {
    if (v == null) return "—";
    if (typeof v === "boolean") return v ? "true" : "false";
    if (typeof v === "number")  return Number.isInteger(v) ? String(v) : v.toFixed(3);
    if (Array.isArray(v))       return "[" + v.map(formatValue).join(", ") + "]";
    return String(v);
  };

  // ---------------- commands UI ----------------

  const buildCommands = (appName) => {
    const root = $("commands");
    root.innerHTML = "";
    const reg = APP_REGISTRY[appName];
    if (!reg) {
      root.appendChild(el("div", { class: "muted",
        text: `No command registry for app "${appName}". Edit APP_REGISTRY in app.js to add one.` }));
      return;
    }
    for (const c of reg.commands) {
      root.appendChild(buildCommandRow(c));
    }
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

  // ---------------- go ----------------

  connect();
})();
