#!/usr/bin/env python3
"""Sync deploy/access-policies.yml to Cloudflare Access.

One Access application per telescope (apex + wildcard subdomain),
one Allow policy per application, rebuilt from the YAML on every
run. Idempotent: finds existing applications by domain, updates in
place; creates on first run.

Usage:
    export CLOUDFLARE_API_TOKEN=...   # needs "Access: Apps and Policies Write"
    python3 deploy/sync-access-policies.py                # sync all telescopes
    python3 deploy/sync-access-policies.py --dry-run      # print, change nothing
    python3 deploy/sync-access-policies.py --telescope clay
    python3 deploy/sync-access-policies.py --allow-lockout  # permit empty allowed-lists

Wildcards: only whole-domain patterns ("*@carnegiescience.edu") are
supported — they map to Access "Emails ending in" rules. Exact
addresses map to "Email" rules. Anything else is an error.
"""

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:
    sys.exit("error: PyYAML not installed — run: python3 -m pip install pyyaml")

API = "https://api.cloudflare.com/client/v4"

EXACT_EMAIL = re.compile(r"^[^*@\s]+@[^*@\s]+\.[^*@\s]+$")
DOMAIN_WILDCARD = re.compile(r"^\*@([^*@\s]+\.[^*@\s]+)$")


def api(method: str, path: str, token: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{API}{path}",
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        data=json.dumps(body).encode() if body is not None else None,
    )
    with urllib.request.urlopen(req) as resp:
        out = json.load(resp)
    if not out.get("success"):
        raise RuntimeError(f"{method} {path}: {out.get('errors')}")
    return out["result"]


def include_rules(allowed: list[str]) -> list[dict]:
    """Map YAML allowed-entries to Access policy include rules."""
    rules = []
    for entry in allowed:
        entry = entry.strip()
        m = DOMAIN_WILDCARD.match(entry)
        if m:
            rules.append({"email_domain": {"domain": m.group(1)}})
        elif EXACT_EMAIL.match(entry):
            rules.append({"email": {"email": entry}})
        else:
            sys.exit(
                f"error: unsupported allowed-entry {entry!r}.\n"
                "  Supported: exact addresses (a@b.edu) and whole-domain "
                "wildcards (*@b.edu).\n"
                "  Cloudflare Access has no partial-local-part or "
                "multi-level-domain wildcard."
            )
    return rules


def sync_telescope(name: str, cfg: dict, account: str, session: str,
                   token: str, dry_run: bool, allow_lockout: bool) -> None:
    domain = cfg["domain"]
    allowed = cfg.get("allowed") or []
    if not allowed and not allow_lockout:
        sys.exit(f"error: {name}: empty allowed-list would lock everyone out "
                 "(pass --allow-lockout if intentional)")

    rules = include_rules(allowed)
    app_body = {
        "name": f"{name} telescope control",
        "type": "self_hosted",
        # Apex serves the SPA + landing; the wildcard covers per-
        # instrument subdomains (pfs.clay..., dcu.clay...).
        "domain": domain,
        "self_hosted_domains": [domain, f"*.{domain}"],
        "session_duration": session,
    }
    policy_body = {
        "name": f"{name} operators",
        "decision": "allow",
        "include": rules,
        "exclude": [],
        "require": [],
    }

    if dry_run:
        print(f"--- {name} ({domain}) ---")
        print(json.dumps({"app": app_body, "policy": policy_body}, indent=2))
        return

    apps = api("GET", f"/accounts/{account}/access/apps", token)
    existing = next((a for a in apps if a.get("domain") == domain), None)

    if existing:
        app = api("PUT", f"/accounts/{account}/access/apps/{existing['id']}",
                  token, app_body)
        print(f"{name}: updated app {app['id']}")
    else:
        app = api("POST", f"/accounts/{account}/access/apps", token, app_body)
        print(f"{name}: created app {app['id']}")

    policies = api("GET",
                   f"/accounts/{account}/access/apps/{app['id']}/policies",
                   token)
    existing_policy = next(
        (p for p in policies if p.get("name") == policy_body["name"]), None)

    if existing_policy:
        api("PUT",
            f"/accounts/{account}/access/apps/{app['id']}/policies/{existing_policy['id']}",
            token, policy_body)
        print(f"{name}: updated policy ({len(rules)} rule(s))")
    else:
        api("POST",
            f"/accounts/{account}/access/apps/{app['id']}/policies",
            token, policy_body)
        print(f"{name}: created policy ({len(rules)} rule(s))")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path,
                        default=Path(__file__).parent / "access-policies.yml")
    parser.add_argument("--telescope", help="sync only this telescope")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-lockout", action="store_true",
                        help="permit an empty allowed-list")
    args = parser.parse_args()

    token = os.environ.get("CLOUDFLARE_API_TOKEN")
    if not token and not args.dry_run:
        sys.exit("error: CLOUDFLARE_API_TOKEN not set "
                 "(needs 'Access: Apps and Policies Write')")

    cfg = yaml.safe_load(args.config.read_text())
    account = cfg["account_id"]
    session = cfg.get("session_duration", "24h")
    telescopes = cfg.get("telescopes") or {}

    if args.telescope:
        if args.telescope not in telescopes:
            sys.exit(f"error: unknown telescope {args.telescope!r} "
                     f"(have: {', '.join(telescopes)})")
        telescopes = {args.telescope: telescopes[args.telescope]}

    for name, tcfg in telescopes.items():
        sync_telescope(name, tcfg, account, session, token or "",
                       args.dry_run, args.allow_lockout)


if __name__ == "__main__":
    main()
