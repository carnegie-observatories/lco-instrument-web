# Simulated night test — recording what the browser sees

Status: recorder implemented and running (`tools/nighttest.py`, 2026-09-17).
First run against SBS: PFS exposing 90 × 300 s, with the SPA (camera tab),
the SPA's Quick Look tab and the `pfs-sv` guider viewer open through the
Cloudflare tunnel. Results below are updated as the run proceeds.

## Why

Every earlier check was a snapshot: a screenshot, a `/healthz`, one frame in
a headless Chrome. A night is eight hours of the same three pages left alone,
and the failures that matter there — frames that stop arriving, a viewer
that quietly reconnects every few minutes, a heap that grows until the tab
dies, a status channel that lies about the camera — leave no trace once the
page is reloaded. The test records the night as the browser lived it, into a
file that can be analysed afterwards without the instruments.

## What it is

`tools/nighttest.py record` attaches to a Chrome through its DevTools port
and writes one JSON record per line (`runs/*.jsonl`, git-ignored) for:

| record | source | what it carries |
|---|---|---|
| `frame` | every binary WebSocket message | the chz1 header parsed off the front: connection `seq`, `w×h`, `bin`, `comp_bytes`, server timings; for the guider the camera's own `src_seq` and `src_ts_ns`; for the quick-look the `fits_id` and path. `wire_bytes` vs `expect_bytes` proves DevTools handed over the whole frame. |
| `ack_sent` | the viewer's ack | `seq` and `decode_ms` — the browser's decode time per frame |
| `status` | `/status` channels (1 Hz) | gcam state, clients, `every`, `roi`, `last_seq`, `age_s`; imageweb's exposure/control status |
| `hello`, `state`, `event`, `ack`, `cmd` | the instrument WebSocket | `hello` counts reconnects; `state` per topic (the `exposure` topic in full: id, loop, runtime, progress); `event` (logs); commands and their acks |
| `ws_open`, `ws_close`, `ws_error` | Network domain | every socket the page opened, by channel |
| `metrics` | every 30 s | JS heap, DOM nodes, layout counts, `document.visibilityState`, the strip's state and rate text, `__viewer.status()`, and the same for the viewer embedded in the SPA's Quick Look tab |
| `console`, `exception`, `log` | Runtime/Log domains | errors and warnings, first 500 chars |
| `navigated`, `loaded`, `crash`, `attached`, `detached`, `target_missing` | Page/Inspector | reloads, renderer crashes, and the recorder's own reattachments |

`tools/nighttest.py report <file>` reads the file back and prints, per page:
frames and bytes received, fps and inter-arrival percentiles, per-connection
sequence breaks, the guider's estimated **dropped frames** (steps in the
camera sequence larger than the `every` in force), **lag** percentiles
(arrival minus camera timestamp — includes the clock offset between the two
hosts, so the spread is the measurement and the median is not), decode-time
percentiles, status histograms (`gcam` state, `clients`, `age_s`),
instrument state-message rates per topic, exposures seen (ids, running
edges), reconnects (`hello` count, `ws_close`), errors with samples, and
heap/node growth. `--json` gives the same as a document.

Nothing in the recorder talks to an instrument or to gcam: every byte comes
out of the browser. The rule that the web side never opens a TCP text
command interface holds here too.

## Running it

