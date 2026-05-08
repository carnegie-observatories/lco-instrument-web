#!/usr/bin/env python3
# Tiny static file server for the LCO instrument web SPA. Use:
#   python3 server.py            # serves on http://localhost:8080/
#   python3 server.py -p 9090    # custom port
# Then open http://localhost:8080/?host=localhost&port=52403 (ADC) or
#                                  ?host=localhost&port=51703 (DCU).
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
    print( "Tip: open with ?host=localhost&port=52403 for ADC,")
    print( "                ?host=localhost&port=51703 for DCU.")
    HTTPServer(addr, SimpleHTTPRequestHandler).serve_forever()


if __name__ == "__main__":
    main()
