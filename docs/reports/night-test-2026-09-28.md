# Night test — 2026-09-28, MIKE driven through the tunnel

**What was tested.** An emulated observing night on MIKE and its slit viewer
`mike-sv`, both simulated on sbs-inst1, with the observer on the web side:
`tools/nightdrive.py` worked the MIKE page (SPA, window tab) the way a person
does — typed into fields, picked from popups, pressed buttons, answered
MIKE's questions — over Chrome's DevTools port, so every command crossed
Access, the tunnel and the gateway. `tools/nighttest.py` recorded four pages
in the same Chrome: the MIKE window, the two Quick Look tabs (blue, red) and
the `mike-sv` guider page. Both socket heartbeats found on 09-17 were
deployed. Times are PDT (UTC−7).

- Recording: 00:04:59 → 06:23:09, 6.30 h, 248 396 records
  (`runs/nighttest-20260928.jsonl`, summary `runs/nighttest-20260928-report.{json,txt}`).
- Driver: 00:05:59 → 06:21:09 (`runs/nightdrive-20260928*.jsonl`, summary
  `runs/nightdrive-20260928-report.txt`; the first six minutes are in `-try1`).
- Chrome 154, `Chrome-nighttest` profile, on a laptop connected through the
  FortiClient VPN (full tunnel, `utun4`).

**Verdict.** The web side did its job all night. Every one of 186 readouts
reached its Quick Look. All 374 commands the page sent were acknowledged,
with a median of 121 ms through the tunnel. The pages rode out three MIKE
restarts and one gcam restart without a reload. No page crashed, leaked or
logged an error. The two tunnel cuts fixed after 09-17 are gone: no idle
cuts at 125 s and no 20-minute cuts, against 172 and 60 that night.

The night found one real bug, in MIKE's CCD simulator: it aborts on the
first unbinned object exposure. It is fixed in mike#17 (`03db236`) and was
re-tested the same night. It also found two deployment hazards.

## Key figures

