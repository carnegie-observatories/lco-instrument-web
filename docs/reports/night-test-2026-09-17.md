# Night test — 2026-09-17, PFS through the tunnel

**What was tested.** The web side of an observing night as a browser lived it:
the PFS instrument page (SPA, camera tab), the SPA's Quick Look tab (imageweb
viewer in an iframe) and the `pfs-sv` guider viewer, all through
`sbs.chimera.observer` (Cloudflare Tunnel + Access), recorded by
`tools/nighttest.py` over Chrome's DevTools port on the operator's laptop.
PFS exposed 90 × 300 s. Recording: 08:29:01 → 16:09:12 UTC, 7.67 h,
248 271 records (`runs/nighttest-20260917.jsonl`, summary in
`runs/nighttest-20260917-report.json`). Method and set-up:
`docs/plans/night-test-plan.md`.

**Verdict.** The software did its job all night: every readout reached the
Quick Look, the guider streamed at 2 fps with sub-second lag, the
instrument page never lost a state update it could not recover in 3 s, no
page crashed, leaked or errored. The tunnel path, however, cut WebSockets
253 times in three distinct ways; two of them are understood and fixed in
code (not yet deployed), one is open. The user-visible cost was a
"disconnected — retrying" flash roughly every two minutes on the Quick
Look and every twenty on the instrument page, and 156 MB of science frames
re-sent that the page already had.

## Key figures

