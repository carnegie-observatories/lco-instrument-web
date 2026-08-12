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
    HTTPServer(addr, SimpleHTTPRequestHandler).serve_forever()


if __name__ == "__main__":
    main()
