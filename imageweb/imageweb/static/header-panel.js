// The header panel: the instrument's state and the frame's FITS header, shown verbatim.
//
// Every value in the cards table is a FITS card the instrument wrote into the frame, printed
// under the card's own name and the instrument's own comment. Nothing is computed from them —
// no unit conversion, no derived quantity. The status block above it is the bridge's /status
// message: the control-WS connection, the exposure/readout topics (pushed by the Cocoa app),
// and the frame's age. See docs/plans/image-viewer-plan.md, "display verbatim, derive nothing".
//
// `mountHeaderPanel(root)` fills the element and returns `update({ fits, status, ageS })`.

const fmt = (v) => {
  if (v === null || v === undefined) return "—";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toFixed(Math.abs(v) < 10 ? 3 : 2);
  return String(v);
};

const pct = (v) => (typeof v === "number" ? `${Math.round(v * 100)}%` : null);

export function mountHeaderPanel(root) {
  root.innerHTML = `
    <header><b>image</b><span id="hp-state">—</span></header>
    <p class="banner" id="hp-banner" hidden></p>
    <h4>instrument</h4>
    <dl id="hp-status"></dl>
    <h4>FITS header</h4>
    <dl class="cards" id="hp-cards"><dt></dt><dd class="v">no image yet</dd><dd class="c"></dd></dl>`;
  const state = root.querySelector("#hp-state");
  const banner = root.querySelector("#hp-banner");
  const statusEl = root.querySelector("#hp-status");
  const cardsEl = root.querySelector("#hp-cards");

  // The live rows, written in place at the /status cadence (~1 Hz).
  const rows = new Map(); // label -> dd
  for (const label of ["control", "app", "exposure", "readout", "image"]) {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.className = "v";
    const dc = document.createElement("dd");
    dc.className = "c";
    statusEl.append(dt, dd, dc);
    rows.set(label, dd);
  }

  let cardsFor = null; // the image id the cards table currently shows

  // Frames are minutes apart, so the cards table is simply rebuilt per frame —
  // in the header's own order, name + value + comment, all of it verbatim.
  function rebuildCards(fits) {
    cardsEl.replaceChildren();
    for (const [k, v] of Object.entries(fits.cards ?? {})) {
      const dt = document.createElement("dt");
      dt.textContent = k;
      dt.title = fits.comments?.[k] ?? k;
      const dv = document.createElement("dd");
      dv.className = "v";
      dv.textContent = fmt(v);
      const dc = document.createElement("dd");
      dc.className = "c";
      dc.textContent = fits.comments?.[k] ?? "";
      cardsEl.append(dt, dv, dc);
    }
  }

  /// `fits` is the frame header's object: { id, path, cards, comments }. `status` is the
  /// bridge's /status message, or null. `ageS` is seconds since the page received the frame.
  return function update({ fits, status, ageS }) {
    if (fits && cardsFor !== (fits.id ?? fits.path)) {
      cardsFor = fits.id ?? fits.path;
      rebuildCards(fits);
    }

    const exp = status?.exposure;
    const ro = status?.readout;
    rows.get("control").textContent = status?.control ?? "—";
    rows.get("app").textContent = status ? `${status.app ?? "?"} ${status.version ?? ""}`.trim() : "—";
    rows.get("exposure").textContent = exp
      ? exp.running
        ? `exposing${exp.paused ? " (paused)" : ""} · #${fmt(exp.id)} · loop ${fmt(exp.loop)}/${fmt(exp.nloops)}${pct(exp.progress) ? ` · ${pct(exp.progress)}` : ""}`
        : "idle"
      : "—";
    rows.get("readout").textContent = ro
      ? (typeof ro.progress === "number" && ro.progress > 0 && ro.progress < 1
          ? `reading out · ${pct(ro.progress)}`
          : `${fmt(ro.binx)}×${fmt(ro.biny)} · ${fmt(ro.speed)} · ${fmt(ro.readmode)}`)
      : "—";
    rows.get("image").textContent = status?.fits_path
      ? `#${fmt(status.image_id)} · ${status.fits_path.split("/").pop()}${status.pending ? " · decoding…" : ""}`
      : "no image yet";

    // The header line: this frame, and how old it is on this page's own clock.
    state.textContent = fits
      ? `#${fmt(fits.id)} · ${ageS === null ? "?" : ageS < 10 ? "new" : `${ageS.toFixed(0)} s ago`}`
      : "no image yet";

    // The banner speaks only when something is wrong: control WS down, or a decode failure.
    const down = !!status && status.control !== "connected";
    const err = status?.error;
    banner.hidden = !down && !err;
    if (down) banner.textContent = `control WS: ${status.control} — new readouts will not be announced`;
    else if (err) banner.textContent = `frame failed to load: ${err}`;
    root.classList.toggle("stale-all", down && !!fits);
  };
}
