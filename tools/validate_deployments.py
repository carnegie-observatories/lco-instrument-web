#!/usr/bin/env python3
"""Validate deployments/*.yml against the schema and the ansible inventory.

Two checks, both cheap enough to run in CI on every PR:

1. **Schema** — deployments/schema.json, plus the cross-file rules a JSON
   Schema cannot express: every `app` exists in instruments/ports.yml and
   has a WS server; every referenced instruments/<app>/ UI directory is
   present; names are unique.

2. **Inventory** — lco-ansible is the authority on which machines exist
   and what runs on them, so every host named here must exist there, and
   every app must be one the inventory actually installs on that host.
   Subset, not equality: an app without a browser surface (GuidePaddle is
   a TCS client, not a server) is legitimately absent from a deployment
   file. A *superset* is always an error.

   Guider names must match exactly — a guider in the inventory but not in
   the deployment is a missing web surface, not a deliberate omission, so
   it is reported as a warning rather than passed over.

Usage:
    uv run python tools/validate_deployments.py
    uv run python tools/validate_deployments.py --inventory ../lco-ansible
    uv run python tools/validate_deployments.py --no-inventory
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
DEPLOYMENTS = REPO / "deployments"
PORTS = REPO / "instruments" / "ports.yml"
SCHEMA = DEPLOYMENTS / "schema.json"


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, where: str, msg: str) -> None:
        self.errors.append(f"{where}: {msg}")

    def warn(self, where: str, msg: str) -> None:
        self.warnings.append(f"{where}: {msg}")


# --------------------------------------------------------------------------
# ansible inventory
# --------------------------------------------------------------------------

def walk_inventory(path: Path) -> dict[str, dict]:
    """Return {hostname: merged vars} from an ansible YAML inventory.

    Group `vars:` blocks are inherited by every host beneath them, with
    nearer groups and the host's own vars winning — the same precedence
    ansible applies within a single inventory file. That matters because
    `instruments:` sits on the group for Clay and on the host for SBS.
    """
    tree = yaml.safe_load(path.read_text()) or {}
    hosts: dict[str, dict] = {}

    def descend(node: dict, inherited: dict) -> None:
        if not isinstance(node, dict):
            return
        merged = {**inherited, **(node.get("vars") or {})}
        for name, hv in (node.get("hosts") or {}).items():
            hosts.setdefault(name, {}).update({**merged, **(hv or {})})
        for child in (node.get("children") or {}).values():
            descend(child or {}, merged)

    for group in tree.values():
        descend(group or {}, {})
    return hosts


def load_inventories(root: Path) -> dict[str, dict]:
    hosts: dict[str, dict] = {}
    found = sorted(root.glob("inventory*.yaml")) + sorted(root.glob("inventory*.yml"))
    if not found:
        raise FileNotFoundError(f"no inventory*.yaml under {root}")
    for inv in found:
        for name, vars_ in walk_inventory(inv).items():
            hosts.setdefault(name, {}).update(vars_)
    return hosts


def check_inventory(dep: dict, hosts: dict[str, dict], rep: Report) -> None:
    where = dep["_file"]

    def host_exists(name: str, what: str) -> bool:
        if name not in hosts:
            rep.error(where, f"{what} names host {name!r}, absent from the inventory")
            return False
        return True

    host_exists(dep["gateway"]["host"], "gateway")

    for inst in dep.get("instruments") or []:
        app, host = inst["app"], inst["host"]
        if not host_exists(host, f"instrument {app!r}"):
            continue
        installed = {
            str(e.get("name", "")).lower()
            for e in (hosts[host].get("instruments") or [])
            if isinstance(e, dict)
        }
        if not installed:
            rep.warn(where, f"{host!r} declares no instruments: in the inventory; cannot check {app!r}")
        elif app not in installed:
            rep.error(
                where,
                f"instrument {app!r} is not installed on {host!r} "
                f"(inventory has: {', '.join(sorted(installed)) or 'none'})",
            )

    declared = {g["name"] for g in (dep.get("guiders") or [])}
    for guider in dep.get("guiders") or []:
        host = guider["host"]
        if not host_exists(host, f"guider {guider['name']!r}"):
            continue
        known = {g.get("name") for g in (hosts[host].get("gcam_guiders") or []) if isinstance(g, dict)}
        if guider["name"] not in known:
            rep.error(
                where,
                f"guider {guider['name']!r} is not declared on {host!r} "
                f"(inventory has: {', '.join(sorted(n for n in known if n)) or 'none'})",
            )

    for host in {i["host"] for i in (dep.get("instruments") or [])} | {g["host"] for g in (dep.get("guiders") or [])}:
        for g in (hosts.get(host, {}).get("gcam_guiders") or []):
            if isinstance(g, dict) and g.get("name") and g["name"] not in declared:
                rep.warn(where, f"{host!r} runs guider {g['name']!r}, which has no web surface here")


# --------------------------------------------------------------------------
# schema + cross-file rules
# --------------------------------------------------------------------------

def check_schema(dep: dict, schema: dict, rep: Report) -> None:
    try:
        import jsonschema
    except ImportError:
        rep.warn(dep["_file"], "jsonschema not installed; structural check skipped")
        return
    validator = jsonschema.Draft202012Validator(schema)
    for err in sorted(validator.iter_errors({k: v for k, v in dep.items() if not k.startswith("_")}),
                      key=lambda e: list(e.path)):
        loc = "/".join(str(p) for p in err.path) or "(root)"
        rep.error(dep["_file"], f"{loc}: {err.message}")


def check_references(dep: dict, ports: dict, rep: Report) -> None:
    where = dep["_file"]
    apps = ports.get("apps") or {}

    if dep["name"] != Path(where).stem:
        rep.error(where, f"name {dep['name']!r} does not match the filename")

    seen_apps: set[str] = set()
    for inst in dep.get("instruments") or []:
        app = inst["app"]
        if app in seen_apps:
            rep.error(where, f"instrument {app!r} listed twice")
        seen_apps.add(app)

        entry = apps.get(app)
        if entry is None:
            rep.error(where, f"instrument {app!r} is not in instruments/ports.yml")
        elif not entry.get("ws_server") and "port" not in inst:
            rep.error(
                where,
                f"instrument {app!r} has no WS server yet (ports.yml ws_server: false); "
                "it cannot be served until it is migrated",
            )
        if not (REPO / "instruments" / app).is_dir():
            rep.error(where, f"instrument {app!r} has no UI directory at instruments/{app}/")

    seen_guiders: set[str] = set()
    for guider in dep.get("guiders") or []:
        if guider["name"] in seen_guiders:
            rep.error(where, f"guider {guider['name']!r} listed twice")
        seen_guiders.add(guider["name"])

    if not (dep.get("instruments") or dep.get("guiders")):
        rep.warn(where, "deployment has neither instruments nor guiders")

    if "access" in dep and not dep["access"]:
        rep.warn(where, "empty access list — this deployment is locked down to nobody")


def resolved_port(inst: dict, ports: dict) -> int | None:
    if "port" in inst:
        return inst["port"]
    entry = (ports.get("apps") or {}).get(inst["app"])
    return entry.get("ws") if entry else None


# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inventory", type=Path, default=REPO.parent / "lco-ansible",
                    help="lco-ansible checkout (default: ../lco-ansible)")
    ap.add_argument("--no-inventory", action="store_true", help="skip the inventory cross-check")
    ap.add_argument("deployments", nargs="*", type=Path, help="files to check (default: deployments/*.yml)")
    args = ap.parse_args()

    files = args.deployments or sorted(DEPLOYMENTS.glob("*.yml"))
    if not files:
        print("no deployment files found", file=sys.stderr)
        return 1

    schema = json.loads(SCHEMA.read_text())
    ports = yaml.safe_load(PORTS.read_text())
    rep = Report()

    hosts: dict[str, dict] = {}
    if args.no_inventory:
        print("inventory cross-check: skipped (--no-inventory)")
    else:
        try:
            hosts = load_inventories(args.inventory)
            print(f"inventory: {len(hosts)} hosts from {args.inventory}")
        except (OSError, FileNotFoundError) as e:
            rep.warn("inventory", f"{e}; cross-check skipped")

    for path in files:
        dep = yaml.safe_load(path.read_text())
        dep["_file"] = path.name
        check_schema(dep, schema, rep)
        check_references(dep, ports, rep)
        if hosts:
            check_inventory(dep, hosts, rep)

        insts = ", ".join(
            f"{i['app']}:{resolved_port(i, ports)}" for i in (dep.get("instruments") or [])
        ) or "none"
        guiders = ", ".join(
            f"{g['name']}:{52200 + g['gnum']}" for g in (dep.get("guiders") or [])
        ) or "none"
        print(f"  {path.name}: {dep.get('domain', '?')} — instruments [{insts}] guiders [{guiders}]")

    for w in rep.warnings:
        print(f"warning: {w}")
    for e in rep.errors:
        print(f"ERROR:   {e}", file=sys.stderr)

    if rep.errors:
        print(f"\n{len(rep.errors)} error(s), {len(rep.warnings)} warning(s)", file=sys.stderr)
        return 1
    print(f"\nOK — {len(files)} deployment(s), {len(rep.warnings)} warning(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
