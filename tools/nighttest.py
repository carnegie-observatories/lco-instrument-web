#!/usr/bin/env python3
"""Night-test recorder: what the browser saw, over Chrome's DevTools port.

Three pages make up an observing session on the web side -- the instrument
SPA, its quick-look, and a guider viewer. This script attaches to each one in
a Chrome started with ``--remote-debugging-port`` and writes one JSON record
per line for everything that crosses their WebSockets (with the chz1 frame
header parsed out of every binary frame), plus page metrics, console errors,
crashes and reconnects. The file is the deliverable: ``report`` reads it back
offline and prints frames received, drops, lag, reconnects and errors.

    uv run tools/nighttest.py record --out runs/night.jsonl
    uv run tools/nighttest.py report runs/night.jsonl

Chrome (136+) refuses a debugging port on its default profile, so start a
second instance with its own ``--user-data-dir`` and log in to Access there
once; the cookie survives in that profile. The recorder waits for the login,
then puts each page in its own window -- the viewer pauses when its tab is
hidden, so three tabs in one window would record two paused pages.

Nothing here talks to the instruments: every byte comes from the browser.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import datetime as dt
import json
import signal
import struct
import sys
import time
from collections import Counter, defaultdict
from urllib.parse import parse_qs, unquote, urlparse

from aiohttp import ClientSession, WSMsgType

MAGIC = b"CHZ1"


# -- pages ---------------------------------------------------------------------------------------------------------


def default_pages(host: str, instrument: str, guider: str) -> list[str]:
    """The pages opened when the debug Chrome has none: the SPA on its camera tab, the SPA on
    its Quick Look tab (which embeds the imageweb viewer in an iframe), and the guider viewer."""
    return [
        f"https://{host}/app.html?ws_path=%2F{instrument}%2Fws&tab=camera",
        f"https://{host}/app.html?ws_path=%2F{instrument}%2Fws&tab=quicklook",
        f"https://{host}/guider/{guider}/",
    ]


def classify(url: str) -> dict | None:
    """A page target's name and kind from its URL, or None if it is not one of ours."""
    u = urlparse(url)
    q = parse_qs(u.query)
    parts = [p for p in u.path.split("/") if p]
    if u.path == "/app.html" and q.get("ws_path"):
        inst = unquote(q["ws_path"][0]).strip("/").split("/")[0]
        tab = q.get("tab", [""])[0]
        return {"name": f"{inst}:{tab}" if tab else inst, "kind": "instrument", "url": url}
    if len(parts) == 2 and parts[0] == "image":
        return {"name": f"{parts[1]}-quicklook", "kind": "quicklook", "url": url}
    if len(parts) == 2 and parts[0] == "guider":
        return {"name": parts[1], "kind": "guider", "url": url}
    return None


def page_key(url: str) -> tuple:
    """What identifies a page target: host, path, and for the SPA its ws_path and tab."""
    u = urlparse(url)
    q = parse_qs(u.query)
    return (u.netloc, u.path, unquote(q.get("ws_path", [""])[0]), q.get("tab", [""])[0])


# -- CDP client ----------------------------------------------------------------------------------------------------


class CDP:
    """One DevTools connection: ``call`` for commands, ``on`` for events."""

    def __init__(self, session: ClientSession, url: str):
        self.session, self.url = session, url
        self.ws = None
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._handlers: dict[str, list] = defaultdict(list)
        self.closed = asyncio.Event()

    async def connect(self) -> None:
        # Frames arrive base64-encoded inside CDP events; a quick-look frame is several MB.
        self.ws = await self.session.ws_connect(self.url, max_msg_size=0)
        asyncio.create_task(self._reader())

    async def _reader(self) -> None:
        try:
            async for msg in self.ws:
                if msg.type != WSMsgType.TEXT:
                    break
                m = json.loads(msg.data)
                if "id" in m:
                    fut = self._pending.pop(m["id"], None)
                    if fut and not fut.done():
                        if "error" in m:
                            fut.set_exception(RuntimeError(m["error"].get("message", str(m["error"]))))
                        else:
                            fut.set_result(m.get("result", {}))
                else:
                    for h in self._handlers.get(m["method"], ()):
                        try:
                            h(m.get("params", {}))
                        except Exception as e:  # a bad handler must not kill the reader
                            print(f"handler {m['method']}: {e!r}", file=sys.stderr)
        finally:
            self.closed.set()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("devtools connection closed"))

    async def call(self, method: str, **params):
        self._id += 1
        fut = asyncio.get_running_loop().create_future()
        self._pending[self._id] = fut
        await self.ws.send_str(json.dumps({"id": self._id, "method": method, "params": params}))
        return await asyncio.wait_for(fut, 30)

    def on(self, method: str, handler) -> None:
        self._handlers[method].append(handler)

    async def close(self) -> None:
        if self.ws and not self.ws.closed:
            await self.ws.close()


