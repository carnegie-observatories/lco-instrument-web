#!/usr/bin/env python3
# Tiny static file server for the LCO instrument web SPA. Use:
#   python3 server.py            # serves on http://localhost:8080/
#   python3 server.py -p 9090    # custom port
# http://localhost:8080/ serves the landing page (instrument chooser).
# Direct SPA links: app.html?host=localhost&port=52403 (ADC),
#                   app.html?host=localhost&port=51703 (DCU),
#                   app.html?host=localhost&port=51603 (PFS).
import argparse
import os
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path


class NoStoreHandler(SimpleHTTPRequestHandler):
    """Static handler that forbids caching at every layer.

    Without cache headers, Cloudflare's edge caches .js/.css by
    default (~2 h TTL) while leaving .html uncached — so after a
    deploy, browsers get fresh HTML importing stale JS. Observed
    live: a stale ws.js produced wss://localhost/ws against the
    new index.html. The SPA is a few hundred KB; correctness on a
    telescope control surface beats cache hits.
    """

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
    args = parser.parse_args()

    os.chdir(args.dir)
    addr = ("0.0.0.0", args.port)
    print(f"Serving {args.dir} on http://localhost:{args.port}/")
    print( "The root URL is the instrument chooser; direct SPA links use")
    print( "app.html?host=localhost&port=52403 (ADC) / 51703 (DCU) / 51603 (PFS).")
    HTTPServer(addr, NoStoreHandler).serve_forever()


if __name__ == "__main__":
    main()
