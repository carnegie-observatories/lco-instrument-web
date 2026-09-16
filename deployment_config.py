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
        # Two names, two jobs. `name` is the operational one -- the
        # lco-ansible gcam_guiders name, the .app bundle, what the inventory
        # cross-check matches on -- and it is the URL, because it says what
        # the camera is for (pfs-sv: the PFS slit viewer). `gcam_name` is
        # gcamweb's addressing, gcamPG (rotator-port digit + guider number),
        # which is the only form gcamweb's --guider accepts; the gateway
        # serves the page under `name` and proxies the live channels to
        # gcamweb under `gcam_name`. It is required: nothing else reaches
        # the upstream, and an operational name can never be one.
        if not g.get("gcam_name"):
            raise DeploymentError(
                f"{path}: guider {g['name']!r} has no gcam_name (gcamweb's gcamPG name for it)"
            )
        guiders.append({
            "name": g["name"],
            "gcam_name": g["gcam_name"],
            "title": g.get("title") or g["name"],
            "path": f"/guider/{g['name']}/",
            "host": g.get("address") or g["host"],
            # The command port (52200+gnum) is deliberately absent: it is
            # gcam's single-client text interface and nothing on the web
            # side may ever dial it. The image port is what gcamweb holds,
            # and what the inventory's gateway_gcamweb_guiders must agree with.
            "image_port": image_base + g["gnum"],
        })

    return {
        "name": dep["name"],
        "title": dep.get("title") or dep["name"],
        "domain": dep.get("domain"),
        "gateway_port": (dep.get("gateway") or {}).get("port", 8080),
        # No dev_ports. Quick look and the guiders used to be separate
        # processes on 8766 and 8765, so a page served over plain http had
        # to address them by port; the gateway now serves both on its own
        # origin, so "/image/<app>/" and "/guider/<name>/" are relative
        # paths that work identically behind the tunnel and on the LAN.
        #
        # Leaving the ports in was not harmless: the Quick Look iframe kept
        # pointing at 127.0.0.1:8766, where nothing listens any more, so the
        # tab loaded a dead origin, imageweb saw no viewers, and it logged
        # "recorded ... (no viewers; decode deferred)" for every exposure.
        # Consumers already fall back to the relative path when this key is
        # absent, which is the correct behaviour in every deployment shape.
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
