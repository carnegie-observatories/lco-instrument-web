"""Turn a deployment file into the config the browser consumes.

`deployments/<name>.yml` is written for people: ports are implied by
each app's PROJECT_ID, hosts are ansible inventory names, and the
Quick Look tab is a boolean. The SPA wants none of that — it wants
resolved ports, dialable addresses and ready-made paths. This module
is the single place that translation happens, so `server.py` (dev) and
the gateway (Phase 2) cannot disagree about it.

The output is deliberately *both* connect shapes for every instrument:

    ws_path   "/pfs/ws"        used when the page is served over https
                               (behind the tunnel, instruments are paths)
    host/port 127.0.0.1:51603  used on plain http (LAN / local dev)

The page picks by its own protocol; nothing here needs to know which
deployment shape is in play. See docs/plans/deployment-config-plan.md.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent
PORTS = REPO / "instruments" / "ports.yml"
DEPLOYMENTS = REPO / "deployments"


class DeploymentError(Exception):
    """The deployment file is unusable — not merely imperfect."""


def load_ports(path: Path = PORTS) -> dict:
    return yaml.safe_load(path.read_text()) or {}


def load(path: Path) -> dict:
    dep = yaml.safe_load(Path(path).read_text())
    if not isinstance(dep, dict) or "name" not in dep:
        raise DeploymentError(f"{path}: not a deployment file")
    return dep


def resolve(path: Path | str, ports: dict | None = None) -> dict:
    """Load a deployment file and return the browser-facing config."""
    dep = load(Path(path))
    ports = ports if ports is not None else load_ports()
    apps = ports.get("apps") or {}
    guider_base = (ports.get("guiders") or {}).get("command_base", 52200)
    image_base = (ports.get("guiders") or {}).get("image_base", 52300)

    instruments = []
    for inst in dep.get("instruments") or []:
        app = inst["app"]
        entry = apps.get(app)
        port = inst.get("port") or (entry or {}).get("ws")
        if not port:
            raise DeploymentError(
                f"{path}: instrument {app!r} has no port and none in instruments/ports.yml"
            )
        entry_out = {
            "app": app,
            "title": inst.get("title") or app.upper(),
            # `address` is what to dial; `host` stays the inventory name,
            # which may well not resolve in DNS.
            "host": inst.get("address") or inst["host"],
            "port": port,
            "ws_path": f"/{app}/ws",
            "quicklook": bool(inst.get("quicklook")),
        }
        if entry_out["quicklook"]:
            entry_out["quicklook_path"] = f"/image/{app}/"
        instruments.append(entry_out)

    guiders = []
    for g in dep.get("guiders") or []:
        guiders.append({
            "name": g["name"],
            "title": g.get("title") or g["name"],
            "path": f"/guider/{g['name']}/",
            "host": g.get("address") or g["host"],
            "command_port": guider_base + g["gnum"],
            "image_port": image_base + g["gnum"],
        })

    return {
        "name": dep["name"],
        "title": dep.get("title") or dep["name"],
        "domain": dep.get("domain"),
        "gateway_port": (dep.get("gateway") or {}).get("port", 8080),
        # Where the quick-look and guider web apps live when the page is
        # served over plain http. Behind the tunnel they are paths on the
        # page's own hostname and these are unused. Separate processes
        # today; Phase 2 folds both into the gateway port, and this is the
        # one place that changes.
        "dev_ports": {"image": 8766, "guider": 8765},
        "instruments": instruments,
        "guiders": guiders,
    }


def default_deployment() -> Path | None:
    """The deployment file to use when none is named.

    Exactly one file means no ambiguity; more than one is a choice the
    caller has to make, so return nothing rather than guess wrong.
    """
    files = sorted(DEPLOYMENTS.glob("*.yml"))
    return files[0] if len(files) == 1 else None
