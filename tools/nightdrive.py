#!/usr/bin/env python3
"""Night driver: an observing night on MIKE, run through its SPA in the debug Chrome.

``nighttest.py`` records what the browser saw; this script is the observer.
Every step is what a person does on the page -- type into a field, pick from a
popup, press a button, answer the question the instrument asks -- dispatched
over DevTools into the SPA's window tab, so each command crosses Access, the
tunnel and the gateway like an observer's, and the recording carries it as the
page's own ``cmd``/``ack``. Waits read the page's topic store (``ws.js``): what
the page shows.

The night: afternoon biases, flats and arcs, a focus check, then science
targets with an arc after each until ``--science-until`` -- one paused for
clouds, one aborted and redone shorter, one at 1x1 fast, one on the red arm
alone, one with a different exposure time per arm -- MIKE and the slit
viewer's gcam stopped and started again at ``--restart-at``, and morning
calibrations. Before every exposure block, and every ten minutes, it samples
the free space of MIKE's data disk (over ssh) and of this machine, and ends the
night below the floors.

    uv run tools/nightdrive.py run --ssh sbsmac --out runs/nightdrive-20260928.jsonl
    uv run tools/nightdrive.py report runs/nightdrive-20260928.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time

from aiohttp import ClientSession

from nighttest import CDP, page_key

# -- the night -----------------------------------------------------------------------------------------------------

BIASES, FLATS, ARCS = 11, 5, 3
FLAT_EXPTIME = (20, 4)                 # blue, red: quartz
ARC_EXPTIME = 10                       # ThAr

# name, slit, exptime (s, or (blue, red)), loops, what else happens
TARGETS = [
    ("HD 10700", "0.70x5.00", 300, 3, {}),
    ("HIP 5364", "0.70x5.00", 900, 2, {"pause": (120, 180)}),       # clouds: pause 2 min in, for 3 min
    ("NGC 1068", "1.00x5.00", 1800, 1, {}),                          # 30 min without a readout
    ("HD 20010", "0.50x5.00", 600, 2, {"abort": 200, "redo": 450}),  # seeing: abort, one shorter
    ("TOI-2525", "0.70x5.00", 1200, 1, {"bin": 1, "speed": "fast"}),
    ("HD 22049", "0.35x5.00", 300, 2, {"arm": "red"}),
    ("WASP-18", "0.70x5.00", (900, 600), 2, {}),
]


def frame_bytes(b: int) -> int:
    """One arm's file from the simulator: 2048x4096 + 128 overscan columns + 128 bias rows, 16 bit."""
    return (2048 // b + 128) * (4096 // b + 128) * 2 + 2 * 2880


def estimate(science_s: float, readout_s: float) -> dict:
    """Frames and bytes the night will write, for the disk check before it starts."""
    cal = 2 * (BIASES + FLATS + ARCS) + 3                             # afternoon + morning, focus arcs
    cycle_s = cycle_frames = cycle_bytes = 0
    for _, _, exptime, loops, x in TARGETS:
        arms = 1 if x.get("arm") else 2
        n = loops + (1 if "redo" in x else 0)
        cycle_s += n * (max(exptime if isinstance(exptime, tuple) else (exptime,)) + readout_s) + ARC_EXPTIME + readout_s
        cycle_frames += n * arms + 2
        cycle_bytes += n * arms * frame_bytes(x.get("bin", 2)) + 2 * frame_bytes(2)
    cycles = science_s / cycle_s
    frames = cal * 2 + cycles * cycle_frames
    return {"frames": round(frames), "GB": round((cal * 2 * frame_bytes(2) + cycles * cycle_bytes) / 1e9, 2),
            "science_cycles": round(cycles, 2)}


# -- the page ------------------------------------------------------------------------------------------------------

# Runs first in every evaluation; hooks the page's ws.js once per document: the commands it sends and their acks,
# events, hellos, and window.confirm -- answered OK and remembered, as the observer would.
PRELUDE = """
const W = await import("/ws.js");
const N = window.__night || (window.__night = (() => {
  const n = { acks: [], sent: [], events: [], confirms: [], hellos: 0 };
  const keep = (a, x) => { a.push(x); if (a.length > 400) a.splice(0, 100); };
  W.onSend((f) => { if (f.type === "cmd") keep(n.sent, { t: Date.now(), id: f.id, name: f.name, args: f.args }); });
  W.onAck((a) => keep(n.acks, { t: Date.now(), id: a.id, ok: a.ok, error: a.error ?? null }));
  W.onEvent((e) => { if (e.name) keep(n.events, { t: Date.now(), name: e.name, data: e.data }); });
  W.onHello(() => { n.hellos += 1; });
  window.confirm = (m) => { keep(n.confirms, { t: Date.now(), m: String(m) }); return true; };
  return n;
})());
const q = (o) => document.querySelector(`#window-view [data-outlet="${o}"]`);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
"""

# One control, then the commands it caused and their acks (a "confirm" answer is resent, so wait for quiet).
ACT = """(async () => {""" + PRELUDE + """
const act = __ACT__, t0 = Date.now(), el = q(act.outlet);
if (!el) return { err: "no such outlet" };
if (el.disabled) return { err: "disabled" };
if (act.op === "click") el.click();
else if (act.op === "check") { el.checked = !!act.value; el.dispatchEvent(new Event("change", { bubbles: true })); }
else {
  const v = String(act.value);
  if (el.tagName === "SELECT" && ![...el.options].some((o) => o.value === v))
    return { err: "no such option", options: [...el.options].map((o) => o.value).slice(0, 30) };
  if (el.readOnly) return { err: "read-only" };
  el.value = v;
  el.dispatchEvent(new Event("change", { bubbles: true }));
}
for (let quiet = 0, i = 0; i < 60 && quiet < 3; i++) {
  await sleep(250);
  const sent = N.sent.filter((s) => s.t >= t0);
  quiet = sent.length && sent.every((s) => N.acks.some((a) => a.id === s.id)) ? quiet + 1 : 0;
}
const sent = N.sent.filter((s) => s.t >= t0);
return { sent, acks: N.acks.filter((a) => sent.some((s) => s.id === a.id)),
         confirms: N.confirms.filter((c) => c.t >= t0) };
})()"""

STATE = """(async () => {""" + PRELUDE + """
const g = (t) => W.topic(t).get() ?? null;
return { exposure: g("exposure"), mechanics: g("mechanics"), readout: g("readout"), disk: g("disk"),
         hellos: N.hellos, conn: document.getElementById("conn-state")?.textContent ?? null };
})()"""

EVENTS = """(async () => {""" + PRELUDE + """ return N.events.filter((e) => e.t >= __T0__); })()"""


class Page:
    """The SPA tab, found by URL each time the DevTools connection is (re)made."""

    def __init__(self, session: ClientSession, port: int, key: tuple):
        self.session, self.port, self.key = session, port, key
        self.cdp: CDP | None = None

    async def _connect(self) -> None:
        for i in range(3):
            try:
                async with self.session.get(f"http://127.0.0.1:{self.port}/json") as r:
                    t = next((t for t in await r.json() if t.get("type") == "page" and page_key(t["url"]) == self.key),
                             None)
                if t is None:
                    raise LookupError(f"no tab for {self.key}")
                self.cdp = CDP(self.session, t["webSocketDebuggerUrl"])
                await self.cdp.connect()
                return
            except Exception:
                if i == 2:
                    raise
                await asyncio.sleep(5)

    async def js(self, expr: str, retry: bool = True):
        """Evaluate in the page. A control is never retried: the first try may have acted."""
        for i in range(3 if retry else 1):
            if self.cdp is None or self.cdp.closed.is_set():
                await self._connect()
            try:
                r = await self.cdp.call("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)
            except Exception:
                await self.cdp.close()
                self.cdp = None
                if not retry or i == 2:
                    raise
                await asyncio.sleep(5)
                continue
            if "exceptionDetails" in r:
                d = r["exceptionDetails"]
                raise RuntimeError((d.get("exception") or {}).get("description") or d.get("text"))
            return r.get("result", {}).get("value")


def arms(s: dict) -> list:
    return ((s or {}).get("exposure") or {}).get("arms") or []


def idle(s: dict) -> bool:
    return not any(a.get("running") for a in arms(s))


def running(s: dict) -> bool:
    return any(a.get("running") for a in arms(s))


# -- the observer --------------------------------------------------------------------------------------------------


def at(hhmm: str) -> float:
    """The next local HH:MM, as a timestamp; "now" is now."""
    if hhmm == "now":
        return time.time()
    now = dt.datetime.now()
    t = now.replace(hour=int(hhmm[:2]), minute=int(hhmm[3:]), second=0, microsecond=0)
    return (t if t > now else t + dt.timedelta(days=1)).timestamp()


class Night:
    def __init__(self, args, session: ClientSession):
        self.args = args
        self.page = Page(session, args.port, (args.host, "/app.html", "/mike/ws", "window"))
        self.out = open(args.out, "a", buffering=1)
        self.stop = asyncio.Event()
        self.science_until, self.restart_at = at(args.science_until), at(args.restart_at)
        self.readout_s = 90.0              # per frame, until the biases measure it
        self.datapath = "/"
        self.written = self.files = 0

    def rec(self, ev: str, **f) -> None:
        now = time.time()
        r = {"t": round(now, 3), "iso": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(timespec="milliseconds"),
             "ev": ev, **f}
        self.out.write(json.dumps(r, separators=(",", ":"), default=str) + "\n")

    def say(self, text: str) -> None:
        print(f"{dt.datetime.now():%H:%M:%S} {text}", flush=True)

    async def sleep(self, secs: float) -> None:
        try:
            await asyncio.wait_for(self.stop.wait(), secs)
        except asyncio.TimeoutError:
            pass

    async def ssh(self, cmd: str, timeout: float = 60) -> str:
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", self.args.ssh, cmd]
        try:
            r = await asyncio.to_thread(subprocess.run, argv, capture_output=True, text=True, timeout=timeout)
            return r.stdout
        except Exception as e:
            self.rec("ssh_failed", cmd=cmd, error=repr(e))
            return ""

    # the page ------------------------------------------------------------------------------------------------------

    async def act(self, op: str, outlet: str, value=None) -> dict | None:
        if self.stop.is_set():
            return None
        try:
            r = await self.page.js(ACT.replace("__ACT__", json.dumps({"op": op, "outlet": outlet, "value": value})),
                                   retry=False)
        except Exception as e:
            self.rec("act_failed", op=op, outlet=outlet, value=value, error=repr(e))
            self.say(f"ALERT {op} {outlet}={value}: {e!r}")
            return None
        self.rec("act", op=op, outlet=outlet, value=value, **r)
        bad = [a for a in r.get("acks") or [] if not a.get("ok") and (a.get("error") or {}).get("code") != "confirm"]
        if r.get("err") or bad or not r.get("acks"):
            self.say(f"ALERT {op} {outlet}={value}: {r.get('err') or bad or 'no ack'}")
        return r

    async def state(self) -> dict | None:
        try:
            return await self.page.js(STATE)
        except Exception as e:
            self.rec("state_failed", error=repr(e))
            return None

    async def wait(self, pred, timeout: float, what: str, every: float = 3) -> dict | None:
        t0 = time.time()
        while not self.stop.is_set():
            s = await self.state()
            if s and s.get("exposure") and pred(s):
                return s
            if time.time() - t0 > timeout:
                self.rec("wait_timeout", what=what, timeout_s=round(timeout))
                self.say(f"ALERT timed out after {timeout:.0f} s waiting for {what}")
                return None
            await self.sleep(every)
        return None

    # disks ---------------------------------------------------------------------------------------------------------

    async def disk(self) -> bool:
        """Free space where MIKE writes and where the recording goes; below a floor the night ends."""
        f = (await self.ssh(f"df -k {shlex.quote(self.datapath)} | tail -1")).split()
        avail = int(f[3]) / 2**20 if len(f) > 4 and f[3].isdigit() else None       # 1K blocks -> GiB
        local = shutil.disk_usage(os.path.dirname(os.path.abspath(self.args.out))).free / 2**30
        s = await self.state()
        self.rec("disk", path=self.datapath, avail_gb=round(avail, 2) if avail is not None else None,
                 capacity=f[4] if len(f) > 4 else None, mike_level=((s or {}).get("disk") or {}).get("level"),
                 local_avail_gb=round(local, 1), written_mb=round(self.written / 1e6, 1), files=self.files)
        low = (avail is not None and avail < self.args.disk_floor_gb) or local < self.args.local_floor_gb
        if low and not self.stop.is_set():
            self.rec("disk_floor", avail_gb=avail, local_avail_gb=local)
            self.say(f"ALERT disk below the floor (instrument {avail} GiB, local {local:.1f} GiB): ending the night")
            self.stop.set()
        return not low

    async def disk_sampler(self) -> None:
        while not self.stop.is_set():
            await self.sleep(600)
            if not self.stop.is_set():
                await self.disk()

    async def sizes(self, paths: list[str]) -> list[int | None]:
        if not paths:
            return []
        out = await self.ssh("stat -f '%z %N' " + " ".join(shlex.quote(p) for p in paths) + " 2>/dev/null")
        got = {line.split(" ", 1)[1]: int(line.split(" ", 1)[0]) for line in out.splitlines() if " " in line}
        return [got.get(p) for p in paths]

    # instrument steps ------------------------------------------------------------------------------------------------

    async def lamp(self, pos: str) -> None:
        await self.act("set", "popup_lamp", pos)
        await self.wait(lambda s: s["mechanics"]["lamp"]["position"] == pos and not s["mechanics"]["lamp"]["busy"],
                        120, f"lamp {pos}")

    async def slit(self, name: str) -> None:
        await self.act("set", "popup_slit", name)
        await self.wait(lambda s: s["mechanics"]["slit"]["name"] == name and not s["mechanics"]["slit"]["moving"],
                        180, f"slit {name}")

    async def readout(self, b: int, speed: str) -> None:
        # Each binning popup sends the other axis from the page's topic: wait for one before the next.
        for i, c in enumerate("BR"):
            for key in ("binx", "biny"):
                await self.act("set", f"popup_{key}{c}", b)
                await self.wait(lambda s: s["readout"]["arms"][i][key] == b, 30, f"{key} {b} ({c})")
            await self.act("set", f"popup_speed{c}", speed)
        await self.wait(lambda s: all(a["speed"] == speed for a in s["readout"]["arms"]), 30, f"speed {speed}")

    async def expose(self, label: str, etype: str, obj: str, exptime, loops: int, arm: str = "both",
                     comment: str = "", pause: tuple | None = None, abort: float | None = None) -> float | None:
        """One block: type, object, comment, time, loops, Start; then wait it out. Returns its duration."""
        if self.stop.is_set() or not await self.disk():
            return None
        if not await self.wait(idle, 900, f"idle before {label}"):
            return None
        col = {"both": "2", "blue": "B", "red": "R"}[arm]
        await self.act("set", "popup_exptype", etype)
        await self.act("set", "edit_object", obj)
        await self.act("set", "edit_instcom", comment)
        if isinstance(exptime, tuple):
            await self.act("set", "edit_exptimeB", exptime[0])
            await self.act("set", "edit_exptimeR", exptime[1])
        elif exptime is not None:              # a bias leaves the time alone; the CCD server reads 0
            await self.act("set", f"edit_exptime{col}", exptime)
        await self.act("set", f"edit_loops{col}", loops)
        t0 = time.time()
        await self.act("click", f"but_start{col}")
        if not await self.wait(running, 30, f"{label} to start", every=1):
            return None
        self.say(f"{label}: {obj} {etype} {exptime} s x{loops} ({arm})")
        longest = max(exptime) if isinstance(exptime, tuple) else (exptime or 0)
        budget = loops * (longest + self.readout_s + 30) + 300
        if pause:
            after, hold = pause
            await self.sleep(after)
            await self.act("click", f"but_pause{col}")
            await self.wait(lambda s: all(a["paused"] for a in arms(s) if a["running"]), 30, "pause")
            await self.sleep(hold)
            await self.act("click", f"but_pause{col}")
            await self.wait(lambda s: not any(a["paused"] for a in arms(s)), 30, "resume")
            budget += hold
        if abort:
            await self.sleep(abort)
            await self.act("click", f"but_abort{col}")
        s = await self.wait(idle, budget * 1.3, f"{label} to finish", every=5)
        if s is None and not self.stop.is_set():
            await self.act("click", f"but_abort{col}")     # stuck: abort, as the observer would
            s = await self.wait(idle, 300, f"{label} after abort")
        if self.stop.is_set():                             # the disk floor: stop writing
            await self.act("click", f"but_abort{col}")
        dur = time.time() - t0
        try:
            ev = await self.page.js(EVENTS.replace("__T0__", str(int(t0 * 1000))))
        except Exception as e:
            ev = []
            self.rec("events_failed", error=repr(e))
        paths = [p for e in ev if e["name"] == "exposure_complete" for p in (e.get("data") or {}).get("fits_paths") or []]
        sizes = await self.sizes(paths)
        self.files += sum(1 for n in sizes if n)
        self.written += sum(n for n in sizes if n)
        self.rec("block", label=label, type=etype, object=obj, exptime=exptime, loops=loops, arm=arm,
                 dur_s=round(dur, 1), events=len(ev), files=paths, bytes=sizes,
                 missing=[p for p, n in zip(paths, sizes) if n is None],
                 ids=[a.get("id") for a in arms(s)] if s else None, aborted=bool(abort))
        self.say(f"{label}: done in {dur:.0f} s, {len(paths)} files, {sum(n for n in sizes if n) / 1e6:.0f} MB")
        return dur

    async def arcs(self, n: int, label: str = "arcs") -> None:
        await self.lamp("thar")
        await self.expose(label, "comp", "ThAr", ARC_EXPTIME, n)
        await self.lamp("off")

    async def flats(self) -> None:
        await self.lamp("quartz")
        await self.expose("flats", "flat", "milky flat", FLAT_EXPTIME, FLATS)
        await self.lamp("off")

    async def focus_check(self) -> None:
        """Arcs at three focus settings per arm, back to the start; autofocus on and off."""
        s = await self.state()
        if not s:
            return
        f0 = [f["value"] for f in s["mechanics"]["focus"]]
        await self.lamp("thar")
        for d in (-20, 20, 0):
            for c, v in zip("BR", f0):
                await self.act("set", f"edit_focus{c}", round(v + d, 1))
            await self.wait(lambda s: not any(f["moving"] for f in s["mechanics"]["focus"]), 120, "focus")
            await self.expose(f"focus {d:+d}", "comp", "ThAr focus", ARC_EXPTIME, 1)
        await self.lamp("off")
        for on in (True, False):
            for c in "BR":
                await self.act("check", f"but_focus{c}", on)
        await self.wait(lambda s: not any(f["autofocus"] for f in s["mechanics"]["focus"]), 30, "autofocus off")

    async def science(self, target: tuple, npass: int) -> None:
        name, slit, exptime, loops, x = target
        arm, comment = x.get("arm", "both"), f"night test, pass {npass}"
        await self.slit(slit)
        if "bin" in x:
            await self.readout(x["bin"], x.get("speed", "slow"))
        await self.expose(name, "object", name, exptime, loops, arm, comment, pause=x.get("pause"), abort=x.get("abort"))
        if "redo" in x:
            await self.expose(f"{name} redo", "object", name, x["redo"], 1, arm, comment)
        if "bin" in x:
            await self.readout(2, "slow")

    def target_s(self, target: tuple) -> float:
        _, _, exptime, loops, x = target
        n = loops + (1 if "redo" in x else 0)
        return (n * (max(exptime if isinstance(exptime, tuple) else (exptime,)) + self.readout_s + 30)
                + x.get("pause", (0, 0))[1] + ARC_EXPTIME + 2 * self.readout_s + 120)

    # stop and start ----------------------------------------------------------------------------------------------------

    async def restart(self, what: str, pattern: str, app: str, back) -> None:
        """Stop an app (SIGTERM, as after a crash: nobody is at the Mac to answer Quit), start it as obs1."""
        before = (await self.ssh(f"pgrep {pattern}")).split()
        self.rec("stopping", what=what, pids=before)
        self.say(f"stopping {what} {before}")
        await self.ssh(f"sudo -n pkill -TERM {pattern}")
        for _ in range(30):
            if not (await self.ssh(f"pgrep {pattern}")).strip():
                break
            await self.sleep(1)
        self.rec("stopped", what=what)
        await self.sleep(60)
        await self.ssh(f"sudo -n launchctl asuser $(id -u obs1) sudo -u obs1 open -a {shlex.quote(app)}")
        t0 = time.time()
        self.rec("started", what=what, app=app)
        ok = await back()
        self.rec("back" if ok else "not_back", what=what, after_s=round(time.time() - t0, 1),
                 pids=(await self.ssh(f"pgrep {pattern}")).split())
        self.say(f"{what} {'back' if ok else 'ALERT not back'} after {time.time() - t0:.0f} s")

    async def restart_apps(self) -> None:
        if not await self.wait(idle, 900, "idle before the restart"):
            return
        app = (await self.ssh("ps -axo command | sed -n 's#^\\(/.*\\.app\\)/Contents/MacOS/MIKE$#\\1#p' | head -1")).strip()
        if not app:
            self.say("ALERT MIKE is not running; no restart")
            return
        # the newest build beside it: the updater may have installed one since MIKE started
        newest = (await self.ssh(f"ls -td {shlex.quote(os.path.dirname(app))}/MIKE-*.app | head -1")).strip() or app
        self.rec("mike_build", running=app, launching=newest)
        h0 = ((await self.state()) or {}).get("hellos", 0)

        async def mike_back() -> bool:            # a new hello on the page, both arms ready
            return await self.wait(lambda s: s["hellos"] > h0 and all(a["ready"] for a in arms(s)), 240,
                                   "MIKE back on the page") is not None

        await self.restart("MIKE", "-x MIKE", newest, mike_back)

        async def gcam_back() -> bool:            # the gateway's health: gcamweb sees gcam stream again
            for _ in range(48):
                h = await self.ssh(f"curl -s --max-time 5 127.0.0.1:{self.args.gateway_port}/healthz")
                try:
                    c = next(c for c in json.loads(h)["checks"] if c["name"] == self.args.guider)
                    if c["up"] and "streaming" in c.get("detail", ""):
                        return True
                except (ValueError, KeyError, StopIteration):
                    pass
                await self.sleep(5)
            return False

        ini = f"/usr/local/etc/gcam/{self.args.guider}.ini"
        # [g]: the pattern must not match the remote shell that carries it
        await self.restart(self.args.guider, f"-f {shlex.quote('[g]camzwo -f ' + ini)}",
                           f"/Applications/gcam-{self.args.guider}.app", gcam_back)

    # the night -------------------------------------------------------------------------------------------------------

    async def run(self) -> None:
        s = await self.wait(idle, 900, "the MIKE page, idle")
        if not s:
            return
        self.datapath = (s.get("disk") or {}).get("datapath") or "/"
        est = estimate(self.science_until - time.time(), self.readout_s)
        self.rec("start", argv=sys.argv[1:], estimate=est, science_until=self.args.science_until,
                 restart_at=self.args.restart_at)
        self.say(f"night starts; science until {self.args.science_until}, restart at {self.args.restart_at}; "
                 f"expect ~{est['frames']} files, {est['GB']} GB in {self.datapath}")
        if not await self.disk():
            return
        sampler = asyncio.create_task(self.disk_sampler())
        # afternoon
        await self.lamp("off")
        await self.readout(2, "slow")
        for c in "BR":
            await self.act("set", f"popup_rmode{c}", "full")
        await self.slit("1.00x5.00")
        if not self.args.science_only:
            d = await self.expose("biases", "bias", "bias", None, BIASES)
            if d:
                self.readout_s = max(20.0, d / BIASES)
                self.rec("readout_s", s=round(self.readout_s, 1))
            await self.flats()
            await self.arcs(ARCS)
            await self.focus_check()
        # night
        i, restarted = self.args.first_target, False
        while not self.stop.is_set():
            if not restarted and time.time() >= self.restart_at:
                await self.restart_apps()
                restarted = True
            fits = [k for k in range(len(TARGETS))
                    if time.time() + self.target_s(TARGETS[(i + k) % len(TARGETS)]) < self.science_until]
            if not fits:
                break
            i += fits[0]
            await self.science(TARGETS[i % len(TARGETS)], i // len(TARGETS) + 1)
            await self.arcs(1)
            i += 1
        # morning
        await self.arcs(ARCS)
        await self.flats()
        await self.expose("biases", "bias", "bias", None, BIASES)
        for op, outlet, v in (("set", "popup_exptype", "object"), ("set", "edit_object", ""),
                              ("set", "edit_instcom", "")):
            await self.act(op, outlet, v)
        await self.lamp("off")
        sampler.cancel()
        await self.disk()
        self.rec("end", files=self.files, written_mb=round(self.written / 1e6, 1))
        self.say(f"night over: {self.files} files, {self.written / 1e6:.0f} MB")


async def main_async(args) -> None:
    async with ClientSession() as session:
        night = Night(args, session)
        interrupted = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: (interrupted.set(), night.stop.set()))
        try:
            await night.run()
        finally:
            night.out.close()
        if args.recorder_pid and not interrupted.is_set():   # the night ended: the recording too, after the
            await asyncio.sleep(120)                          # last readout's frames reach the pages
            try:
                os.kill(args.recorder_pid, signal.SIGINT)
            except ProcessLookupError:
                pass


# -- offline report ------------------------------------------------------------------------------------------------


def report(path: str) -> None:
    recs = [json.loads(line) for line in open(path) if line.strip()]
    if not recs:
        print("empty file")
        return
    by: dict[str, list] = {}
    for r in recs:
        by.setdefault(r["ev"], []).append(r)
    print(f"{path}: {recs[0]['iso']} -> {recs[-1]['iso']} ({(recs[-1]['t'] - recs[0]['t']) / 3600:.2f} h)")
    acts = by.get("act", [])
    sent = {s["id"]: s for r in acts for s in r.get("sent") or []}
    acks = [a for r in acts for a in r.get("acks") or []]
    lat = sorted(a["t"] - sent[a["id"]]["t"] for a in acks if a["id"] in sent)
    failed = [(r["outlet"], r.get("value"), a.get("error")) for r in acts for a in r.get("acks") or []
              if not a["ok"] and (a.get("error") or {}).get("code") != "confirm"]
    asked = [c["m"] for r in acts for c in r.get("confirms") or []]
    print(f"controls: {len(acts)}, refused by the page: {sum(1 for r in acts if r.get('err'))}; commands sent "
          f"{len(sent)}, acked {len(acks)}, failed {len(failed)}; questions answered {len(asked)}")
    if lat:
        print(f"ack after send (ms): p50 {lat[len(lat) // 2]}, p90 {lat[int(len(lat) * 0.9)]}, max {lat[-1]}")
    for q, n in sorted(((q, asked.count(q)) for q in set(asked)), key=lambda x: -x[1]):
        print(f"  asked {n}x: {q}")
    for f in failed:
        print(f"  failed: {f}")
    blocks = by.get("block", [])
    files = [n for b in blocks for n in b["bytes"]]
    print(f"blocks: {len(blocks)}, files {sum(1 for n in files if n)}, {sum(n for n in files if n) / 1e9:.2f} GB, "
          f"missing {sum(len(b['missing']) for b in blocks)}")
    for b in blocks:
        print(f"  {b['iso'][11:19]} {b['label']:<16} {b['type']:<6} {str(b['exptime']):>10} x{b['loops']:<2} {b['arm']:<4}"
              f" {b['dur_s']:>7.0f} s  {len(b['files']):>2} files  ids {b['ids']}")
    for r in by.get("stopping", []) + by.get("stopped", []) + by.get("started", []) + by.get("back", []) + \
            by.get("not_back", []):
        print(f"  {r['iso'][11:19]} {r['ev']} {r['what']} {r.get('after_s', '')}")
    disk = [r for r in by.get("disk", []) if r.get("avail_gb") is not None]
    if disk:
        print(f"disk ({disk[0]['path']}): free {disk[0]['avail_gb']} -> {disk[-1]['avail_gb']} GiB "
              f"(min {min(r['avail_gb'] for r in disk)}), {disk[0]['capacity']} -> {disk[-1]['capacity']} used, "
              f"{len(disk)} samples; local free min {min(r['local_avail_gb'] for r in disk)} GiB")
    for ev in ("wait_timeout", "act_failed", "state_failed", "events_failed", "ssh_failed", "disk_floor"):
        for r in by.get(ev, []):
            print(f"  {r['iso'][11:19]} {ev} {r.get('what') or r.get('outlet') or ''} {r.get('error', '')}"[:160])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="drive the night; stops at the end, on SIGINT, or at a disk floor")
    r.add_argument("--port", type=int, default=9222, help="Chrome remote debugging port")
    r.add_argument("--host", default="sbs.chimera.observer")
    r.add_argument("--ssh", required=True, help="ssh destination of the instrument Mac (disk, restarts)")
    r.add_argument("--guider", default="mike-sv", help="the slit viewer (gcam ini and healthz name)")
    r.add_argument("--gateway-port", type=int, default=8080, help="the gateway's port on the instrument Mac")
    r.add_argument("--science-until", default="06:30", help="local HH:MM; morning calibrations follow")
    r.add_argument("--restart-at", default="03:00", help="local HH:MM; MIKE and gcam stopped and started")
    r.add_argument("--disk-floor-gb", type=float, default=10, help="end the night below this free on MIKE's disk")
    r.add_argument("--local-floor-gb", type=float, default=5, help="and below this free here")
    r.add_argument("--science-only", action="store_true", help="skip the afternoon calibrations (a resumed night)")
    r.add_argument("--first-target", type=int, default=0, help="index into TARGETS to start the science at")
    r.add_argument("--recorder-pid", type=int, help="nighttest.py record to stop (SIGINT) when the night ends")
    r.add_argument("--out", default=f"nightdrive-{dt.datetime.now():%Y%m%d-%H%M}.jsonl")
    p = sub.add_parser("report", help="summarise a night")
    p.add_argument("file")
    args = ap.parse_args()
    if args.cmd == "report":
        report(args.file)
        return
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
