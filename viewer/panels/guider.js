// The guider panel: gcam's state, shown verbatim.
//
// Every number here is a FITS card gcam served with the frame, printed under the card's own name
// and gcam's own comment. Nothing is computed from them — no unit conversion, no derived
// position, no health score. What the panel decides is only *presentation*: which cards to
// show, in which group, and whether to grey a group whose served flag says it is not updating.
// If a quantity is missing, that is an issue for zwogcam, not arithmetic here. See
// docs/plans/gcam-web-viewer.md, "display verbatim, derive nothing".
//
// `mountPanel(root)` fills the element and returns `update({ meta, status, ageS })`, `meta` being the
// frame header's `guider` object. The same signature as panels/header.js: app.js picks one by page kind.
//
// Restored verbatim from zwo 1f47b40 (src/web/gcamweb/static/guider-panel.js), which gcamweb dropped
// when it became a proxy alone; see docs/plans/guider-viewer-plan.md.

/// Groups of cards, in display order. The labels come from the served card comments; these are
/// only the keys and the grouping.
const GROUPS = [
  ["guider", ["GDGUIDE", "GDLOOP", "GDINIT", "GDMODE", "GDFMODE", "GDMMODE", "GDSENS", "GDPA"]],
  ["exposure", ["EXPTIME", "GAIN", "CCDOFFS", "GDAVG", "BINNING", "CAMFPS", "GDFPS"]],
  ["measured", ["GDFWHM", "GDFLUX", "GDPEAK", "GDBACK", "GDDX", "GDDY"]],
  ["corrections", ["GDAZ", "GDEL"]],
  ["box", ["GDBOXX", "GDBOXY", "GDBOXSZ"]],
  ["camera", ["TEMPCCD", "TEMPSET", "COOLER", "SEQ-NUM", "FRAMETS"]],
  ["telescope", ["RA", "DEC", "AIRMASS", "ZD", "TELFOCUS", "ROTANGLE"]],
];

/// The strip charts: the four plots gcam's own X11 window keeps. History lives in the page.
const SPARKS = ["GDFLUX", "GDFWHM", "GDAZ", "GDEL"];
const HISTORY = 300;

/// Groups greyed when GDGUIDE is 0: gcam only updates these while guiding, and the served flag says
/// so. That is a display rule keyed on a served value, not a derivation.
const GUIDING_ONLY = new Set(["measured", "corrections"]);

const fmt = (v) => {
  if (v === null || v === undefined) return "—";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toFixed(Math.abs(v) < 10 ? 3 : 2);
  return String(v);
};

