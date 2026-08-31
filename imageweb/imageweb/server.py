"""Instrument images -> browser gateway.

One process serves every FITS-producing instrument on the Mac, each under
``<prefix>/<name>/`` so the gateway sits behind a reverse proxy or a
Cloudflare Tunnel that forwards paths unchanged (ingress
``path: ^/image(/.*)?$``)::

    /image/                  the list of instruments (+ instruments.json)
    /image/pfs/              the quick-look viewer for one instrument
    /image/pfs/ws            its CHZ1 frame stream
    /image/pfs/status        its status channel (JSON, once a second)
    /image/pkg/{chz1,core,viewer}/         the JS packages, from the astro-ph monorepo

Per instrument: a read-only client of the control WS (``exposure_complete``
-> local ``fits_path``), a lazily-decoded latest-frame source, and per-client
CHZ1 encode pipelines. Design: docs/plans/image-viewer-plan.md.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from aiohttp import web

from .control import ControlClient
from .source import InstrumentSource
from chz1.stream import Settings, isolation_headers, ws_handler

log = logging.getLogger("imageweb")

STATIC = Path(__file__).parent / "static"
PAGES = ("index.html", "app.js", "header-panel.js", "viewer.css", "favicon.svg")
# imageweb/imageweb/server.py -> workspace/ — the astro-ph monorepo
# checkout, the same convention gcamweb uses.
DEFAULT_ASTRO_PH = Path(__file__).resolve().parents[3] / "astro-ph-labs" / "astro-ph"

# Control-WS ports per ws-migration-plan.md (50001 + PROJECT_ID*100 + 2),
# FITS-producing instruments only — ADC/DCU have nothing to show here.
KNOWN_PORTS = {
    "ldss3": 50603, "mike": 50803, "swope": 51203, "mage": 51503,
    "pfs": 51603, "ifum": 51803, "m2fs": 51803, "henrietta": 52803,
}
INSTRUMENT_SPEC = re.compile(r"^(?P<name>[a-z0-9_]+)(?:@(?P<host>[^:]+)(?::(?P<port>\d+))?)?$")


def parse_instrument(spec: str, default_host: str) -> tuple[str, str, int]:
    """``NAME[@HOST[:PORT]]`` -> (name, host, control_ws_port)."""
    m = INSTRUMENT_SPEC.match(spec.strip().lower())
    if not m:
        raise argparse.ArgumentTypeError(
            f"bad instrument {spec!r}; expected NAME[@HOST[:PORT]], e.g. pfs or pfs@localhost:51603")
    name = m["name"]
    port = int(m["port"]) if m["port"] else KNOWN_PORTS.get(name)
    if not port:
        raise argparse.ArgumentTypeError(
            f"unknown instrument {name!r}: give the control-WS port explicitly ({spec}@HOST:PORT)")
    return name, m["host"] or default_host, port


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instrument", action="append", metavar="NAME[@HOST[:PORT]]",
                    help="an instrument to serve (control-WS port from the migration plan's table "
                         "when omitted); repeat for several. Default: pfs")
    ap.add_argument("--control-host", default="127.0.0.1",
                    help="control-WS host for instruments given without @HOST (default 127.0.0.1 — "
                         "the gateway runs on the instrument Mac; fits_path is local)")
    ap.add_argument("--prefix", default="/image", help="path prefix (default /image)")
    ap.add_argument("--host", default="127.0.0.1", help="listen address")
    ap.add_argument("--port", type=int, default=8766, help="listen port")
    ap.add_argument("--astro-ph", dest="astro_ph", type=Path, default=DEFAULT_ASTRO_PH,
                    help="the astro-ph monorepo checkout (chz1, core, viewer)")
    enc = ap.add_argument_group("encoder (chz1)")
    enc.add_argument("--bands", type=int, default=8)
    enc.add_argument("--level", type=int, default=1, help="zstd level")
    enc.add_argument("--encoders", type=int, default=4, help="encode thread pool size")
    enc.add_argument("--inflight", type=int, default=2, help="unacked frames per client")
    enc.add_argument("--bin", type=int, default=4, help="preview bin factor (default 4 — full science frames)")
    enc.add_argument("--q", type=float, default=0.5, help="preview quantization, fraction of noise sigma")
    enc.add_argument("--no-dither", dest="dither", action="store_false")
    args = ap.parse_args(argv)
    try:
        args.instruments = [parse_instrument(s, args.control_host) for s in (args.instrument or ["pfs"])]
    except argparse.ArgumentTypeError as e:
        ap.error(str(e))
    if len({i[0] for i in args.instruments}) != len(args.instruments):
        ap.error("an instrument is given twice")
    args.prefix = "/" + args.prefix.strip("/")
    return args


def instrument_app(name: str, host: str, port: int, args: argparse.Namespace) -> web.Application:
    """One instrument: its own control client, source and default settings; the
    encode pool is shared per instrument, pipelines are per client connection."""
    app = web.Application()
    settings = Settings(bands=args.bands, level=args.level, encoders=args.encoders,
                        inflight=args.inflight, bin=args.bin, q=args.q, dither=args.dither)
    source = InstrumentSource(name)
    control = ControlClient(name, host, port, source.announce)
    app["settings"], app["source"], app["control"] = settings, source, control
    status_clients: set[web.WebSocketResponse] = set()

    def full_status() -> dict:
        return source.status() | control.status()

    app["full_status"] = full_status

    async def status_ws(request):
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        status_clients.add(ws)
        try:
            await ws.send_json(full_status())
            async for _ in ws:  # the client sends nothing
                pass
        finally:
            status_clients.discard(ws)
        return ws

    async def status_broadcast():
        while True:
            await asyncio.sleep(1.0)
            for ws in list(status_clients):
                try:
                    await ws.send_json(full_status())
                except Exception:
                    status_clients.discard(ws)

    async def on_start(app):
        control.start()
        app["pool"] = ThreadPoolExecutor(args.encoders, thread_name_prefix=f"enc-{name}")
        app["status_task"] = asyncio.create_task(status_broadcast())

    async def on_stop(app):
        app["status_task"].cancel()
        await control.stop()
        app["pool"].shutdown(wait=False)

    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/status", status_ws)
    app.router.add_get("/", page("index.html"))
    for name_ in PAGES:
        app.router.add_get(f"/{name_}", page(name_))
    app.on_startup.append(on_start)
    app.on_cleanup.append(on_stop)
    return app


def page(name: str):
    async def handler(request):
        return web.FileResponse(STATIC / name)
    return handler


def redirect(location: str):
    async def handler(request):
        raise web.HTTPFound(location)
    return handler


def build_app(args: argparse.Namespace) -> web.Application:
    prefix = args.prefix
    root = web.Application(middlewares=[isolation_headers])
    subs = {name: instrument_app(name, host, port, args) for name, host, port in args.instruments}
    statuses = lambda: [sub["full_status"]() | {"host": sub["control"].host, "port": sub["control"].port}
                        for sub in subs.values()]

    async def landing(request):
        rows = "".join(
            f'<li><a href="{s["name"]}/">{s["name"]}</a> <span class="dim">'
            f'control at {html.escape(s["host"])}:{s["port"]} · {html.escape(str(s["control"]))}'
            + (f' · image #{s["last_seq"]}, {s["age_s"]} s ago' if s["last_seq"] is not None else " · no image yet")
            + "</span></li>"
            for s in statuses())
        return web.Response(content_type="text/html", text=f"""<!doctype html><meta charset="utf-8">
