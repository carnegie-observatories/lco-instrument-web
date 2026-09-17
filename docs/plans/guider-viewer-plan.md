# The web guider page — Plan

**Status:** implemented 2026-09-16; complete 2026-09-17, when zwo
PR #35 (`4ce4694`) restored the `roi`/`every` setters and the selects
came alive on SBS. Decisions below settled with W.S. on 2026-09-16. Supersedes the one-line disposition in
[zwo's gcam-web-viewer.md](../../../zwo/docs/plans/gcam-web-viewer.md)
("the viewer page ships with the instrument SPA instead of this
repository") by saying *where* in this repo, and what it costs.

## What this is

The browser page that shows a guider live — frames, the guider panel,
the guide box — served by the **gateway** at `/guider/<name>/`, against
the CHZ1 stream `gcamweb` proxies out of gcam. A standalone page an
operator parks in its own window; it is deliberately **not** a tab in
`app.html`.

## Why there is nothing to point at today

Three commits, in order:

- zwo `1f47b40` (2026-09-14) built the page: `app.js` (913 lines),
  `guider-panel.js` (168), `index.html`, `viewer.css` (818).
- this repo's `ca76a16` **copied that assembly** into
  [imageweb/imageweb/static/](../../imageweb/imageweb/static/) for
  science-frame quick look, replacing the guider panel with a FITS
  header panel and adding visibility gating.
- zwo `5fcc1fa` (2026-09-16) **deleted** zwo's `static/` tree, trimming
  `gcamweb` to the proxy alone, on the grounds that the page now ships
  with the SPA.

So the guider viewer was never lost and never ported — it is sitting in
this repo under another name. `imageweb/imageweb/static/app.js` says so
in its third line. The work below is **re-convergence**, not a port.

Two consequences, both live right now:

1. `http://<gateway>/guider/pfs-sv/` is a 404 — that path was never
   routed; `/config.json` emits `/guider/gcam13/`.
2. `/guider/gcam13/` is a 404 too — the gateway proxies it faithfully to
   a `gcamweb` whose only routes are `/guider/`, `/guider/guiders.json`,
   `/guider/<name>/ws` and `/guider/<name>/status`.

## How far apart the two assemblies actually are

Measured, not estimated:

| file | gcamweb `1f47b40` | imageweb today | differing lines |
|---|---|---|---|
| `app.js` | 913 | 934 | 141 |
| `viewer.css` | 818 | 789 | ~60, **all** `#guider`→`#hdr` renames plus one `#controls` left offset |
| `index.html` | 76 | 59 | title, strip controls, panel id |
| panel | `guider-panel.js` 168 | `header-panel.js` 104 | separate files already |

The panels are **already a clean seam**: both export
`mount*(root) → update({ …, status, ageS })` and both obey the same
"display verbatim, derive nothing" rule. The renderer, stretch,
colormaps, imexam, histogram, panner, magnifier, help overlay, chz1
wiring and reconnect logic are common.

The genuine differences are four:

1. **Which panel module is mounted**, and the `k`-toggle's label.
2. **The guide box overlay** — `drawGuideBox()` from the served cards
   `GDBOXX/GDBOXY/GDBOXSZ`, greyed on `GDGUIDE == 0`. Guider only.
3. **Visibility gating** — imageweb only today, but the guider page
   wants it more: a backgrounded guider tab streaming at 5 Hz is the
   more expensive of the two. Keep the `visibilitychange` half; the
   `postMessage` half goes (see "No SPA tab").
4. **The `roi` and `every` strip controls** — guider only, and their
   server side needs restoring (Decision 3).

## Decision 1 — one assembly, two panels ✅

**Extract the assembly to a top-level `viewer/` directory** served by
both surfaces, with the panel chosen at runtime.

```
viewer/
  quicklook.html    <body data-kind="quicklook">  — its strip, its panel toggle
  guider.html       <body data-kind="guider">     — its strip: + roi, every
  app.js            the one assembly
  viewer.css        one sheet (#info-panel, not #hdr / #guider — nor #panel, which the viewer chrome owns)
  panels/header.js  quick look   — moved from imageweb/static/
  panels/guider.js  the guider   — restored from zwo 1f47b40
  favicon.svg
```

**Two HTML files, one script.** The first draft said "one `index.html`
parameterised by `data-kind`", which cannot work: both servers hand the
page out as a static `FileResponse`, and nothing injects an attribute
into a static file. The strip markup differs per kind anyway (title,
the `roi`/`every` selects, the panel toggle's label), so one page would
need JS to hide half its own strip — more code than two 60-line files
that differ where they should. Each server serves its own file as
`/…/<name>/` and `app.js` reads `document.body.dataset.kind` to
dynamic-`import()` the one panel it needs; the quick-look page never
fetches the guider panel and vice versa.

One sheet still, with one per-kind rule: the two panels are different
widths, which is the sole reason `#controls` sits at `left: 340px` in
gcamweb's sheet and `404px` in imageweb's. `body[data-kind="guider"]
#controls { left: 340px }` and the rest is shared.

`imageweb.server.PAGES` is a flat tuple of filenames served one route
each; `panels/header.js` lives a directory down, so `instrument_app()`
switches to `add_static` on the viewer directory, registered **after**
`/ws` and `/status`.

*The cost, stated plainly:* `imageweb` stops being a self-contained
installable package — its `STATIC` points outside itself, the way
`DEFAULT_ASTRO_PH` already points at
`parents[3]/astro-ph-labs/astro-ph`. It is a `uv` workspace member of
this repo only, never published, so nothing outside this repo breaks.

## Decision 2 — the URL is the camera's function, not its number ✅

The gateway serves the **page** at `/guider/<name>/` — `pfs-sv`, the
name in the deployment file, on the `.app` bundle, and in the
lco-ansible inventory — and proxies only the live channels onward to
gcamweb's `gcam13`.

This is not cosmetic. In gcam's numbering, **guiders 1 and 2 are the
real guiders; 3 and up are other guiding-capable cameras on the
instrument** — `gcam13` is rotator port 1, camera 3, and what it
actually *is* is the PFS slit viewer. The number cannot carry that, so
the operator-facing surface must: the URL is the function name, the
card and panel headers read the `title` ("PFS slit viewer"), and
`gcam13` appears only as secondary detail next to the upstream it
proxies.

`deployment_config.resolve()` reverts `path` to `f"/guider/{name}/"`
and keeps `gcam_name` as the proxy target. `fa9e1ce` gets narrowed, not
reverted — the field it added is still needed, just not in the URL.

**`gcam_name` becomes required.** Today the schema lists it as optional,
"defaults to name". Under this decision it is the *only* thing that
reaches the upstream, and `pfs-sv` can never be a gcamweb name — so the
default would produce a guider that validates, deploys, and proxies to
`/guider/pfs-sv/ws` on a bridge that has no such guider. The validator
requires it; the only guiders that could omit it are ones whose
operational name already happens to be `gcamPG`, and spelling it twice
is cheaper than the failure mode.

`gateway.py`'s `make_proxy()` forwards `request.rel_url` **verbatim** —
that is the docstring's stated purpose. The name rewrite needs a
sibling, `make_guider_proxy(name, gcam_name)`, that maps
`/guider/pfs-sv/<tail>` → `/guider/gcam13/<tail>` and hands off to the
same `proxy_ws` / `proxy_http`; the same shape as `make_ws_proxy`,
which already rewrites the instrument path to `/`.

## Decision 3 — keep `roi` and `every`; restore their server side ✅

**These controls stay.** Centre-cropping is the bandwidth lever that
matters: the guider streams continuously at 1–5 Hz over the tunnel,
and `roi = N` keeps the central `1/N` of each side — `1/N²` of the
pixels, so "centre ½" is a ~4× cut in sustained WAN rate and
"centre ¼" ~16×, for a slit viewer whose interesting pixels are all
near the box. The tier selector (`bin`/`q`) trades resolution; `roi`
trades field; `every` trades cadence. An operator on a thin link needs
all three.

The guide box already survives the crop: `gcam.py` puts
`{x0, y0, w, h, src_w, src_h, n}` in the frame header as `crop`, and
`1f47b40`'s `drawGuideBox()` subtracts `crop.x0/y0` before dividing by
`bin`. Restored as-is, verified, not re-derived.

Their server side was deleted by `5fcc1fa` and must come back in zwo
PR #35 — see the appendix for the prompt to carry over there.

**Shared, not per-client.** In `gcam.py` both are applied at the
*source*: `every` at the pull (`if self._rx % self.every: continue`,
before parse or encode) and `roi` inside `_parse`, before the frame is
published. One viewer changing either changes it for every viewer of
that guider. That is acceptable for a slit viewer with one or two
watchers, but the strip must **label them as shared** rather than let
an operator assume a private setting. Contrast the tier selector, which
*is* already per-client — chz1 carries `bin`/`q`/`accept` in its
per-connection `Settings` and a `config` message mutates only that
client's copy. `roi` and `every` are the odd ones out, and making them
per-client is an astro-ph change, not a gcamweb one (the crop would
move into chz1's encode path). Out of scope here; noted in the appendix
as the eventual right shape.

**One thing from `1f47b40` does not come back: `?every=N` / `?roi=N` on
the page URL.** The old page `POST`ed them on load. With shared
semantics that turns a bookmark into a remote control — anyone opening
a saved `…/pfs-sv/?roi=4` re-crops every other viewer's stream, every
time, without touching a control. The selects stay; the URL parameters
go. On load the selects mirror `status()`, which already reports both
values, so no `GET` is needed either.

## Decision 4 — cross-origin isolation is already correct ✅

The decode worker pool needs `crossOriginIsolated`, which needs COOP/COEP
on the **top-level** document. `gateway.py`'s `no_store` middleware
already sets both when `Cf-Ray` is present and withholds them on plain
http — and the guider page will be served by the gateway, from the
gateway's own origin, exactly as quick look is. Nothing to add. It goes
on the verification list rather than the work list, because inherited
correctness is the kind that quietly stops being true.

## Decision 5 — no status-shape normalisation ✅

This is also the less-code option, which is why it wins on both counts.

`GcamSource.status()` and imageweb's `full_status()` overlap on
`name / clients / last_seq / age_s` and diverge after that (`gcam`,
`gnum`, `every`, `roi` vs `control`, `app`, `exposure`, `readout`,
`image_id`, `fits_path`). **`app.js` touches only the four common keys**
— they are all the strip needs — and passes the whole object through to
the panel, which reads its own. No adapter, no field map, no shared
type: the normalisation layer would be net-new code whose only job is
inventing second names for things the two sources already name, which
is the same derivation the "display verbatim" rule forbids.

## No SPA tab

`app.html` gets no Guider tab. The guider is a page an operator parks in
its own window and watches continuously — the opposite of Quick Look,
which is glanceable and belongs beside the controls that produce it.
`window-host.js`'s `embed` machinery is untouched, and the deployment
file needs no field linking a guider to an instrument.

The practical consequence for the shared assembly: the page keeps its
own `document.visibilityState` gating and drops the
`{quicklook: "active"|"inactive"}` `postMessage` listener under
`kind === "guider"`. Quick look keeps both.

## The gateway's `/guider` routes, after

```
/guider/                          gateway-generated index
/guider/guiders.json              config ∪ upstream status
/pkg/{chz1,core,viewer}/          astro-ph packages — ONE mount, shared with /image/
/guider/<name>/                   the page
/guider/<name>/app.js  viewer.css  panels/*  favicon.svg
/guider/<name>/ws                 proxied → gcamweb /guider/<gcam_name>/ws
/guider/<name>/status             proxied → gcamweb /guider/<gcam_name>/status
/guider/<name>/every              proxied, GET + POST  (Decision 3)
/guider/<name>/roi                proxied, GET + POST
```

**One `pkg/` mount (Decision 7).** The astro-ph packages are served
once, at `/pkg/`, not once per surface. Both pages sit two levels deep
(`/image/<app>/`, `/guider/<name>/`), so both import maps — and the
decode worker and wasm URLs `app.js` builds the same way — address
`../../pkg/…`. The page still never knows its prefix; it only knows it
is two deep, which both surfaces guarantee. imageweb's standalone
`build_app()` moves its mount from `<prefix>/pkg/` to `/pkg/` to match,
so `python -m imageweb.server` serves the same page unchanged. On the
gateway the `/pkg/` static goes before the SPA's `/` static (both are
prefix resources; first match wins).

Three hazards, all from aiohttp's first-match routing:

- the existing catch-all `app.router.add_route("*", "/guider/{tail:.*}", …)`
  must be **replaced**, not merely preceded — a catch-all registered
  before the static and page routes shadows them all;
- the `every`/`roi` proxies must be registered with `add_route("*", …)`,
  not `add_get`, or the POST arrives as a 405;
- the per-name `add_static("/guider/<name>/", viewer/)` is itself a
  prefix resource and goes **after** the four proxied routes and the
  page route — `add_static` also 403s a bare directory, which is why
  `/guider/<name>/` needs its own `add_get` (the same reason `/` does
  at the bottom of `build_app`);
- `/guider/` and `/guider/guiders.json` stop being proxied and become
  gateway-generated, because the gateway knows names gcamweb does not
  (`pfs-sv`, the titles) and gcamweb knows state the config does not.
  The join is by `gcam_name` against one upstream fetch of
  `guiders.json`; when that fetch fails the index still renders from
  config alone, each guider marked "bridge unreachable". A landing that
  500s because the thing it describes is down describes nothing.

## Decision 6 — `/healthz` over WebSocket only; the text command interfaces are never touched ✅

Not in the first draft; found while checking the plan's claims. This
is inherited from Phase 2, not introduced here, and it is the one item
that should not wait for the rest.

**The rule, from W.S.:** the gateway never opens a connection to a TCP
text command interface — not gcam's command port (`52200 + gnum`), not
an instrument's. Nothing in the gateway, health check or otherwise,
ever will. Every liveness question is asked over the WebSocket
interface the browser itself uses.

**What is wrong today.** `health_handler()` probes every guider at
`command_port` — 52203 for `pfs-sv` — and `reachable()` does so by
opening a raw TCP connection and closing it. zwo's own design docs
describe that port as **single-client** (`gcam-image-server.md`: "on
the command port, single client") and `gcam-web-viewer.md` says of it:
"a bridge holding or even periodically grabbing it could lock
operations tooling out." The ansible playbook's post-task hits
`/healthz` on every run, and any monitor will hit it on a schedule.
Each hit takes the one slot — for milliseconds, but from whatever
operations tool holds or is about to take it, and if that tool already
has it the probe reports the guider *down*. The check is both hazardous
and wrong. The instrument and bridge probes are the same raw
connect-and-close against ports that happen to be WebSocket servers:
not hazardous, but a probe that proves a socket accepts is not a probe
that proves the app answers.

**What replaces it.** `reachable()` is deleted, so nothing can reuse
it. One check per target, each a real WebSocket session on the
gateway's existing `ClientSession`, each closed after the first frame:

| target | WebSocket | what proves "up" |
|---|---|---|
| instrument | `ws://<host>:<port>/` — the control WS | its unsolicited `hello` frame (`app`, `version`), which `ws.js` and imageweb's `ControlClient` already wait for |
| guider | `ws://<bridge>/guider/<gcam_name>/status` — gcamweb's status channel | the first status JSON, which carries `gcam: streaming \| idle \| unreachable` from the image port gcamweb is designed to hold |

The guider check proves the bridge too — the separate `("bridge",
"gcam", host, port)` TCP probe goes. gcamweb's `status_ws` adds the
probe to `status_clients` only, never to the source's viewer count, so
gcam sees nothing: no pull, no client slot. Each check reports `up`
plus a `detail` (`app`/`version` for an instrument, the `gcam` state
for a guider); the playbook's report prints it.

`command_port` then has no consumer in `/config.json`, `server.py`'s
banner, or `/healthz`; it leaves `resolve()`'s output rather than
sitting there inviting the next reader to dial it. `image_port` stays
— it is what `gateway_gcamweb_guiders` in the inventory has to agree
with, and the validator cross-checks it.

## Staging

0. **`/healthz` over WebSocket only** (Decision 6). Independent of
   everything below, a few dozen lines in `gateway.py` plus the
   playbook's report line, and the one change here that removes a
   hazard rather than adds a feature — it ships first and alone.
1. **Extract the shared assembly.** Pure move + rename (`#hdr`/`#guider`
   → `#info-panel`), quick look unchanged in behaviour. *Exit:* `/image/pfs/`
   on SBS still renders a live frame, header panel intact.
2. **The guider page.** Restore `panels/guider.js` and the guide box
   from zwo `1f47b40`; gateway serves `/guider/<name>/` and proxies the
   live channels. *Exit:* a real gcam frame on screen at
   `/guider/pfs-sv/`, panel greying with `GDGUIDE`, box at
   `GDBOXX/GDBOXY`.
3. **Names, index, and the bandwidth controls.** `deployment_config`
   path back to `name`; gateway-generated `/guider/` and
   `guiders.json`; landing card points at the working URL;
   `roi`/`every` wired once PR #35 restores them. *Exit:* the URL an
   operator types is the URL that works, and centre-crop measurably
   drops the wire rate.

Phases 1–3 are one deployable unit; shipping 1 or 2 alone leaves the
landing card pointing at a 404, which is where we are now. Phase 3's
`roi`/`every` half is the only part gated on another repo — the rest of
Phase 3 does not wait for it, and the controls degrade to disabled
selects with a title explaining why until `gcamweb` answers.

## Verification

- Phase 1 is a refactor: quick look must be indistinguishable. A frame
  through `/image/pfs/`, header panel populated, no console errors.
- Phase 0: run the SBS playbook while a command-port client is
  connected to gcam and confirm `/healthz` still reports the guider up
  — the old probe would have reported it down, or worse. Then
  `grep -n open_connection gateway.py` returns nothing, and stays that
  way: the rule is that the gateway has no raw-socket code at all.
- Phase 2 against the live rig, not the emulator: `gcam13` is guiding
  PFS's slit viewer on SBS and has already been proven to deliver
  1000² CHZ1 frames through this gateway (0.54 MB lossless at 3.7×;
  the 22 fps figure was encoder throughput, not gcam's cadence).
- Phase 3: set `roi` to 4 and confirm the served frame side drops 4×
  **and** the wire bytes drop with it — the point of the control is the
  second half, and cropping that doesn't reach the wire is a no-op.
- COOP/COEP: `crossOriginIsolated === true` in the console **behind the
  tunnel**, `false` on the LAN origin with inline decode still working.
  Both, because the failure mode is one-sided.
- `changed=0` on a second ansible run — the role itself needs no change,
  only `gateway_version` in `inventory_sbs.yaml`.

## Open items

- **The temporary rsync** of `zwo/src/web` in the gateway role stays
  until PR #35 merges and `gcamweb` ships in a zwo release.
- **Per-client `roi`/`every`** — the eventual right shape, an astro-ph
  change. See the appendix.

---

## Appendix — prompt for zwo PR #35

Paste into a Claude Code session in `~/workspace/zwo`, on branch
`feature/gcam-web-viewer`:

```
On this branch, commit 5fcc1fa ("web: trim gcamweb to the WebSocket
proxy -- no pages") removed the POST setters for `every` and `roi`
along with the static viewer, on the reasoning that both "remain as
per-guider CLI flags". The viewer is being rebuilt in
carnegie-observatories/lco-instrument-web (docs/plans/guider-viewer-plan.md),
and it needs those two as *runtime* controls, not launch flags:
centre-crop and frame stride are the bandwidth levers for a guider
streaming continuously over a Cloudflare Tunnel, and the operator
choosing them is at the glass, not at the ansible inventory.

Please restore the server side only — no static/, no pages, no
--astro-ph, no isolation_headers. Concretely, put back into
gcamweb/gcam.py and gcamweb/server.py what 5fcc1fa deleted:

  - GcamSource.MAX_EVERY / MAX_ROI and the clamping set_every() /
    set_roi() methods;
  - the `setting(attr, setter)` handler and its two routes,
    add_route("*", "/every", ...) and add_route("*", "/roi", ...),
    GET returning the current value and POST ?n=N setting it;
  - the two lines in the module docstring's route table.

`status()` already reports `every` and `roi`, so the viewer can render
the current value without a GET on load — please keep that. (The
viewer will not bring back the old page's `?every=N` / `?roi=N`
URL parameters: with shared semantics a bookmark that POSTs on load is
a remote control for everyone else's stream.)

Two things to decide rather than assume:

1. These are source-level and therefore SHARED across every viewer of
   a guider (`every` gates the pull in _pump before parse/encode; `roi`
   crops inside _parse, before publish). The viewer will label them as
   shared. If you would rather they were per-client, say so and the
   viewer will not expose them at all until that lands — a control that
   silently changes another operator's view is worse than no control.
   Per-client is the better end state and the plan says so: chz1
   already carries bin/q/accept in a per-connection Settings that a
   `config` message mutates for one client only, so roi/every are the
   odd ones out. Doing it properly means moving the crop into chz1's
   encode path (an astro-ph change, packages/chz1/python/stream.py),
   which is out of scope for this PR.

2. Whether POST-with-query-string is the shape you want on a proxied
   endpoint. The gateway will forward these with add_route("*", ...),
   so any method works; if you prefer a JSON body or a `config` message
   on the existing WS, name it and the viewer follows.

Please also update src/web/README.md's route list and
docs/plans/gcam-web-viewer.md — the latter's 2026-09-16 note should
point at lco-instrument-web's docs/plans/guider-viewer-plan.md as the
place the viewer now lives, so the next person looking for it does not
repeat the search that produced this.
```