| | value |
|---|---|
| Duration | 7.67 h (08:29–16:09 UTC) |
| Exposures seen | 88 (ids 7 → 94), 300 s each |
| Quick Look readouts received | 88 of 88 (FITS #6 → #93) |
| Quick Look frames on the wire | 261 — 88 useful, 173 replays |
| Quick Look bytes | 223 MB, of which 156 MB (70 %) replay |
| Guider frames | 54 894, 1.14 GB, 1.99 fps |
| Guider frames lost to the network | 10 (of 55 051) |
| Guider lag p50 / p90 / p99 / max | 0.10 / 0.51 / 0.61 / 5.4 s |
| Guider decode p50 / p99 | 0.7 / 3 ms |
| Instrument state messages | 74 / min, flat |
| Instrument reconnects (`hello`) | 26 (22 cuts, 3 recorder reloads, 1 first) |
| WebSocket closes, all code 1006 | 253 |
| Page crashes / console errors / heap growth | 0 / 0 (after start-up) / none |

## Socket closes — three classes

Every close of the night was code 1006 (connection gone without a close
frame). By age at close and by what the socket carried, they fall into
exactly three classes:

| class | closes | which sockets | rule | status |
|---|---|---|---|---|
| 1 — idle cut | 172 | `/image/pfs/ws` | dies 125 s after the last frame, i.e. whenever nothing crosses the socket in either direction (Cloudflare's documented idle timeout) | fixed in chz1: `heartbeat=20` on the stream socket — [astro-ph PR #1](https://github.com/astro-ph-labs/astro-ph/pull/1) |
| 2 — twenty-minute cut | 60 | `/pfs/ws` (both SPA pages), `/guider/pfs-sv/status` | dies exactly 20:00 after it was opened, 22 times in a row to the second; only sockets on which the browser never sends after its first message | fixed in the gateway: `heartbeat=20` on `proxy_ws` (`5ef8747`) |
| 3 — subset reset | 21 in 14 events | any socket | a few sockets, of any age (88 s to 2.6 h) and any traffic pattern, die within the same second; the operator's own tabs must be hit too | open — needs `/opt/cloudflared/log/cloudflared.log` on sbs-inst1 at the times below |

Both fixes are one argument each, and both were verified only as far as
the code allows (chz1's own `test_stream.py` gate: 9/9; local `uv sync`
reinstalls chz1). Neither was on the server during this run, so every
number in this report is the *before* picture.

### Class 1 in detail — the Quick Look socket

```
08:29:04  frame pfs #6                        seq 1
08:31:10  /image/pfs/ws closed 1006           126 s after the frame
08:31:11  reconnect, config
08:31:12  frame pfs #6 again (0.77 MB)        seq 1   ← replay
08:33:07  frame pfs #7                        seq 2
08:35:12  closed 1006                         125 s after the frame
…
```

The pattern repeated 172 times, twice per five-minute exposure. Delivery
is seq-keyed, so no readout was missed (`status.age_s` never exceeded one
exposure, max 320 s), but 173 of the 261 frames received were replays:
156 MB of 223 MB.

### Class 2 in detail — the twenty-minute rule

| socket opened | closed | lifetime |
|---|---|---|
| 08:29:02 (page load) | 08:49:02 | 20:00 |
| 08:58:46 (reload) | 09:18:47 | 20:01 |
| 09:18:49 | 09:38:49 | 20:00 |
| 09:38:51 | 09:58:51 | 20:00 |
| … 18 more, every one within ±1 s of 20:00 … | | |

The three victims have one thing in common: the SPA sends `subscribe`
once and then only listens; gcamweb's status channel sends nothing at
all. Every socket that carried client-to-server bytes (the guider frame
socket acks each frame; imageweb's status socket pongs its 20 s
heartbeat) was never cut this way. The gateway has no such timer (aiohttp
3.14 maps the proxy's `ClientTimeout(total=10)` to a close timeout only,
receive timeout `None`); the cut is in the tunnel or the edge, and
whichever it is, a pong every 20 s is the traffic it wants.

### Class 3 — subset resets, times (UTC)

```
10:03:08  guider frame socket                       age 64 min
10:05:40  imageweb status                           age 67 min
10:20:28  imageweb status + guider frame + guider status   (15 min, 17 min, 88 s)
10:39:53  guider frame                              age 19 min
11:29:28  imageweb status                           age 69 min
12:14:42  guider frame                              age 95 min
12:57:28  imageweb status                           age 88 min
13:14:47  guider frame                              age 60 min
13:32:35  guider frame                              age 18 min
13:34:29  PFS socket, camera page                   age 15 min
14:00:40  guider status                             age 20 min (not on the 20:00 rule: 19.6)
15:17:29  PFS socket, camera page                   age 2.8 min
15:35:00  imageweb status + guider frame + PFS camera + /image/pfs/ws   (2.6 h, 2.0 h, 17 min, 1 min)
15:51:12  imageweb status, guider status, PFS quicklook, PFS camera   (staggered over 70 s)
```

Each cost the guider one or two frames (a 2–3 step in gcam's sequence)
and a one-second reconnect; the pages hid all of it. Chrome carried every
socket on its own HTTP/1.1 connection (every handshake `101 Switching
Protocols`, `Server: cloudflare`, colo LAX), so these are separate TCP
connections reset in the same instant — what a reset of one of
cloudflared's four edge connections looks like from the browser. Only the
host log can say.

## Guider `pfs-sv`

- 54 894 frames, 1.14 GB, 1.99 fps sustained for 7.7 h. gcam reported
  `streaming` in 27 418 of 27 425 status samples (the rest: the seconds
  around reloads). roi 4 for the first 45 min (125×125 bin 2), roi 2
  after (250×250 bin 2) — the operator's change, propagated to the page
  through the shared setting.
- Camera sequence 102986 → 158036: 157 frames not seen by the browser, of
  which 146 fell in the recorder's own three page reloads (08:55–08:59)
  and **10** in class-3 resets. Nothing dropped for bandwidth or credit
  reasons: every step outside those moments was 1.
- Lag (arrival minus gcam's timestamp, clock offset included): p50
  0.105 s, p90 0.508 s, p99 0.608 s, max 5.4 s (one frame). Inter-arrival
  p50 0.52 s, p99 1.05 s.
- Decode in the browser: p50 0.7 ms, p99 3 ms; the 900 ms outlier is the
  first frame after a load (WebGPU warm-up).
- One console error at the 08:58 reload, `decode_band(y0=0) failed with
  -2` from the worker pool; never recurred, no frame was acked as failed.

## Quick Look

- 88 distinct readouts received, FITS #6 → #93, none missed. Interval
  between distinct frames: the exposure cadence (300 s + readout).
- 261 frames on the wire: 88 useful + 173 class-1 replays.
- Decode p50 26 ms at bin 4 (2800×1330, 0.77 MB); the one lossless
  11200×5320 frame the operator requested decoded in 1.85 s.
- One warning at the 08:58 reload ("WebSocket is closed before the
  connection is established") — the viewer reconnecting while the SPA tab
  activated it; never recurred.

## Instrument page (PFS SPA)

- 34 045 state messages, 74/min all night: `exposure` at 1 Hz, `disk`
  every 3 s, `readout`/`shutter`/`mechanics` per exposure. 5 480 log
  events.
- 26 `hello`s on the camera page: 22 class-2 cuts + 3 recorder reloads +
  the first connect. Every cut healed in 2–3 s (the SPA's 2 s retry).
- No command was sent during the run (PFS was driven from its Cocoa UI),
  so nothing to say about acks.
- PFS logged `WARNING: 4 WS clients connected` whenever ours reconnected:
  the operator's ordinary Chrome kept two SPA tabs open throughout, so
  the instrument served twice the normal client load.

## Page health

| page | JS heap first → max → last (MB) | DOM nodes first → last | hidden samples | crashes |
|---|---|---|---|---|
| guider `pfs-sv` | 3.0 → 3.0 → 2.3 | 3 994 → 4 899 | 0 / 860 | 0 |
| PFS camera | 2.5 → 2.5 → 1.3 | 10 332 → 4 129 | 0 / 860 | 0 |
| PFS Quick Look | 2.0 → 3.0 → 2.5 | 5 192 → 6 987 | 0 / 860 | 0 |

(from the 08:58 reload onwards; before it, 34 samples of the camera page
and 7 of the Quick Look were `hidden` because another application's
window covered them — the reason the debug Chrome was relaunched with
`--disable-backgrounding-occluded-windows`).

## Recorder notes

- Chrome 136+ refuses a debugging port on its default profile; the test
  ran in a second Chrome (`Chrome-nighttest` profile, port 9222) with the
  Access login done once there.
- The recorder reloaded each page three times (08:29 start, 08:55 and
  08:58 restarts to add the close-code hook). Those reloads account for
  the three `navigated`/`hello` per page and the 146-frame guider gap.
- DevTools handed over every binary frame whole (0 truncated of 55 155).

## What to do next

1. Deploy the two heartbeats: `playbooks/gateway.yml` on sbs-inst1
   (rsyncs astro-ph with the chz1 change, `uv sync` reinstalls it,
   restarts the gateway and gcamweb).
2. Re-run the recorder for a night: classes 1 and 2 should read zero in
   `report`; class 3 should be unchanged.
3. Read `/opt/cloudflared/log/cloudflared.log` at the class-3 timestamps
   above, and `cloudflared tunnel info` for connection ages.
4. For a clean-load night, close the operator's own SPA tabs while the
   test Chrome runs, or run only the test Chrome.