export function mountPanel(root) {
  root.innerHTML = `
    <header><b>guider</b><span id="gd-state">—</span></header>
    <p class="banner" id="gd-banner" hidden></p>
    <div id="gd-groups"></div>
    <h4>history</h4>
    <div class="spark" id="gd-spark"></div>`;
  const state = root.querySelector("#gd-state");
  const banner = root.querySelector("#gd-banner");
  const groupsEl = root.querySelector("#gd-groups");
  const sparkEl = root.querySelector("#gd-spark");

  // One <dl> per group, cells created once and written in place — the panel updates at frame rate.
  const cells = new Map(); // key -> { dt, v, c }
  const groupEls = new Map();
  for (const [title, keys] of GROUPS) {
    const h = document.createElement("h4");
    h.textContent = title;
    const dl = document.createElement("dl");
    for (const k of keys) {
      const dt = document.createElement("dt");
      dt.textContent = k;
      const v = document.createElement("dd");
      v.className = "v";
      const c = document.createElement("dd");
      c.className = "c";
      dl.append(dt, v, c);
      cells.set(k, { dt, v, c });
    }
    groupsEl.append(h, dl);
    groupEls.set(title, [h, dl]);
  }

  const sparks = new Map(); // key -> { canvas, last, hist: number[] }
  for (const k of SPARKS) {
    const fig = document.createElement("figure");
    const canvas = document.createElement("canvas");
    canvas.width = 300;
    canvas.height = 88;
    const cap = document.createElement("figcaption");
    const name = document.createElement("span");
    name.textContent = k;
    const last = document.createElement("b");
    cap.append(name, last);
    fig.append(canvas, cap);
    sparkEl.append(fig);
    sparks.set(k, { canvas, last, hist: [] });
  }

  let lastSeq = null;

  function drawSpark(s) {
    const g = s.canvas.getContext("2d");
    const { width: W, height: H } = s.canvas;
    g.clearRect(0, 0, W, H);
    const h = s.hist;
    if (h.length < 2) return;
    let lo = Infinity, hi = -Infinity;
    for (const v of h) { if (v < lo) lo = v; if (v > hi) hi = v; }
    if (hi === lo) { hi = lo + 1; lo -= 1; }
    const pad = 4;
    const y = (v) => H - pad - ((v - lo) / (hi - lo)) * (H - 2 * pad);
    // a zero line, when zero is in range: the corrections read against it
    if (lo < 0 && hi > 0) {
      g.strokeStyle = "rgba(138,138,152,.5)";
      g.beginPath(); g.moveTo(0, y(0)); g.lineTo(W, y(0)); g.stroke();
    }
    g.strokeStyle = "#6ee7b7";
    g.lineWidth = 1.5;
    g.beginPath();
    const n = h.length;
    for (let i = 0; i < n; i++) {
      const x = ((i / (HISTORY - 1)) * (W - 2)) + 1;
      i ? g.lineTo(x, y(h[i])) : g.moveTo(x, y(h[i]));
    }
    g.stroke();
    // the range, so the trace is readable without an axis
    g.fillStyle = "rgba(138,138,152,.9)";
    g.font = "10px ui-monospace, Menlo, monospace";
    g.textBaseline = "top";
    g.fillText(fmt(hi), 3, 2);
    g.textBaseline = "bottom";
    g.fillText(fmt(lo), 3, H - 2);
  }

  /// `guider` is the frame header's object: { seq, ts_ns, cards, comments }. `status` is the
  /// bridge's /status message, or null. `ageS` is seconds since the page last received a frame.
  return function update({ meta: guider, status, ageS }) {
    if (guider) {
      const { cards, comments } = guider;
      for (const [k, cell] of cells) {
        cell.v.textContent = fmt(cards[k]);
        cell.c.textContent = comments[k] ?? "";
        cell.dt.title = comments[k] ?? k;
      }
      const guiding = Number(cards.GDGUIDE) !== 0;
      for (const [title, els] of groupEls) {
        const stale = GUIDING_ONLY.has(title) && !guiding;
        for (const el of els) el.classList.toggle("stale", stale);
      }
      if (guider.seq !== lastSeq) {
        lastSeq = guider.seq;
        for (const [k, s] of sparks) {
          const v = cards[k];
          if (typeof v === "number") {
            s.hist.push(v);
            while (s.hist.length > HISTORY) s.hist.shift();
            s.last.textContent = fmt(v);
            drawSpark(s);
          }
        }
      }
    }

    // The header line: frames flowing, or since when they are not. `age` is the page's own clock
    // since the last frame arrived — a fact about the stream, not about the sky.
    const gcam = status?.gcam ?? "—";
    const flowing = gcam === "streaming" && ageS !== null && ageS < 10;
    state.textContent = guider
      ? `#${guider.seq} · ${flowing ? "live" : `${ageS === null ? "?" : ageS.toFixed(0)} s ago`}`
      : "no frame yet";
    state.classList.toggle("warn", !flowing && !!guider);
    const idle = !flowing;
    banner.hidden = !idle;
    if (idle) {
      banner.textContent = `frames stopped — gcam: ${gcam}` +
        (status?.age_s != null ? ` · last frame ${status.age_s.toFixed(0)} s ago at the bridge` : "");
    }
    root.classList.toggle("stale-all", idle && !!guider);
  };
}