# -- the recorder --------------------------------------------------------------------------------------------------


class Recorder:
    def __init__(self, args):
        self.args = args
        self.host = args.host
        self.pages: list[dict] = []
        self.out = open(args.out, "a", buffering=1)
        self.n_records = 0
        self.stop = asyncio.Event()
        self.session: ClientSession | None = None
        self.counts: Counter = Counter()

    # records ------------------------------------------------------------------------------------------------------

    def rec(self, page: str, ev: str, **fields) -> None:
        now = time.time()
        r = {"t": round(now, 3), "iso": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(timespec="milliseconds"),
             "page": page, "ev": ev, **fields}
        self.out.write(json.dumps(r, separators=(",", ":"), default=str) + "\n")
        self.n_records += 1
        self.counts[(page, ev)] += 1

    # chrome ---------------------------------------------------------------------------------------------------------

    async def targets(self) -> list[dict]:
        async with self.session.get(f"http://127.0.0.1:{self.args.port}/json") as r:
            return [t for t in await r.json() if t.get("type") == "page"]

    async def browser(self) -> CDP:
        async with self.session.get(f"http://127.0.0.1:{self.args.port}/json/version") as r:
            v = await r.json()
        b = CDP(self.session, v["webSocketDebuggerUrl"])
        await b.connect()
        return b, v

    async def wait_for_login(self) -> None:
        """Access sends every page to its login until the profile holds a cookie."""
        told = 0.0
        while not self.stop.is_set():
            ts = await self.targets()
            if any(urlparse(t["url"]).netloc == self.host for t in ts):
                return
            if time.time() - told > 30:
                told = time.time()
                where = {urlparse(t["url"]).netloc for t in ts} or {"(no page targets)"}
                print(f"waiting for the Access login in the debug Chrome (tabs are on {', '.join(sorted(where))})",
                      flush=True)
                self.rec("-", "waiting_login", tabs=sorted(where))
            await asyncio.sleep(3)

    async def ensure_windows(self) -> dict[str, str]:
        """Find our pages among the tabs (opening the defaults if there are none), give each its
        own window, and tile the windows so none is fully covered -- Chrome treats a covered window
        as hidden, and a hidden viewer stops streaming. Returns page name -> targetId."""
        b, _ = await self.browser()
        try:
            ts = await self.targets()
            keep: dict[str, dict] = {}
            for t in ts:
                p = classify(t["url"]) if urlparse(t["url"]).netloc == self.host else None
                if p and p["name"] not in keep:
                    keep[p["name"]] = {"target": t, **p}
                elif p or urlparse(t["url"]).netloc.endswith("cloudflareaccess.com"):
                    await b.call("Target.closeTarget", targetId=t["id"])  # a duplicate, or a stale login page
                    self.rec(p["name"] if p else "-", "closed_tab", url=t["url"])
            if not keep:
                for url in default_pages(self.host, self.args.instrument, self.args.guider):
                    r = await b.call("Target.createTarget", url=url, newWindow=True)
                    p = classify(url)
                    keep[p["name"]] = {"target": {"id": r["targetId"], "url": url}, **p}
                    self.rec(p["name"], "opened_window", url=url)
            # Pages sharing a window are hidden tabs; reopen them in windows of their own.
            windows: dict[int, str] = {}
            for name, p in keep.items():
                t = p["target"]
                win = (await b.call("Browser.getWindowForTarget", targetId=t["id"]))["windowId"]
                if win in windows:
                    await b.call("Target.closeTarget", targetId=t["id"])
                    r = await b.call("Target.createTarget", url=t["url"], newWindow=True)
                    p["target"] = {"id": r["targetId"], "url": t["url"]}
                    win = (await b.call("Browser.getWindowForTarget", targetId=r["targetId"]))["windowId"]
                    self.rec(name, "opened_window", url=t["url"])
                windows[win] = name
            if self.args.tile:
                await self.tile(b, list(windows), next(iter(keep.values()))["target"])
            self.pages = [{k: v for k, v in p.items() if k != "target"} for p in keep.values()]
            return {name: p["target"]["id"] for name, p in keep.items()}
        finally:
            await b.close()

    async def tile(self, b: CDP, windows: list[int], any_target: dict) -> None:
        """Two columns, as many rows as needed, on the screen of one of the pages."""
        page = CDP(self.session, any_target.get("webSocketDebuggerUrl") or
                   f"ws://127.0.0.1:{self.args.port}/devtools/page/{any_target['id']}")
        await page.connect()
        try:
            r = await page.call("Runtime.evaluate", expression="({w: screen.availWidth, h: screen.availHeight})",
                                returnByValue=True)
            scr = r["result"]["value"]
        finally:
            await page.close()
        cols, rows = 2, (len(windows) + 1) // 2
        w, h = scr["w"] // cols, scr["h"] // rows
        for i, win in enumerate(windows):
            await b.call("Browser.setWindowBounds", windowId=win,
                         bounds={"left": (i % cols) * w, "top": (i // cols) * h, "width": w, "height": h,
                                 "windowState": "normal"})

    # one page -------------------------------------------------------------------------------------------------------

    async def watch_page(self, page: dict) -> None:
        """Attach, record until the connection drops, reattach. Runs for the whole test."""
        name = page["name"]
        while not self.stop.is_set():
            target = next((t for t in await self.targets() if page_key(t["url"]) == page_key(page["url"])), None)
            if target is None:
                self.rec(name, "target_missing")
                try:
                    await self.ensure_windows()
                except Exception as e:
                    self.rec(name, "ensure_failed", error=repr(e))
                await asyncio.sleep(5)
                continue
            cdp = CDP(self.session, target["webSocketDebuggerUrl"])
            try:
                await cdp.connect()
                await self.attach(cdp, page)
                self.rec(name, "attached", target=target["id"], url=target["url"])
                waiter = asyncio.create_task(self.stop.wait())
                done, _ = await asyncio.wait([asyncio.create_task(cdp.closed.wait()), waiter],
                                             return_when=asyncio.FIRST_COMPLETED)
                waiter.cancel()
                if not self.stop.is_set():
                    self.rec(name, "detached")
            except Exception as e:
                self.rec(name, "attach_failed", error=repr(e))
            finally:
                await cdp.close()
            if not self.stop.is_set():
                await asyncio.sleep(5)

    async def attach(self, cdp: CDP, page: dict) -> None:
        name, kind = page["name"], page["kind"]
        sockets: dict[str, str] = {}  # requestId -> channel (the ws url's path)

        def channel(p) -> str:
            return sockets.get(p.get("requestId"), "?")

        def on_ws_created(p):
            # The whole path: the SPA's Quick Look tab has /pfs/ws and /image/pfs/ws side by side.
            sockets[p["requestId"]] = urlparse(p["url"]).path
            self.rec(name, "ws_open", ch=sockets[p["requestId"]], url=p["url"])

        def on_ws_closed(p):
            self.rec(name, "ws_close", ch=channel(p))
            sockets.pop(p["requestId"], None)

        def on_ws_error(p):
            self.rec(name, "ws_error", ch=channel(p), error=p.get("errorMessage"))

        def on_frame(p):
            ch, r = channel(p), p["response"]
            data, opcode = r.get("payloadData", ""), r.get("opcode")
            if opcode == 2:  # binary: a chz1 frame; only the header is decoded
                self.rec(name, "frame", ch=ch, ts=p["timestamp"], **chz1_header(data))
            else:
                self.on_text(name, kind, ch, data, p["timestamp"])

        def on_sent(p):
            ch, r = channel(p), p["response"]
            if r.get("opcode") != 1:
                return
            try:
                m = json.loads(r["payloadData"])
            except ValueError:
                return
            t = m.get("type")
            if t == "ack":
                self.rec(name, "ack_sent", ch=ch, seq=m.get("seq"), **(m.get("client") or {}))
            elif t == "cmd":
                self.rec(name, "cmd", ch=ch, id=m.get("id"), name=m.get("name"), args=m.get("args"))
            else:
                self.rec(name, "sent", ch=ch, type=t, bytes=len(r["payloadData"]))

        def on_console(p):
            if p.get("type") in ("error", "warning", "assert"):
                args = " ".join(str(a.get("value", a.get("description", a.get("type")))) for a in p.get("args", []))
                self.rec(name, "console", level=p["type"], text=args[:500])

        def on_exception(p):
            d = p.get("exceptionDetails", {})
            self.rec(name, "exception", text=(d.get("exception", {}).get("description") or d.get("text", ""))[:500])

        def on_log(p):
            e = p.get("entry", {})
            if e.get("level") in ("error", "warning"):
                self.rec(name, "log", level=e["level"], source=e.get("source"), text=e.get("text", "")[:500],
                         url=e.get("url"))

        def on_navigated(p):
            f = p.get("frame", {})
            if not f.get("parentId"):
                self.rec(name, "navigated", url=f.get("url"))

        def on_binding(p):
            if p.get("name") != "__nighttest":
                return
            try:
                m = json.loads(p["payload"])
            except ValueError:
                return
            self.rec(name, m.pop("ev", "hook"), **fields(m))

        cdp.on("Runtime.bindingCalled", on_binding)
        cdp.on("Network.webSocketCreated", on_ws_created)
        cdp.on("Network.webSocketClosed", on_ws_closed)
        cdp.on("Network.webSocketFrameError", on_ws_error)
        cdp.on("Network.webSocketFrameReceived", on_frame)
        cdp.on("Network.webSocketFrameSent", on_sent)
        cdp.on("Runtime.consoleAPICalled", on_console)
        cdp.on("Runtime.exceptionThrown", on_exception)
        cdp.on("Log.entryAdded", on_log)
        cdp.on("Page.frameNavigated", on_navigated)
        cdp.on("Page.loadEventFired", lambda p: self.rec(name, "loaded"))
        cdp.on("Inspector.targetCrashed", lambda p: self.rec(name, "crash"))
        for dom in ("Network", "Runtime", "Log", "Page", "Performance", "Inspector"):
            await cdp.call(f"{dom}.enable")
        # The Network domain says a socket closed, not why. The page itself sees the close code and
        # reason (1006: the connection died without a close frame; 1000/1001: the other side said
        # goodbye), so a hook on WebSocket reports them through a binding, on every document load.
        await cdp.call("Runtime.addBinding", name="__nighttest")
        await cdp.call("Page.addScriptToEvaluateOnNewDocument", expression=WS_HOOK)
        # DevTools reports only WebSockets opened after Network.enable: reload so every socket is seen.
        await cdp.call("Page.reload")
        self.rec(name, "reloaded")
        asyncio.create_task(self.metrics_loop(cdp, page))

    def on_text(self, name: str, kind: str, ch: str, data: str, ts: float) -> None:
        try:
            m = json.loads(data)
        except ValueError:
            self.rec(name, "text", ch=ch, bytes=len(data))
            return
        if ch.endswith("/status"):
            self.rec(name, "status", **fields(m, skip=("cards", "comments")))
            return
        t = m.get("type")
        if t == "state":
            r = {"topic": m.get("topic"), "bytes": len(data)}
            if m.get("topic") == "exposure":
                r["data"] = m.get("data")
            self.rec(name, "state", ch=ch, **r)
        elif t in ("hello", "event"):
            self.rec(name, t, ch=ch, **fields(m, skip=("type",)))
        elif t == "ack":
            self.rec(name, "ack", ch=ch, id=m.get("id"), ok=m.get("ok"), error=m.get("error"))
        else:
            self.rec(name, "text", ch=ch, type=t, bytes=len(data))

    async def metrics_loop(self, cdp: CDP, page: dict) -> None:
        name, kind = page["name"], page["kind"]
        probe = PROBES[kind]
        while not cdp.closed.is_set() and not self.stop.is_set():
            try:
                m = await cdp.call("Performance.getMetrics")
                metrics = {x["name"]: x["value"] for x in m["metrics"] if x["name"] in METRICS}
                r = await cdp.call("Runtime.evaluate", expression=probe, returnByValue=True)
                self.rec(name, "metrics", **metrics, **(r.get("result", {}).get("value") or {}))
            except Exception as e:
                if not cdp.closed.is_set():
                    self.rec(name, "metrics_failed", error=repr(e))
            try:
                await asyncio.wait_for(cdp.closed.wait(), self.args.metrics_every)
            except asyncio.TimeoutError:
                pass

    # run ------------------------------------------------------------------------------------------------------------

    async def run(self) -> None:
        async with ClientSession() as session:
            self.session = session
            b, v = await self.browser()
            await b.close()
            self.rec("-", "start", chrome=v.get("Browser"), argv=sys.argv[1:])
            print(f"recording to {self.args.out}; chrome {v.get('Browser')}", flush=True)
            await self.wait_for_login()
            if self.stop.is_set():
                return
            ids = await self.ensure_windows()
            self.rec("-", "pages", pages=self.pages, targets=ids)
            print("pages: " + ", ".join(f"{p['name']} ({p['kind']})" for p in self.pages), flush=True)
            tasks = [asyncio.create_task(self.watch_page(p)) for p in self.pages]
            tasks.append(asyncio.create_task(self.progress()))
            await self.stop.wait()
            self.rec("-", "stop", records=self.n_records)
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        self.out.close()

    async def progress(self) -> None:
        """A line on stderr now and then, so a glance at the terminal says whether data flows."""
        while not self.stop.is_set():
            await asyncio.sleep(self.args.progress_every)
            frames = {p["name"]: self.counts[(p["name"], "frame")] for p in self.pages}
            states = {p["name"]: self.counts[(p["name"], "state")] for p in self.pages if p["kind"] == "instrument"}
            errs = sum(n for (_, ev), n in self.counts.items() if ev in ("console", "exception", "crash", "ws_error"))
            print(f"{dt.datetime.now():%H:%M:%S} records={self.n_records} frames={frames} "
                  f"states={states} errors={errs}", flush=True)


RESERVED = ("t", "iso", "page", "ev", "ch")

# Runs before any page script (main document and same-origin iframes alike): wraps WebSocket so
# each close reports its code, reason and cleanliness. `ch` matches the Network domain's channel.
WS_HOOK = """(() => {
  const Native = WebSocket;
  const send = (o) => { try { __nighttest(JSON.stringify(o)); } catch (e) {} };
  const Hooked = function (url, protocols) {
    const ws = protocols === undefined ? new Native(url) : new Native(url, protocols);
    const ch = new URL(url, location.href).pathname;
    ws.addEventListener("close", (e) => send({ ev: "ws_closed", ch, code: e.code, reason: e.reason, clean: e.wasClean }));
    ws.addEventListener("error", () => send({ ev: "ws_onerror", ch }));
    return ws;
  };
  Hooked.prototype = Native.prototype;
  for (const k of ["CONNECTING", "OPEN", "CLOSING", "CLOSED"]) Hooked[k] = Native[k];
  window.WebSocket = Hooked;
})();"""


def fields(m: dict, skip=()) -> dict:
    """A message's keys as record fields, renamed where they would shadow the record's own."""
    return {(f"m_{k}" if k in RESERVED else k): v for k, v in m.items() if k not in skip}


METRICS = {"JSHeapUsedSize", "JSHeapTotalSize", "Nodes", "Documents", "LayoutCount", "RecalcStyleCount",
           "TaskDuration", "ScriptDuration", "Frames"}

# Evaluated in the page every metrics interval; must return a plain object.
PROBES = {
    "instrument": """(() => {
        const o = { visibility: document.visibilityState,
                    conn: document.getElementById("conn-state")?.textContent ?? null };
        const f = document.querySelector("iframe.embed-frame")?.contentWindow;  // the SPA's Quick Look tab
        if (f) { const d = f.document;
                 o.embed = { state: d.getElementById("strip-state")?.textContent ?? null,
                             rate: d.getElementById("strip-rate")?.textContent ?? null,
                             status: f.__viewer?.status?.() ?? null }; }
        return o;
    })()""",
    "quicklook": """({
        visibility: document.visibilityState,
        state: document.getElementById("strip-state")?.textContent ?? null,
        rate: document.getElementById("strip-rate")?.textContent ?? null,
        status: globalThis.__viewer?.status?.() ?? null,
        workers: document.getElementById("workers")?.value ?? null,
        tier: document.getElementById("tier")?.value ?? null,
    })""",
}
PROBES["guider"] = PROBES["quicklook"]


def chz1_header(b64: str) -> dict:
    """The JSON header at the front of a chz1 message, without decoding the pixel blobs.

    ``wire_bytes`` is the size DevTools handed over and ``expect_bytes`` what the
    header says the message is; a difference means DevTools truncated the frame
    (the header itself is complete either way, or ``parse`` says why not).
    """
    n = len(b64)
    wire = n * 3 // 4 - (2 if b64.endswith("==") else 1 if b64.endswith("=") else 0)
    head = base64.b64decode(b64[: min(n, 64) // 4 * 4])
    if head[:4] != MAGIC:
        return {"wire_bytes": wire, "parse": "not a chz1 message"}
    (blen,) = struct.unpack_from("<I", head, 4)
    need = 8 + blen
    if need > len(head):
        head = base64.b64decode(b64[: min(n, (need + 2) // 3 * 4 + 4) // 4 * 4])
    if len(head) < need:
        return {"wire_bytes": wire, "parse": f"header truncated ({len(head)} < {need})"}
    h = json.loads(head[8:need])
    pad = (-blen) % 4
    out = {
        "wire_bytes": wire,
        "expect_bytes": need + pad + h.get("comp_bytes", 0),
        "seq": h.get("seq"), "name": h.get("name"), "w": h.get("w"), "h": h.get("h"),
        "bin": h.get("bin", 1), "qstep": h.get("qstep"), "comp_bytes": h.get("comp_bytes"),
        "raw_bytes": h.get("raw_bytes"), "server": h.get("server"), "crop": h.get("crop"),
    }
    if "guider" in h:  # gcamweb: the camera's own frame counter and clock
        g = h["guider"]
        out["src_seq"] = g.get("seq")
        out["src_ts_ns"] = int(g["ts_ns"]) if g.get("ts_ns") is not None else None
    if "fits" in h:  # imageweb: which science frame
        out["fits_id"] = h["fits"].get("id")
        out["fits_path"] = h["fits"].get("path")
    return out


# -- offline report ------------------------------------------------------------------------------------------------


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


def report(path: str, as_json: bool) -> None:
    recs = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                recs.append(json.loads(line))
    if not recs:
        print("empty file")
        return
    start, end = recs[0]["t"], recs[-1]["t"]
    hours = (end - start) / 3600
    pages = sorted({r["page"] for r in recs if r["page"] != "-"})
    out: dict = {"file": path, "start": recs[0]["iso"], "end": recs[-1]["iso"], "hours": round(hours, 2),
                 "records": len(recs), "pages": {}}
    for name in pages:
        rs = [r for r in recs if r["page"] == name]
        by = defaultdict(list)
        for r in rs:
            by[r["ev"]].append(r)
        p: dict = {"records": len(rs)}
        p["ws_opens"] = dict(Counter(r["ch"] for r in by["ws_open"]))
        p["ws_closes"] = dict(Counter(r["ch"] for r in by["ws_close"]))
        p["attach"] = {"attached": len(by["attached"]), "detached": len(by["detached"]),
                       "target_missing": len(by["target_missing"]), "crashes": len(by["crash"]),
                       "navigations": len(by["navigated"]), "loads": len(by["loaded"])}
        p["errors"] = {"console": len(by["console"]), "exceptions": len(by["exception"]), "log": len(by["log"]),
                       "ws_errors": len(by["ws_error"])}
        p["error_samples"] = Counter(r.get("text", "")[:120] for r in by["console"] + by["exception"] + by["log"]).most_common(5)
        frames = by["frame"]
        if frames:
            ts = [r["t"] for r in frames]
            gaps = [b - a for a, b in zip(ts, ts[1:])]
            f: dict = {"n": len(frames), "first": frames[0]["iso"], "last": frames[-1]["iso"],
                       "wire_MB": round(sum(r.get("wire_bytes", 0) for r in frames) / 1e6, 2),
                       "truncated": sum(1 for r in frames if r.get("expect_bytes") and r["wire_bytes"] != r["expect_bytes"]),
                       "unparsed": sum(1 for r in frames if "parse" in r),
                       "fps": round((len(frames) - 1) / (ts[-1] - ts[0]), 3) if len(frames) > 1 and ts[-1] > ts[0] else None,
                       "interval_s": {"p50": round(pct(gaps, 50), 2), "p90": round(pct(gaps, 90), 2),
                                      "max": round(max(gaps), 2)} if gaps else None,
                       "sizes": dict(Counter(f'{r.get("w")}x{r.get("h")} bin{r.get("bin")}' for r in frames))}
            # Per-connection seq: consecutive unless DevTools lost an event.
            conn_gaps = 0
            prev = None
            for r in frames:
                s = r.get("seq")
                if prev is not None and s is not None and s != prev + 1 and s != 1:
                    conn_gaps += 1
                prev = s
            f["conn_seq_breaks"] = conn_gaps
            src = [r for r in frames if r.get("src_seq") is not None]
            if src:
                # gcam numbers every frame; gcamweb sends every Nth (`every`). A step larger than the
                # `every` in force at the time is a frame the browser never got.
                every_at = sorted((r["t"], r.get("every", 1)) for r in by["status"] if r.get("every"))
                dropped = 0
                steps = Counter()
                prev = None
                for r in src:
                    ev = 1
                    for t, e in every_at:
                        if t <= r["t"]:
                            ev = e
                    if prev is not None:
                        step = r["src_seq"] - prev
                        steps[step] += 1
                        if step > ev:
                            dropped += max(1, round(step / max(ev, 1)) - 1)
                    prev = r["src_seq"]
                f["src_seq"] = {"first": src[0]["src_seq"], "last": src[-1]["src_seq"], "steps": dict(steps.most_common(6)),
                                "dropped_est": dropped}
                lags = [r["t"] - r["src_ts_ns"] / 1e9 for r in src if r.get("src_ts_ns")]
                if lags:
                    f["lag_s"] = {"note": "arrival minus camera timestamp; includes the clock offset between the two hosts",
                                  "p50": round(pct(lags, 50), 3), "p90": round(pct(lags, 90), 3),
                                  "max": round(max(lags), 3), "min": round(min(lags), 3)}
            fits = [r["fits_id"] for r in frames if r.get("fits_id") is not None]
            if fits:
                f["fits_ids"] = {"n": len(fits), "unique": len(set(fits)), "first": fits[0], "last": fits[-1]}
            srv = [r["server"] for r in frames if r.get("server")]
            if srv:
                f["server_ms"] = {k: round(pct([s.get(k, 0) for s in srv], 50), 1)
                                  for k in ("read_ms", "filter_ms", "zstd_ms", "stats_ms")}
            p["frames"] = f
        acks = [r["decode_ms"] for r in by["ack_sent"] if r.get("decode_ms") is not None]
        if acks:
            p["decode_ms"] = {"n": len(acks), "p50": round(pct(acks, 50), 1), "p90": round(pct(acks, 90), 1),
                              "max": round(max(acks), 1)}
        st = by["status"]
        if st:
            s: dict = {"n": len(st)}
            for k in ("gcam", "clients", "every", "roi", "state", "connected"):
                vals = [r.get(k) for r in st if k in r]
                if vals:
                    s[k] = dict(Counter(str(v) for v in vals).most_common(6))
            ages = [r["age_s"] for r in st if isinstance(r.get("age_s"), (int, float))]
            if ages:
                s["age_s"] = {"p50": round(pct(ages, 50), 1), "p90": round(pct(ages, 90), 1), "max": round(max(ages), 1)}
            last = [r["last_seq"] for r in st if isinstance(r.get("last_seq"), int)]
            if last:
                s["last_seq"] = {"first": last[0], "last": last[-1]}
            p["status"] = s
        states = by["state"]
        if states:
            p["state_msgs"] = {"n": len(states), "per_min": round(len(states) / max(hours * 60, 1e-9), 1),
                               "topics": dict(Counter(r["topic"] for r in states).most_common())}
            exp = [r["data"] for r in states if r.get("topic") == "exposure" and isinstance(r.get("data"), dict)]
            if exp:
                ids = [e.get("id") for e in exp if e.get("id") is not None]
                starts = sum(1 for a, b in zip(exp, exp[1:]) if not a.get("running") and b.get("running"))
                p["exposures"] = {"snapshots": len(exp), "unique_ids": len(set(ids)), "first_id": ids[0] if ids else None,
                                  "last_id": ids[-1] if ids else None, "running_edges": starts,
                                  "exptime": dict(Counter(str(e.get("exptime")) for e in exp).most_common(3))}
        if by["hello"]:
            p["hello"] = {"n": len(by["hello"]), "app": by["hello"][-1].get("app"), "version": by["hello"][-1].get("version")}
        if by["event"]:
            p["events"] = dict(Counter(r.get("topic") or r.get("name") for r in by["event"]).most_common(8))
        if by["cmd"] or by["ack"]:
            p["cmds"] = {"sent": len(by["cmd"]), "acked": len(by["ack"]),
                         "failed": sum(1 for r in by["ack"] if r.get("ok") is False)}
        met = by["metrics"]
        if met:
            heap = [r["JSHeapUsedSize"] / 1e6 for r in met if r.get("JSHeapUsedSize")]
            m: dict = {"samples": len(met), "hidden_samples": sum(1 for r in met if r.get("visibility") != "visible")}
            if heap:
                m["heap_MB"] = {"first": round(heap[0], 1), "max": round(max(heap), 1), "last": round(heap[-1], 1)}
            nodes = [r["Nodes"] for r in met if r.get("Nodes")]
            if nodes:
                m["nodes"] = {"first": nodes[0], "max": max(nodes), "last": nodes[-1]}
            for k in ("state", "conn"):
                vals = [r.get(k) or (r.get("embed") or {}).get(k) for r in met]
                vals = [v for v in vals if v]
                if vals:
                    m[k] = dict(Counter(vals).most_common(5))
            p["metrics"] = m
        out["pages"][name] = p
    if as_json:
        json.dump(out, sys.stdout, indent=2, default=str)
        print()
        return
    print(f"{path}: {out['start']} -> {out['end']} ({out['hours']} h, {out['records']} records)")
    for name, p in out["pages"].items():
        print(f"\n== {name}")
        for k, v in p.items():
            if k == "records":
                continue
            if isinstance(v, dict):
                print(f"  {k}:")
                for kk, vv in v.items():
                    print(f"    {kk}: {vv}")
            else:
                print(f"  {k}: {v}")


# -- main ----------------------------------------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record", help="attach to the debug Chrome and record until interrupted")
    r.add_argument("--port", type=int, default=9222, help="Chrome remote debugging port")
    r.add_argument("--host", default="sbs.chimera.observer")
    r.add_argument("--instrument", default="pfs")
    r.add_argument("--guider", default="pfs-sv", help="guider page name (its operational name)")
    r.add_argument("--out", default=f"nighttest-{dt.datetime.now():%Y%m%d-%H%M}.jsonl")
    r.add_argument("--metrics-every", type=float, default=30, help="seconds between page metric samples")
    r.add_argument("--progress-every", type=float, default=60, help="seconds between terminal progress lines")
    r.add_argument("--no-tile", dest="tile", action="store_false", help="leave the windows where they are")
    p = sub.add_parser("report", help="summarise a recording")
    p.add_argument("file")
    p.add_argument("--json", action="store_true", help="machine-readable summary")
    args = ap.parse_args()
    if args.cmd == "report":
        report(args.file, args.json)
        return
    rec = Recorder(args)
    loop = asyncio.new_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, rec.stop.set)
    loop.run_until_complete(rec.run())
    print(f"stopped; {rec.n_records} records in {args.out}", flush=True)


if __name__ == "__main__":
    main()
