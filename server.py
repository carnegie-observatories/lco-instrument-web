#!/usr/bin/env python3
# Tiny static file server for the LCO instrument web SPA. Use:
#   uv run python server.py                          # serves on :8080
#   uv run python server.py -p 9090                  # custom port
#   uv run python server.py --deployment deployments/sbs.yml
#
# The root URL serves the landing page, which builds itself from
# /config.json — this server's rendering of the deployment file. The
# instruments and guiders on the page are whatever that file declares;
# nothing here is hardcoded. Direct SPA links still work:
#   app.html?host=<host>&port=<ws port>     (LAN / local dev)
#   app.html?ws_path=/<app>/ws              (behind the tunnel)
#
# Superseded by the gateway in Phase 2 (deployment-config-plan.md);
# until then this is what serves a deployment.
import argparse
import json
import os
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import deployment_config


class NoStoreHandler(SimpleHTTPRequestHandler):
    """Static handler that forbids caching at every layer.

    Without cache headers, Cloudflare's edge caches .js/.css by
    default (~2 h TTL) while leaving .html uncached — so after a
    deploy, browsers get fresh HTML importing stale JS. Observed
    live: a stale ws.js produced wss://localhost/ws against the
    new index.html. The SPA is a few hundred KB; correctness on a
    telescope control surface beats cache hits.
    """

    # Set from main(); the config is read once at startup so a broken
    # deployment file fails loudly on launch rather than on first load.
    config_bytes = b"{}"

    def do_GET(self):
        if self.path.split("?")[0] == "/config.json":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(self.config_bytes)))
            self.end_headers()
            self.wfile.write(self.config_bytes)
            return
        super().do_GET()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        # Cross-origin isolation, tunnel mode only. The Quick Look iframe
        # (imageweb) only gets crossOriginIsolated — SharedArrayBuffer for
        # its decode pool — when the top-level document sends COOP/COEP
        # too. Behind the tunnel everything shares one origin (instruments
        # are paths), so this is safe; requests forwarded by cloudflared
        # carry Cf-Ray. Local/VPN serving stays permissive: there the
        # gateway is a different origin (its own port) and a COEP parent
        # would refuse the iframe outright, while without COEP it embeds
        # fine and the embedded page falls back to inline decode.
        if "Cf-Ray" in self.headers:
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        super().end_headers()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-p", "--port", type=int, default=8080,
                        help="HTTP port to listen on (default: 8080)")
    parser.add_argument("-d", "--dir", type=Path, default=Path(__file__).resolve().parent,
                        help="directory to serve (default: this script's directory)")
    parser.add_argument("--deployment", type=Path, default=None,
                        help="deployment file to serve as /config.json "
                             "(default: the only file in deployments/)")
    args = parser.parse_args()

    deployment = args.deployment or deployment_config.default_deployment()
    if deployment is None:
        parser.error("more than one deployment in deployments/ — name one with --deployment")
    config = deployment_config.resolve(deployment)
    NoStoreHandler.config_bytes = json.dumps(config, indent=2).encode()

    os.chdir(args.dir)
    addr = ("0.0.0.0", args.port)
    print(f"Serving {args.dir} on http://localhost:{args.port}/")
    print(f"Deployment {config['name']} ({deployment}) at /config.json:")
    for inst in config["instruments"]:
        ql = " + quick look" if inst["quicklook"] else ""
        print(f"  {inst['app']:<10} {inst['host']}:{inst['port']}  {inst['ws_path']}{ql}")
    for g in config["guiders"]:
        print(f"  {g['name']:<10} {g['host']}:{g['command_port']}  {g['path']}")
    HTTPServer(addr, NoStoreHandler).serve_forever()


if __name__ == "__main__":
    main()
