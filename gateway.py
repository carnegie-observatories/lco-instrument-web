#!/usr/bin/env python3
"""lco-gateway — one process serving one deployment.

Replaces the three processes a deployment used to need (`server.py` for
static files, `imageweb` for quick look, the gcam bridge for guiders)
with a single front door, configured entirely by
`deployments/<name>.yml`:

    /                     SPA static files
    /config.json          the deployment, as the SPA consumes it
    /pkg/{chz1,core,viewer}/  the viewer packages, once, for every page below
    /image/<app>/         quick-look viewer   — imageweb, in-process
    /guider/<name>/       web guider page     — served here; its ws/status/every/roi
                                                proxied to gcamweb's /guider/<gcam_name>/
    /<app>/ws             instrument control  — proxied, --proxy-ws only
    /healthz              per-target liveness, over each target's own WebSocket

Two kinds of mounting, and the difference is deliberate. imageweb is a
member of this repo's uv workspace, so its instrument apps are built
in-process — one control-WS client per instrument, sharing this event
loop. The gcam bridge is not: it lives in carnegie-observatories/zwo,
ships on its own release cadence, and vendoring another repo's service
to avoid a loopback hop would be the wrong trade. It stays a separate
process and this gateway reverse-proxies to it — the live channels only.
The guider *page* is this repo's (viewer/guider.html, one assembly with
quick look), served here under the guider's operational name: `pfs-sv`
is what the camera is for, `gcam13` is gcamweb's addressing (rotator
port 1, camera 3 — and cameras 3 and up are not the telescope's guiders
but the instrument's own), so only the proxy side ever sees the latter.
See docs/plans/guider-viewer-plan.md.

The instrument WebSocket is proxied only under `--proxy-ws`. Without
it, `/<app>/ws` is left to cloudflared exactly as before — see
docs/plans/deployment-config-plan.md § The gateway service for why
that hop is opt-in.

    uv run python gateway.py                           # deployments/*.yml, if there is one
    uv run python gateway.py --deployment deployments/sbs.yml --port 8080
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from aiohttp import ClientError, ClientSession, ClientTimeout, WSMsgType, web

import deployment_config

REPO = Path(__file__).resolve().parent
log = logging.getLogger("gateway")

# Hop-by-hop headers must not be forwarded by a proxy (RFC 9110 §7.6.1);
# Host is recomputed by the client session for the upstream.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
}


# --------------------------------------------------------------------------
# headers
# --------------------------------------------------------------------------

@web.middleware
async def no_store(request: web.Request, handler):
    """Forbid caching, and turn on cross-origin isolation behind the tunnel.

    Both rules are inherited from server.py, which learned them the hard
    way: Cloudflare's edge caches .js/.css by default while leaving .html
    uncached, so a deploy leaves browsers importing stale modules. And
    the Quick Look iframe only gets SharedArrayBuffer for its decode pool
    when the *top-level* document sends COOP/COEP too — safe behind the
    tunnel, where everything is one origin, and harmful on plain http,
    where a COEP parent would refuse the cross-origin iframe outright.
    Requests forwarded by cloudflared carry Cf-Ray.
    """
    response = await handler(request)
    response.headers["Cache-Control"] = "no-store"
    if "Cf-Ray" in request.headers:
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Embedder-Policy"] = "require-corp"
    return response


# --------------------------------------------------------------------------
# reverse proxy (HTTP + WebSocket)
# --------------------------------------------------------------------------

async def proxy_ws(request: web.Request, target: str) -> web.WebSocketResponse:
    """Bridge a client WebSocket to an upstream one, both directions."""
    # Only advertise subprotocols the client actually offered: passing a
    # [""] from an absent header makes the handshake negotiate an empty
    # protocol, which clients reject.
    offered = [p for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",") if p.strip()]
    # heartbeat: a ping to the browser every 20 s, answered by a pong. The instrument and
    # status channels flow one way -- the browser subscribes once and never speaks again --
    # and the night test of 2026-09-17 saw exactly those sockets cut (1006) twenty minutes
    # after they were opened, every time, while every socket with client-to-server traffic
    # lived. The pongs are that traffic.
    client = web.WebSocketResponse(protocols=[p.strip() for p in offered], heartbeat=20)
    await client.prepare(request)
    session: ClientSession = request.app["session"]

    try:
        async with session.ws_connect(target, timeout=ClientTimeout(total=10)) as upstream:
            async def pump(src, dst, what):
                async for msg in src:
                    if msg.type == WSMsgType.TEXT:
                        await dst.send_str(msg.data)
                    elif msg.type == WSMsgType.BINARY:
                        await dst.send_bytes(msg.data)
                    elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
                        break
                    elif msg.type == WSMsgType.ERROR:
                        log.warning("proxy %s: %s", what, src.exception())
                        break

            # Whichever side closes first ends the pair; the other pump is
            # cancelled rather than left waiting on a dead socket, and then
            # awaited so cancellation has actually landed before the
            # finally-block closes the client. (Hygiene, not a fix for an
            # observed bug: killing the upstream mid-connection delivers the
            # close to the browser with or without the await.)
            done, pending = await asyncio.wait(
                [asyncio.create_task(pump(client, upstream, "client->upstream")),
                 asyncio.create_task(pump(upstream, client, "upstream->client"))],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
    except (OSError, asyncio.TimeoutError) as e:
        log.warning("proxy: upstream %s unreachable: %s", target, e)
    finally:
        if not client.closed:
            await client.close()
    return client


async def proxy_http(request: web.Request, target: str) -> web.StreamResponse:
    session: ClientSession = request.app["session"]
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
    try:
        async with session.request(
            request.method, target, headers=headers,
            data=request.content if request.body_exists else None,
            allow_redirects=False,
        ) as upstream:
            response = web.StreamResponse(status=upstream.status, headers={
                k: v for k, v in upstream.headers.items() if k.lower() not in HOP_BY_HOP
            })
            await response.prepare(request)
            async for chunk in upstream.content.iter_chunked(64 * 1024):
                await response.write(chunk)
            await response.write_eof()
            return response
    except (OSError, asyncio.TimeoutError) as e:
        log.warning("proxy: %s unreachable: %s", target, e)
        return web.Response(status=502, text=f"upstream unreachable: {target}\n{e}\n")


def make_proxy(target: str):
    """Proxy this route to one fixed upstream URL, keeping the query string.

    The route's own path is *not* forwarded: /guider/pfs-sv/ws lands on
    gcamweb's /guider/gcam13/ws, so the rename between the operational
    name and gcamweb's happens here and nowhere else. HTTP and WebSocket
    alike (both guider channels, ws and status, are WebSockets; the
    guiders.json fetch is HTTP).
    """
    async def handler(request: web.Request) -> web.StreamResponse:
        t = f"{target}?{request.query_string}" if request.query_string else target
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await proxy_ws(request, t.replace("http://", "ws://", 1))
        return await proxy_http(request, t)

    return handler


def make_ws_proxy(host: str, port: int):
    """Proxy /<app>/ws to an instrument's WS server, which listens at `/`.

    The path must be *rewritten*, not forwarded: the Cocoa apps' WSServer
    serves the root, so passing /pfs/ws through gets a 404 on the
    handshake. cloudflared has the same rewrite in its ingress rule,
    which is why this only shows up once the gateway takes the hop over.
    """
    target = f"ws://{host}:{port}/"

    async def handler(request: web.Request) -> web.StreamResponse:
        return await proxy_ws(request, target)

    return handler


# --------------------------------------------------------------------------
# health
# --------------------------------------------------------------------------

# The rule: the gateway never opens a connection to a TCP text command
# interface -- not gcam's command port (52200+gnum, single-client: a
# probe there takes the one slot from operations tooling, or reports the
# guider down because that tooling holds it), not an instrument's. Every
# liveness question is asked over the WebSocket interface the browser
# itself uses, and answered by the first frame that interface sends
# unprompted. There is no raw-socket code in this file; keep it that way.

async def ws_first_frame(session: ClientSession, url: str, timeout: float = 3.0) -> tuple[bool, str | dict]:
    """Open a WebSocket, take its first text frame as JSON, close.

    (True, frame) when the server spoke; (False, why) when it did not.
    A socket that accepts but never sends is reported down: the
    question is whether the app answers, not whether a port is open.
    """
    async def go():
        async with session.ws_connect(url, timeout=ClientTimeout(total=timeout)) as ws:
            msg = await ws.receive()
            if msg.type != WSMsgType.TEXT:
                return False, f"first frame was {msg.type.name}, not text"
            return True, json.loads(msg.data)
    try:
        return await asyncio.wait_for(go(), timeout)
    except asyncio.TimeoutError:
        return False, f"no frame within {timeout:g} s"
    except (OSError, ClientError, ValueError) as e:
        return False, f"{type(e).__name__}: {e}"


def health_handler(config: dict, guider_upstream: tuple[str, int] | None):
    """One check per target. An instrument proves itself with the `hello`
    its WS server sends on connect (app, version); a guider with the
    first message of gcamweb's status channel, which carries gcam's
    state as seen from the image port gcamweb is designed to hold. The
    guider check proves the bridge too, so there is no separate one.
    gcamweb's status socket counts the probe as a status listener only,
    never as a frame viewer: gcam sees nothing."""

    targets = [("instrument", i["app"], f"ws://{i['host']}:{i['port']}/") for i in config["instruments"]]
    if guider_upstream:
        bridge = "ws://%s:%d" % guider_upstream
        targets += [("guider", g["name"], f"{bridge}/guider/{g['gcam_name']}/status") for g in config["guiders"]]

    def detail(kind: str, frame: dict) -> str:
        if kind == "instrument":
            return " ".join(str(frame.get(k)) for k in ("app", "version") if frame.get(k)) or frame.get("type", "?")
        return f"gcam {frame.get('gcam', '?')}"

    async def handler(request: web.Request) -> web.Response:
        results = await asyncio.gather(*(ws_first_frame(request.app["session"], url) for _, _, url in targets))
        checks = [
            {"kind": kind, "name": name, "target": url, "up": up,
             "detail": detail(kind, got) if up else got}
            for (kind, name, url), (up, got) in zip(targets, results)
        ]
        ok = all(c["up"] for c in checks)
        return web.json_response(
            {"deployment": config["name"], "ok": ok, "checks": checks},
            status=200 if ok else 503,
        )
    return handler


# --------------------------------------------------------------------------
# app
# --------------------------------------------------------------------------

def mount_packages(app: web.Application, astro_ph: Path) -> bool:
    """/pkg/{chz1,core,viewer}/ from the astro-ph checkout: one mount for
    every viewer page (quick look and the guiders both address ../../pkg/).
    Missing checkout -> no packages, and both kinds of page say so."""
    if not all((astro_ph / "packages" / p).is_dir() for p in ("chz1", "core", "viewer")):
        log.error("viewer packages unavailable: no astro-ph checkout at %s", astro_ph)
        return False
    for pkg in ("chz1", "core", "viewer"):
        app.router.add_static(f"/pkg/{pkg}/", astro_ph / "packages" / pkg)
    return True


def mount_guiders(app: web.Application, config: dict, bridge: tuple[str, int]) -> None:
    """The guider pages, and the proxies behind them.

    Per guider, under its operational name: the page and the assembly's
    files (viewer/, served static), and four routes proxied to gcamweb
    under gcamweb's name -- ws and status (WebSockets), every and roi
    (GET/POST settings). Above them, /guider/ and /guider/guiders.json
    are generated here from the deployment joined with gcamweb's own
    list, because the gateway knows names gcamweb does not and gcamweb
    knows state the config does not. Route order matters throughout:
    aiohttp takes the first match, and add_static is a prefix resource.
    """
    host, port = bridge
    upstream = f"http://{host}:{port}/guider"
    guiders = config["guiders"]

    async def upstream_status(session: ClientSession) -> tuple[dict, str | None]:
        try:
            async with session.get(f"{upstream}/guiders.json", timeout=ClientTimeout(total=3)) as r:
                body = await r.json()
                return {g["name"]: g for g in body.get("guiders", [])}, None
        except (OSError, ClientError, ValueError, KeyError) as e:
            return {}, f"{type(e).__name__}: {e}"

    async def rows(session: ClientSession) -> list[dict]:
        seen, err = await upstream_status(session)
        out = []
        for g in guiders:
            s = seen.get(g["gcam_name"])
            out.append({
                "name": g["name"], "title": g["title"], "path": g["path"],
                "gcam_name": g["gcam_name"], "image_port": g["image_port"],
                "bridge": "unreachable" if err else ("up" if s else "up, guider not served"),
                "gcam": s.get("gcam") if s else None,
                "last_seq": s.get("last_seq") if s else None,
                "age_s": s.get("age_s") if s else None,
                "clients": s.get("clients") if s else None,
            })
        return out

    async def index(request: web.Request) -> web.Response:
        items = []
        for r in await rows(request.app["session"]):
            state = (f'gcam {r["gcam"]}' + (f', frame #{r["last_seq"]}, {r["age_s"]} s ago' if r["last_seq"] is not None else "")
                     if r["gcam"] else f'bridge {r["bridge"]}')
            items.append(f'<li><a href="{r["name"]}/">{r["name"]}</a> — {r["title"]} '
                         f'<span class="dim">(gcamweb {r["gcam_name"]}) · {state}</span></li>')
        return web.Response(content_type="text/html", text=
            "<!doctype html><meta charset=utf-8><title>web guiders</title>"
            "<style>body{margin:0;padding:1.2rem;background:#0a0a0c;color:#d8d8e0;font:14px/1.6 ui-monospace,Menlo,monospace}"
            "h1{font-size:1.1rem;color:#6ee7b7;margin:0 0 .6rem} a{color:#6ee7b7} .dim{color:#8a8a98;font-size:12px}</style>"
            f"<h1>web guiders</h1><ul>{''.join(items)}</ul>")

    async def guiders_json(request: web.Request) -> web.Response:
        return web.json_response({"prefix": "/guider", "guiders": await rows(request.app["session"])})

    app.router.add_get("/guider", lambda r: web.HTTPFound("/guider/"))
    app.router.add_get("/guider/", index)
    app.router.add_get("/guider/guiders.json", guiders_json)

    async def guider_page(request: web.Request) -> web.StreamResponse:
        return web.FileResponse(REPO / "viewer" / "guider.html")

    for g in guiders:
        base, target = f"/guider/{g['name']}", f"{upstream}/{g['gcam_name']}"
        app.router.add_get(base, lambda r, to=f"{base}/": web.HTTPFound(to))  # relative URLs need the slash
        app.router.add_get(f"{base}/", guider_page)
        for channel in ("ws", "status"):
            app.router.add_route("*", f"{base}/{channel}", make_proxy(f"{target}/{channel}"))
        app.router.add_static(f"{base}/", REPO / "viewer")


def mount_imageweb(app: web.Application, config: dict, astro_ph: Path) -> list[str]:
    """Mount one imageweb instrument app per quick-look instrument.

    imageweb's own build_app() claims "/" for a redirect, so its root is
    unusable inside a larger app; instrument_app() is the right seam —
    it is the per-instrument unit, and the package static routes are
    three lines to add here.
    """
    from imageweb import server as iw

    quicklook = [i for i in config["instruments"] if i["quicklook"]]
    if not quicklook:
        return []

    args = iw.parse_args([])                       # encoder defaults
    args.astro_ph = astro_ph
    args.prefix = "/image"

    subs = {}
    for inst in quicklook:
        sub = iw.instrument_app(inst["app"], inst["host"], inst["port"], args)
        app.router.add_get(f"/image/{inst['app']}", iw.redirect(f"/image/{inst['app']}/"))
        app.add_subapp(f"/image/{inst['app']}/", sub)
        subs[inst["app"]] = sub

    # imageweb's build_app() also serves /image/ and /image/instruments.json;
    # mounting instrument_app() directly skips them, and the index is what
    # imageweb's README documents and the landing page links to.
    def statuses():
        return [sub["full_status"]() | {"host": sub["control"].host, "port": sub["control"].port}
                for sub in subs.values()]

    async def index(request):
        rows = "".join(
            f'<li><a href="{s["name"]}/">{s["name"]}</a> — control {s["host"]}:{s["port"]}, '
            + (f'image #{s["last_seq"]}, {s["age_s"]} s ago' if s["last_seq"] is not None else "no image yet")
            + "</li>"
            for s in statuses())
        return web.Response(content_type="text/html", text=
            f"<!doctype html><meta charset=utf-8><title>instrument quick look</title>"
            f"<h1>instrument quick look</h1><ul>{rows}</ul>")

    async def instruments_json(request):
        return web.json_response({"prefix": "/image", "instruments": statuses()})

    app.router.add_get("/image/", index)
    app.router.add_get("/image/instruments.json", instruments_json)
    return list(subs)


def build_app(config: dict, args: argparse.Namespace) -> web.Application:
    app = web.Application(middlewares=[no_store])
    body = json.dumps(config, indent=2).encode()

    async def config_json(request):
        return web.Response(body=body, content_type="application/json")

    app.router.add_get("/config.json", config_json)

    # The viewer packages first: both kinds of page below import them.
    packages = mount_packages(app, args.astro_ph)

    guider_upstream = None
    if config["guiders"] and not args.no_guiders:
        guider_upstream = (args.guider_host, args.guider_port)
        mount_guiders(app, config, guider_upstream)

    app.router.add_get("/healthz", health_handler(config, guider_upstream))

    if args.proxy_ws:
        for inst in config["instruments"]:
            app.router.add_get(
                inst["ws_path"],
                make_ws_proxy(inst["host"], inst["port"]),
            )

    mounted = []
    if not args.no_imageweb and packages:
        try:
            mounted = mount_imageweb(app, config, args.astro_ph)
        except Exception as e:                       # noqa: BLE001 — report, don't die
            log.error("quick look unavailable: %s", e)

    # aiohttp's static route refuses a bare directory (403 rather than an
    # index), so the landing page needs its own route.
    async def landing(request):
        return web.FileResponse(REPO / "index.html")

    app.router.add_get("/", landing)
    # Static last: it is a prefix resource on "/" and would otherwise
    # shadow everything above it.
    app.router.add_static("/", REPO, show_index=False)

    async def on_start(a):
        a["session"] = ClientSession()

    async def on_stop(a):
        await a["session"].close()

    app.on_startup.append(on_start)
    app.on_cleanup.append(on_stop)
    app["mounted_imageweb"] = mounted
    return app


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--deployment", type=Path, default=None,
                    help="deployment file (default: the only one in deployments/)")
    ap.add_argument("--host", default="0.0.0.0", help="listen address (default 0.0.0.0)")
    ap.add_argument("--port", type=int, default=None,
                    help="listen port (default: the deployment's gateway.port)")
    ap.add_argument("--astro-ph", dest="astro_ph", type=Path,
                    default=REPO.parent / "astro-ph-labs" / "astro-ph",
                    help="astro-ph monorepo checkout, for the quick-look JS packages")
    ap.add_argument("--guider-host", default="127.0.0.1", help="gcam bridge host")
    ap.add_argument("--guider-port", type=int, default=8765, help="gcam bridge port")
    ap.add_argument("--no-guiders", action="store_true", help="do not proxy /guider/")
    ap.add_argument("--no-imageweb", action="store_true", help="do not mount quick look")
    ap.add_argument("--proxy-ws", action="store_true",
                    help="proxy /<app>/ws in-process instead of leaving it to cloudflared")
    return ap.parse_args(argv)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    args = parse_args()

    deployment = args.deployment or deployment_config.default_deployment()
    if deployment is None:
        raise SystemExit("more than one deployment in deployments/ — name one with --deployment")
    config = deployment_config.resolve(deployment)
    port = args.port or config["gateway_port"]

    app = build_app(config, args)

    print(f"lco-gateway: deployment {config['name']} ({deployment}) on http://{args.host}:{port}/")
    for inst in config["instruments"]:
        ws = "proxied here" if args.proxy_ws else "via cloudflared"
        ql = f", quick look /image/{inst['app']}/" if inst["quicklook"] else ""
        print(f"  {inst['app']:<10} {inst['host']}:{inst['port']}  {inst['ws_path']} ({ws}){ql}")
    for g in config["guiders"]:
        where = "disabled" if args.no_guiders else f"page here, channels → {args.guider_host}:{args.guider_port}/guider/{g['gcam_name']}/"
        print(f"  {g['name']:<10} {g['path']} ({where})")
    print("  /config.json, /healthz, and the SPA at /")

    web.run_app(app, host=args.host, port=port, print=None)


if __name__ == "__main__":
    main()