<title>instrument images</title>
<style>body{{margin:0;padding:1.2rem;background:#0a0a0c;color:#d8d8e0;font:14px/1.6 ui-monospace,Menlo,monospace}}
h1{{font-size:1.1rem;color:#6ee7b7;margin:0 0 .6rem}} a{{color:#6ee7b7}} .dim{{color:#8a8a98;font-size:12px}}
li{{margin:.3rem 0}} ul{{padding-left:1.2rem}}</style>
<h1>instrument quick look</h1><ul>{rows}</ul>""")

    async def instruments_json(request):
        return web.json_response({"prefix": prefix, "instruments": statuses()})

    root.router.add_get("/", redirect(f"{prefix}/"))
    root.router.add_get(prefix, redirect(f"{prefix}/"))
    root.router.add_get(f"{prefix}/", landing)
    root.router.add_get(f"{prefix}/instruments.json", instruments_json)
    # Package roots from the astro-ph monorepo, so the import map reaches each
    # package's own layout: chz1 ships plain JS under ts/src; core and viewer
    # ship built JS under dist (npm run build in the checkout) plus core's
    # overlay.css under src.
    root.router.add_static(f"{prefix}/pkg/chz1/", args.astro_ph / "packages" / "chz1")
    root.router.add_static(f"{prefix}/pkg/core/", args.astro_ph / "packages" / "core")
    root.router.add_static(f"{prefix}/pkg/viewer/", args.astro_ph / "packages" / "viewer")
    for name, sub in subs.items():
        root.router.add_get(f"{prefix}/{name}", redirect(f"{prefix}/{name}/"))  # relative URLs need the slash
        root.add_subapp(f"{prefix}/{name}/", sub)
    return root


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    args = parse_args()
    for d in ("packages/chz1/ts/src", "packages/core/dist", "packages/viewer/dist"):
        if not (args.astro_ph / d).is_dir():
            raise SystemExit(
                f"{args.astro_ph / d} not found -- the JS packages come from the astro-ph "
                "monorepo checkout (--astro-ph); run `npm install && npm run build` there first")
    for name, host, port in args.instruments:
        log.info("%s: control at ws://%s:%d -> http://%s:%d%s/%s/",
                 name, host, port, args.host, args.port, args.prefix, name)
    web.run_app(build_app(args), host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