1. Chrome 136+ refuses a debugging port on its default profile. Start a
   second instance with its own profile — the Access cookie lands there once:

       "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
         --user-data-dir="$HOME/Library/Application Support/Google/Chrome-nighttest" \
         --remote-debugging-port=9222 --no-first-run \
         --disable-backgrounding-occluded-windows --disable-renderer-backgrounding \
         --disable-background-timer-throttling \
         'https://sbs.chimera.observer/app.html?ws_path=%2Fpfs%2Fws&tab=camera' \
         'https://sbs.chimera.observer/app.html?ws_path=%2Fpfs%2Fws&tab=quicklook' \
         'https://sbs.chimera.observer/guider/pfs-sv/'

   Log in to Access in that window. The recorder waits for the login.

   The three `--disable-…` flags matter on a desktop someone is using:
   Chrome marks a window covered by any other application's window as
   hidden, the viewer then pauses ("paused (not visible)"), and the test
   records nothing from it. With the flags an occluded window stays
   `visible`; only a minimised one is hidden. (Learned on the first run:
   by 08:55 both SPA windows were under the operator's own windows.)

2. Record. It finds every `sbs.chimera.observer` tab and classifies it by
   URL (SPA + tab, `/image/<inst>/`, `/guider/<name>/`), gives each its own
   window and tiles the windows so none is fully covered — Chrome treats a
   covered window as hidden, and a hidden viewer stops streaming (`--no-tile`
   leaves them alone). It reloads each page once, because DevTools reports
   only WebSockets opened after it started listening.

       uv run tools/nighttest.py record --out runs/nighttest-$(date +%Y%m%d).jsonl

   The terminal gets one line a minute (records, frames per page, exposure
   snapshots, error count). Ctrl-C ends the run cleanly with a `stop` record.

3. Report, any time, on the live file or afterwards:

       uv run tools/nighttest.py report runs/nighttest-20260917.jsonl

## Reading the numbers

- **Guider drops.** gcam numbers every frame (`src_seq`); gcamweb forwards
  every `every`-th. A step larger than `every` is a frame the browser never
  got — either gcamweb skipped it because the client had no credit (the
  `inflight` window was full) or the socket was down. `dropped_est` counts
  them; `steps` shows the histogram, which should be `{every: n}`.
- **Guider lag.** `lag_s` p50 is the clock offset plus the true latency, and
  cannot separate the two; p90 − p50 and max − p50 are the jitter and the
  worst stall, which is what a night is judged on. `status.age_s` is the
  bridge's own view of the newest frame's age, from the server's clock.
- **Quick-look.** One frame per readout: `fits_ids.unique` should equal the
  number of exposures completed while recording, `interval_s.p50` the
  exposure time plus readout. `status.age_s.max` growing past one exposure
  means a readout the viewer did not receive.
- **Instrument.** `hello.n` above 1 is a reconnect. `state_msgs.per_min`
  should be flat all night; `exposures.unique_ids` and `running_edges`
  count what PFS did. `cmds.failed` is a rejected command.
- **Page health.** `metrics.heap_MB` first→last and `nodes` first→last should
  not climb; `hidden_samples` above 0 means a page spent time hidden and its
  gaps are not the network's fault; `crashes`, `detached` and `navigated`
  above 1 say the page did not stay up on its own.

## First run — 2026-09-17

Set-up: PFS exposing 90 × 300 s ("object", mode time), the debug Chrome on
the operator's laptop through `sbs.chimera.observer`. The operator's own
Chrome kept its two SPA tabs open as well, so PFS saw four WS clients (its
log said so at connect: `WARNING: 4 WS clients connected`) — the SPA channel
was under twice the client load of a normal night.

First minute, from `report`:

| page | what arrived |
|---|---|
| `pfs-sv` | 111 frames, 125×125 bin 2 (roi 4, every 1), 1.97 fps, `steps {1: 110}`, 0 dropped; lag p50 0.03 s, p90 0.41 s, max 0.51 s; decode p50 0.6 ms (max 900 ms on the first frame, WebGPU warm-up); gcam `streaming` throughout |
| `pfs:quicklook` | frame `pfs #6` (2800×1330 bin 4, 0.77 MB, complete), decode 833 ms; status `age_s` counting up from the last readout |
| `pfs:camera` | hello once, 85 state messages/min (`exposure` 1 Hz, `disk` every ~3 s), exposure id 7 running, 300 s, loop 4 of 90 |

No console errors, exceptions or crashes in the first hour.

### Finding 1 — the frame socket dies every idle 125 s (fixed, deploy pending)

The Quick Look's frame socket (`/image/pfs/ws`) closed at 08:31:10,
08:35:12 and 08:37:19 UTC: each time 125 s after the last frame it had
received, with `clients` dropping 1 → 0 → 1 on the status channel and no
error on either side. The viewer reconnected a second later, sent its
`config`, and imageweb replayed the newest frame (`seq` back to 1, same
FITS id, 0.77 MB again). Nothing was lost — delivery is seq-keyed — but
the page spent a second per cycle "disconnected — retrying", and every
five-minute exposure cost two reconnects and 1.5 MB of replay. The
guider's socket never showed it because gcam was streaming at 2 fps; an
idle guider would.

Cause: Cloudflare closes a WebSocket that carries no data in either
direction for ~100 s (documented under "Idle timeout" in
developers.cloudflare.com/network/websockets). chz1's stream socket had no
heartbeat; imageweb's and gcamweb's *status* sockets already had
`heartbeat=20`, which is why they never dropped.

Fix (astro-ph `8574e61`, branch `chz1-stream`): `heartbeat=20` on the
stream socket, so aiohttp pings every 20 s and the browser pongs. One
handler serves both imageweb and gcamweb, so both frame sockets are
covered. Alongside it, chz1's `pyproject.toml` now keys uv's cache on
`python/*.py`: consumers install chz1 as a directory dependency, and uv
was rebuilding it only when `pyproject.toml` changed — the fix would have
rsynced to the gateway and never entered its venv (verified locally:
`uv sync --frozen` reinstalled chz1 once after the change, and was quiet
the second time).

Deploy: the usual `playbooks/gateway.yml` run (rsyncs astro-ph, `uv sync`
reinstalls chz1, restarts the gateway and gcamweb). The recorder will
show the restart as one `ws_close`/`ws_open` on every socket, and then no
further `ws_close` on `/image/pfs/ws` between readouts. Not run yet.

### Finding 2 — three sockets dropped together at 08:49:02 (open)

Both SPA sockets (`/pfs/ws`, camera and Quick Look pages) and the guider's
status socket closed within 400 ms of each other; the guider's frame socket
(connection seq 2349 → 2350 unbroken) and both imageweb sockets did not,
so the gateway did not restart. PFS logged `4 WS clients connected` when
our two came back, and nothing for the operator's own tabs, so the drop
was confined to the test Chrome. All three pages recovered on their own
(SPA hello after 2.4 s, status after 3 s). Cause unknown: the Network
domain reports a close without its code. From 08:56 the recorder installs
a WebSocket hook on every document (`ws_closed` records: code, reason,
`wasClean`), so the next such event says which side closed and how; 1006
would mean the connection died under the page, a clean 1000/1001 a peer
that said goodbye.

**Repeat at 09:18:47**, 29 min 45 s after the first: the same three
sockets (`/pfs/ws` on both SPA pages, `/guider/pfs-sv/status`), all with
code 1006 and `wasClean: false`, within 620 ms; the guider frame socket
and both imageweb sockets survived again, and PFS's count line (`3 WS
clients connected` as ours came back) shows the operator's own tabs were
untouched both times. What is known:

- Chrome opens each WebSocket on its own TCP connection: every handshake
  captured is `HTTP/1.1 101 Switching Protocols` from `Server: cloudflare`
  (CF-RAY `…-LAX`), no HTTP/2 multiplexing. So three separate TCP
  connections to the edge died in the same second, twice.
- The gateway's proxy leg is not it: aiohttp 3.14 turns the
  `ClientTimeout(total=10)` passed to `ws_connect` into a `ws_close`
  value only, `ws_receive` stays `None`, so nothing there times out a
  socket. (That argument is the wrong type and deprecated; a separate
  tidy-up.)
- The three victims were created within the same second (the page
  reloads at 08:29:02 and 08:58:46); the survivors were created at other
  moments (`/image/pfs/ws` is re-made every 2 min by finding 1, the
  guider frame socket a second later than the rest).

**Third time at 09:38:49**, and the rule is now exact — each of the
three sockets dies **20 minutes after it was opened**:

| opened | closed | lifetime |
|---|---|---|
| 08:29:02 (reload) | 08:49:02 | 20:00 |
| 08:58:46 (reload) | 09:18:47 | 20:01 |
| 09:18:49 (reconnect) | 09:38:49 | 20:00 |

and the discriminator is not when a socket was opened but **whether the
browser ever sends on it**. The three victims are the one-way channels:
the SPA sends `subscribe` once and then only listens; the guider's
status channel sends nothing at all. Every survivor carries
client-to-server bytes: the guider frame socket acks every frame, the
imageweb status socket pongs its server heartbeat every 20 s, and
`/image/pfs/ws` never lived 20 minutes. Something in the path (the
edge or the tunnel; the gateway has no such timer, and the operator's
own tabs on the same channels have the same traffic pattern, so they
must be dropping too, unnoticed, every 20 minutes) closes a WebSocket
that has carried no client-originated data for 20 minutes.

Fix (`5ef8747`): `heartbeat=20` on the gateway's proxied sockets
(`proxy_ws`), the same one line as finding 1 on the other side of the
gateway. The browser's pongs are the missing direction. Deployed with
the same ansible run as the chz1 change; the check is the same — no
`ws_closed` on `/pfs/ws` or `/guider/pfs-sv/status` twenty minutes after
a reload. The cloudflared log at those three timestamps would still say
which hop did it (`/opt/cloudflared/log/cloudflared.log` on sbs-inst1).

Recorder restarts: 08:54:29 (stop) and 08:55–08:56 (two attempts; the
first failed on a CDP parameter name). Each restart reloads every page
once, so the file carries an extra `navigated`/`hello` per page there —
not the pages' doing.

The camera-tab window reports `visibilityState: hidden` in every metrics
sample: it sits bottom-left of the 2×2 tiling and is covered by another
application's window. The SPA does not pause when hidden, so its data are
unaffected; a viewer page in that position would have stopped.

_(Final figures to be added when the run ends.)_

## Limits, and what would fix them

- **Clock offset.** The guider lag is arrival-time minus gcam's timestamp
  on another host. A one-line `time` field in gcamweb's `/status` (server
  wall clock at send) would let the report solve for the offset from the
  status channel's own round trips. Not done: outside this repo (zwo).
- **Client load.** The recorder's Chrome adds a full set of clients. For a
  clean night the operator's own tabs should be closed, or the test run
  from the debug profile alone.
- **DevTools overhead.** Every frame crosses the DevTools socket base64
  encoded; at the guider's 0.02 MB/s and one science frame per 5 min this
  is nothing, but a full-rate unbinned guider stream would be a different
  test.
- **One laptop.** The run measures the tunnel path from one machine; the
  numbers include that machine's Wi-Fi.