| | value |
|---|---|
| Duration | 6.30 h recorded, 6.25 h driven |
| Files written | 186 (91 blue, 95 red), 0.96 GB: 184 at 2×2 (5.02 MB), 2 at 1×1 (18.4 MB) |
| Quick Look readouts received | blue 91 of 91, red 95 of 95 |
| Quick Look frames on the wire | 94 + 96 — 4 replays after reconnects |
| Guider frames | 18 804, 2.98 GB, 0.83 fps (the emulator's rate), 756×756 bin 2 |
| Guider frames missed | 13: 12 in one 17 s stall (04:29), 1 at 05:31; plus the planned 66 s gcam restart |
| Guider lag p50 / p90 / max | 0.49 / 0.71 / 17.5 s (clock offset included) |
| Guider decode p50 / p90 | 7.1 / 8.1 ms |
| Page controls worked / commands sent | 355 / 374, all acked; 4 failed (dead simulators) |
| Ack after send p50 / p90 / max | 121 / 195 / 385 ms |
| Questions answered | 19 (17 "Diffuser is not out … Continue?", 2 "abort exposure (Blue+Red)?") |
| Instrument state messages | 118.6 / min per page, flat |
| WebSocket closes | 104: 89 clean (1000) while MIKE was down, 15 unclean (1006) in 6 episodes |
| Idle (125 s) / twenty-minute cuts | 0 / 0 (09-17: 172 / 60) |
| Free space on sbs-inst1 | 28.9 → 28.1 GiB (86 → 87 %), 75 samples |
| Page crashes / console errors / heap growth | 0 / 0 / none |

## The night

| time | what the observer did |
|---|---|
| 00:12–00:22 | afternoon: 11 biases; 5 quartz flats at 20 s blue / 4 s red; 3 ThAr arcs; arcs at focus −20, +20, 0 on both arms; autofocus on and off |
| 00:22–02:06 | science, pass 1, an arc after each target: HD 10700 3×300 s; HIP 5364 2×900 s, paused 2 min in for 3 min ("clouds"); NGC 1068 1×1800 s; HD 20010 2×600 s aborted at 200 s, then 1×450 s |
| 02:06 | TOI-2525 at 1×1 fast: both simulators abort (finding 1) |
| 02:32 | MIKE restarted by hand (it starts fresh simulators) |
| 02:39 | HD 22049 2×300 s on the red arm alone |
| 02:44–02:46 | fix pushed to mike#17, CI 29 s, updater run: `MIKE-03db236.app` installed (finding 2) |
| 02:50–02:52 | planned stop/start: MIKE killed, relaunched on the new build, back on the page 3 s later; gcam `mike-sv` killed, relaunched, streaming 8 s later |
| 02:53 | WASP-18 2×(900 s blue, 600 s red) |
| 03:23 | MIKE and its simulators restarted together (finding 3) |
| 03:24–06:13 | science, pass 2; TOI-2525 at 1×1 fast now writes 2 × 18.4 MB |
| 06:14–06:21 | morning: 3 arcs, flats, 11 biases; window reset |

Every block set the exposure type, object and comment (they reach the FITS
headers), the time(s) and loops, then pressed Start. Before each block and
every ten minutes the driver sampled free space on MIKE's data disk over
ssh, with a floor of 10 GiB that would have ended the night. It never came
close.

## Findings

### 1. The simulator aborts on unbinned object frames — fixed

At 02:06:47 (red) and 02:06:51 (blue), each within a second of its `start`,
both `mikeserver`s died: `stack buffer overflow` in the unified log, no core
(AMFI refuses it). In `src/Simulator/rsim.c`, `create_spect()` fills
`double rho[N_SPECT+1]` with `n_spect` values. `n_spect` is made odd
upward, and for the last order at 1×1 it comes to `N_SPECT+2`, one more
than the array. The code is unchanged since v3.0 (2021). The simulator
rebuilt in mike#17 aborts on it (stack protector). At 2×2, the
binning used all evening, `n_spect` is 2001, which fits.

Fix `03db236`: `rho[N_SPECT+2]` and the rebuilt universal `mikeserver`. It was checked with a
harness that runs `do_rsim()` on a 1×1 object frame, built with
`-fstack-protector-all`: the old `rsim.c` exits 134 (abort), the new one
completes. It was re-tested on sbs-inst1 at 05:09 (TOI-2525, 1200 s, 1×1 fast):
two files, and both Quick Look pages showed the frame (544×1056 at bin 4).

What the web side saw of the failure is what it should see. MIKE's
status polls failed from 02:14, and the exposure ended at 02:26:50 with
`exposure loop failed` on both arms. The next four web commands came back
`hardware` with MIKE's own message (`TCPIP: opening socket failed`), and
MIKE showed no dialogs for them. MIKE's own loop failure did put one dialog
per arm on sbs-inst1's screen, as the Cocoa UI always has.

### 2. The updater deletes the build MIKE is running from

`instrument-updater.sh` drops the old PR folder before staging the new build
(`rm -rf "$pr_dir"`, "the PR folder holds at most one build"). At 02:46 it
deleted `pr-17/MIKE-2b878d3.app` under the running MIKE. MIKE carried on
from memory, but anything it loads from its bundle afterwards is gone, and
that includes `Resources/mikeserver`, which `run_setup` starts when no
simulator answers. Any push to an open PR does this to a running
instrument within 30 minutes, at the updater's next run. Suggested fix, in
lco-ansible: stage the new bundle beside the old one, and remove old
bundles only when `pgrep -f "$old.app/"` finds nothing.

### 3. A new MIKE adopts the simulators already running

`run_setup` opens the CCD server first and starts `mikeserver` only if
nothing answers. A kill leaves the simulators running (seen twice tonight).
By the code, so does Quit: only closing the main window stops them, and
only in Release builds (`windowWillClose:` → `CameraController shutdown`). After the 02:50 restart,
the new build was therefore talking to the old build's simulators, still
running from the deleted bundle. It took a second restart with them
stopped (03:23) to run the fix. This mirrors the telescope, where the CCD
servers outlive a MIKE restart. But on SBS, a simulator update needs
`mikeserver` stopped together with MIKE.

### 4. Socket closes: both 09-17 fixes hold; the rest is between browser and gateway

- **Idle cut (class 1):** none. The red frame socket lived 5.5 h. The blue
  one lived through a 62 min gap between frames (the simulators down, then
  the red arm alone) and a 30 min exposure.
- **Twenty-minute cut (class 2):** none. The SPA sockets and the guider's status
  socket lived for hours.
- **Clean closes:** all 89 code-1000 closes fall inside the three MIKE
  restarts. Each page retried every ~2.5 s, and the gateway accepted each
  socket and closed it cleanly while MIKE was away. Each page had its
  `hello` 1–3 s after MIKE listened again.
- **Unclean closes (1006):** 15, in six episodes:

| episode | sockets | age at close |
|---|---|---|
| 00:37:48–00:38:07 | guider status, guider frames | 33 min |
| 00:45:02–00:45:31 | MIKE window, blue Quick Look (`/mike/ws` and `/image/mike/blue/ws`), guider status | 7.6–40 min |
| 00:52:02–00:52:28 | blue and red status, MIKE window, blue and red frames | 7–47 min |
| 01:46:03 | guider frames | 68 min |
| 03:55:01 | blue frames | 3 h |
| 05:32:27–05:32:39 | guider status, red status | 4.7 h |

Unlike 09-17's same-second resets, each episode is spread over 10–30 s. In
every case the browser saw the close first. The gateway noticed 10–30 s
later, through its own heartbeat (`No PONG received after 10.0 seconds` in
`lco-gateway.log`), and imageweb through `client gone`. So neither of them
closed the socket. cloudflared re-registered no tunnel connection all night
(its log's last registration is 09-26), and this laptop logged no Wi-Fi
events at 00:37 or 00:45. At 04:28:53 the whole path paused instead:
nothing arrived on any of the eight sockets, on all four pages, for 17.5 s.
Then the held data arrived in a burst on the same sockets, with no close and
no reconnect. The SPA and status messages came late but complete. The guider
skipped 12 frames by design, since gcamweb sends only the newest one. A pause
across every socket at once points at something they share rather than at a
server. The path the
test cannot see into is the laptop's FortiClient full tunnel and the
firewall behind it. The next run should go without the VPN, or with
cloudflared at `--loglevel debug`, to separate the two.

## Pages

- **Guider `mike-sv`:** 18 804 frames at the emulator's 0.83 fps. gcam `streaming`
  in 22 516 of 22 583 status samples, and `unreachable` in the rest (the planned
  restart). After that restart the page kept its sockets, and frames resumed with
  gcam's counter reset (9418 → 1). Decode p50 7.1 ms at 756×756.
- **Quick Look:** one frame per readout on each arm's tab, each tab showing
  only its own arm. Every readout arrived (blue ids 3 → 93, red 3 → 97), and
  4 frames were replays after the reconnects above. Decode p50 3.5–3.7 ms.
  All six decodes over 0.5 s (0.9–1.5 s) came where the frame size changed:
  each page's first frame, the 1×1 frame, and the first 2×2 frame after it.
- **MIKE window:** 118.6 state messages a minute on each SPA page. `telescope`
  and `exposure` are at 1 Hz, `readout` and `shutter` per exposure. There were
  6 `hello`s on the window page: the first, three MIKE restarts, and two
  episodes above. MIKE logged up to 7 WS clients: three SPA pages, imageweb's
  control client, the gateway's health probe, and two more that were already
  connected before the test, probably the operator's own tabs.

| page | JS heap first → max → last (MB) | DOM nodes first → last | hidden samples | crashes |
|---|---|---|---|---|
| guider `mike-sv` | 1.9 → 2.7 → 2.4 | 1 482 → 3 672 | 0 / 757 | 0 |
| MIKE window | 1.6 → 2.4 → 1.5 | 8 502 → 4 847 | 0 / 757 | 0 |
| Quick Look blue | 5.5 → 5.5 → 2.5 | 18 873 → 7 591 | 0 / 757 | 0 |
| Quick Look red | 6.1 → 6.1 → 2.5 | 18 873 → 7 804 | 0 / 757 | 0 |

## Recorder and driver notes

- The driver ran in five parts, and the recorder once without a break. The
  parts are the first six minutes (`-try1`, then restarted to fix the gcam
  `pkill -f` pattern, which matched its own ssh shell), then 00:12–02:29,
  02:39–02:46, 02:50–02:53 and 03:24–06:21. The pauses between them were for
  finding 1 and for putting the fix in place. `report` in `nightdrive.py`
  reads the concatenation (`-all.jsonl`).
- The recorder's `cmds.failed` (23 on the window page) counts MIKE's 19
  questions: a question is an ack with `ok: false` and code `confirm`,
  answered by a resend. `nightdrive.py report` keeps them apart.
- The Quick Look pages ran the SPA's own tabs (`tab=quicklook`,
  `tab=quicklook_red`). Each tab mounts only its own iframe, so the
  recorder's embed probe read the right arm.
- MIKE writes to `/Users/obs1` (no `dbe_datapath1` set). The 186 test files
  are still there.

## What to do next

1. Merge mike#17 with `03db236`, or carry `rsim.c`'s one line to any other
   branch that builds the simulator.
2. lco-ansible: keep a running PR build on disk across updater runs (finding 2).
3. SBS runbook: to change the simulator, stop `mikeserver` along with MIKE
   (finding 3).
4. Separate the remaining resets: one night without the VPN, or with cloudflared
   at debug level.
5. Give MIKE on SBS a data directory in the pinned preferences, so test frames
   stop landing in the obs1 home, and delete tonight's 0.96 GB when it is no
   longer wanted.
