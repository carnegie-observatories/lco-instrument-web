#!/usr/bin/env python3
"""lco-gateway — one process serving one deployment.

Replaces the three processes a deployment used to need (`server.py` for
static files, `imageweb` for quick look, the gcam bridge for guiders)
with a single front door, configured entirely by
`deployments/<name>.yml`:

    /                     SPA static files
    /config.json          the deployment, as the SPA consumes it
    /image/<app>/         quick-look viewer   — imageweb, in-process
    /guider/<name>/       web guider          — gcam bridge, proxied
    /<app>/ws             instrument control  — proxied, --proxy-ws only
    /healthz              per-target reachability

Two kinds of mounting, and the difference is deliberate. imageweb is a
member of this repo's uv workspace, so its instrument apps are built
in-process — one control-WS client per instrument, sharing this event
loop. The gcam bridge is not: it lives in carnegie-observatories/zwo,
ships on its own release cadence, and vendoring another repo's service
to avoid a loopback hop would be the wrong trade. It stays a separate
process and this gateway reverse-proxies to it.

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

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

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
    client = web.WebSocketResponse(protocols=request.headers.get("Sec-WebSocket-Protocol", "").split(", ") or ())
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
            # cancelled rather than left waiting on a dead socket.
            done, pending = await asyncio.wait(
                [asyncio.create_task(pump(client, upstream, "client->upstream")),
                 asyncio.create_task(pump(upstream, client, "upstream->client"))],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
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


def make_proxy(upstream_base: str):
    """Handler proxying this request's full path+query to `upstream_base`."""
    def target_for(request: web.Request) -> str:
        return f"{upstream_base}{request.rel_url}"

    async def handler(request: web.Request) -> web.StreamResponse:
        target = target_for(request)
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await proxy_ws(request, target.replace("http://", "ws://", 1))
        return await proxy_http(request, target)

    return handler


# --------------------------------------------------------------------------
# health
# --------------------------------------------------------------------------

async def reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        writer.close()
        await writer.wait_closed()
        return True
    except (OSError, asyncio.TimeoutError):
        return False


def health_handler(config: dict, guider_upstream: tuple[str, int] | None):
    async def handler(request: web.Request) -> web.Response:
        targets = [("instrument", i["app"], i["host"], i["port"]) for i in config["instruments"]]
        targets += [("guider", g["name"], g["host"], g["command_port"]) for g in config["guiders"]]
        if guider_upstream:
            targets.append(("bridge", "gcam", *guider_upstream))

        results = await asyncio.gather(*(reachable(h, p) for _, _, h, p in targets))
        checks = [
            {"kind": kind, "name": name, "target": f"{host}:{port}", "up": up}
            for (kind, name, host, port), up in zip(targets, results)
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

    for pkg in ("chz1", "core", "viewer"):
        app.router.add_static(f"/image/pkg/{pkg}/", astro_ph / "packages" / pkg)

    mounted = []
    for inst in quicklook:
        sub = iw.instrument_app(inst["app"], inst["host"], inst["port"], args)
        app.router.add_get(f"/image/{inst['app']}", iw.redirect(f"/image/{inst['app']}/"))
        app.add_subapp(f"/image/{inst['app']}/", sub)
        mounted.append(inst["app"])
    return mounted


def build_app(config: dict, args: argparse.Namespace) -> web.Application:
    app = web.Application(middlewares=[no_store])
    body = json.dumps(config, indent=2).encode()

    async def config_json(request):
        return web.Response(body=body, content_type="application/json")

    app.router.add_get("/config.json", config_json)

    guider_upstream = None
    if config["guiders"] and not args.no_guiders:
        guider_upstream = (args.guider_host, args.guider_port)
        app.router.add_route("*", "/guider/{tail:.*}",
                             make_proxy(f"http://{args.guider_host}:{args.guider_port}"))

    app.router.add_get("/healthz", health_handler(config, guider_upstream))

    if args.proxy_ws:
        for inst in config["instruments"]:
            app.router.add_get(
                inst["ws_path"],
                make_proxy(f"http://{inst['host']}:{inst['port']}"),
            )

    mounted = []
    if not args.no_imageweb:
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
        where = "disabled" if args.no_guiders else f"→ {args.guider_host}:{args.guider_port}"
        print(f"  {g['name']:<10} {g['path']} ({where})")
    print("  /config.json, /healthz, and the SPA at /")

    web.run_app(app, host=args.host, port=port, print=None)


if __name__ == "__main__":
    main()
