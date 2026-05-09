// WebSocket transport — shared across diagnostic and main-window views.
// Pure protocol plumbing; no DOM. Consumers import the named bindings below.
//
// Protocol per ws-migration-plan.md: JSON frames with `type` discriminator
// (hello / state / event / ack), client subscribes to topics and dispatches
// commands by id.

const params = new URLSearchParams(window.location.search);
const host = params.get("host") || "localhost";
const port = parseInt(params.get("port") || "52403", 10);
export const url = `ws://${host}:${port}/`;

let ws = null;
let reconnectTimer = null;
let cmdCounter = 0;
const pendingAcks = new Map();           // cmd id -> { resolve, reject }

const helloListeners = [];
const stateListeners = [];
const eventListeners = [];
const ackListeners   = [];
const sendListeners  = [];
const connListeners  = [];
const logListeners   = [];

const fire = (list, ...args) => {
  for (const cb of list) {
    try { cb(...args); } catch (e) { console.error(e); }
  }
};

export const onHello = (cb) => helloListeners.push(cb);
export const onState = (cb) => stateListeners.push(cb);
export const onEvent = (cb) => eventListeners.push(cb);
export const onAck   = (cb) => ackListeners.push(cb);
export const onSend  = (cb) => sendListeners.push(cb);    // (frame)
export const onConn  = (cb) => connListeners.push(cb);    // (state, cls)
export const onLog   = (cb) => logListeners.push(cb);     // (level, ...parts)

// Topic stores: latest value per topic + subscribers. Renderer-friendly.
const topicValues = new Map();
const topicSubs   = new Map();

export const topic = (name) => {
  if (!topicSubs.has(name)) topicSubs.set(name, new Set());
  const subs = topicSubs.get(name);
  return {
    get: () => topicValues.get(name),
    subscribe: (cb) => {
      subs.add(cb);
      if (topicValues.has(name)) {
        try { cb(topicValues.get(name)); } catch (e) { console.error(e); }
      }
      return () => subs.delete(cb);
    },
  };
};

export const send = (frame) => {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    fire(logListeners, "err", "send while not open:", frame);
    return;
  }
  ws.send(JSON.stringify(frame));
  fire(sendListeners, frame);
};

export const cmd = (name, args = {}) => {
  const id = `c${++cmdCounter}`;
  return new Promise((resolve, reject) => {
    pendingAcks.set(id, { resolve, reject });
    send({ type: "cmd", id, name, args });
    setTimeout(() => {
      if (pendingAcks.delete(id)) reject(new Error(`ack timeout: ${name}`));
    }, 10000);
  });
};

export const subscribe = (...topics) => send({ type: "subscribe", topics });

const dispatch = (msg) => {
  switch (msg.type) {
    case "hello":
      fire(helloListeners, msg);
      break;
    case "state": {
      topicValues.set(msg.topic, msg.data);
      const subs = topicSubs.get(msg.topic);
      if (subs) for (const cb of subs) {
        try { cb(msg.data); } catch (e) { console.error(e); }
      }
      fire(stateListeners, msg);
      break;
    }
    case "event":
      fire(eventListeners, msg);
      break;
    case "ack": {
      const p = pendingAcks.get(msg.id);
      if (msg.ok) { if (p) p.resolve(msg.result); }
      else        { if (p) p.reject(msg.error); }
      pendingAcks.delete(msg.id);
      fire(ackListeners, msg);
      break;
    }
    default:
      fire(logListeners, "err", "unknown frame type:", msg);
  }
};

export const connect = () => {
  fire(connListeners, "connecting…", "warn");
  ws = new WebSocket(url);

  ws.addEventListener("open", () => {
    fire(connListeners, "waiting for hello…", "warn");
  });

  ws.addEventListener("message", (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); }
    catch { fire(logListeners, "err", "non-json frame:", ev.data); return; }
    dispatch(msg);
  });

  ws.addEventListener("close", () => {
    fire(connListeners, "disconnected", "err");
    for (const [, { reject }] of pendingAcks) reject(new Error("connection closed"));
    pendingAcks.clear();
    if (!reconnectTimer) {
      reconnectTimer = setTimeout(() => { reconnectTimer = null; connect(); }, 2000);
    }
  });

  ws.addEventListener("error", () => {
    fire(connListeners, "error", "err");
  });
};
