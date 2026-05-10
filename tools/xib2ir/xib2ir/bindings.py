"""bindings.yml loader + IR application.

The YAML maps outlet name → write/read spec (and optionally an option-value
override for popups). The converter walks the IR after building it and
attaches `binding.write` / `binding.read` to each element whose outlet has a
spec.

Phase 5 (this commit): write specs only — bindings populate `binding.write`
and override popup option values where the XIB title differs from the wire
enum. Phase 6 will add `read` clauses (state-driven topic subscriptions).

YAML shape:

    ignore_outlets: [view_eng, ...]   # outlets present in the XIB but
                                       # decorative; suppress unbound warnings.
    outlets:
      popup_adc:
        options:                        # display-label → wire-value
          Retracted: out
          Inserted:  in
        write:
          cmd: set_adc_insertion
          args: { pos: $state }
"""

from pathlib import Path
from typing import Any

import yaml


def load(path: Path | str) -> dict:
    """Read and parse a bindings.yml document. Returns {} if the file is empty."""
    with open(path) as f:
        return yaml.safe_load(f) or {}


def apply(layout: dict, spec: dict) -> list[dict]:
    """Mutate ``layout['elements']`` in place; return any new warnings.

    For each element with an outlet:
      * If the outlet appears in ``spec['ignore_outlets']``, leave the
        binding null and skip warning emission downstream.
      * If the outlet has an ``options`` block in the spec, override each
        option's wire value (the option's label is matched against the
        spec's keys).
      * If the outlet has ``write``/``read`` blocks, attach them to
        ``element['binding']``.
    """
    if not spec:
        return []

    ignore = set(spec.get("ignore_outlets") or [])
    outlets_spec: dict[str, Any] = spec.get("outlets") or {}
    new_warnings: list[dict] = []
    seen: set[str] = set()

    for el in layout["elements"]:
        outlet = el.get("outlet")
        if not outlet:
            continue
        if outlet in ignore:
            continue
        s = outlets_spec.get(outlet)
        if s is None:
            continue
        seen.add(outlet)

        if "options" in s and "options" in el:
            override = s["options"] or {}
            for opt in el["options"]:
                label = opt.get("label")
                if label in override:
                    opt["value"] = str(override[label])

        binding = el.get("binding") or {}
        if "write" in s and s["write"]:
            binding["write"] = s["write"]
        if "read" in s and s["read"]:
            binding["read"] = s["read"]
        if "hidden_if" in s and s["hidden_if"]:
            binding["hidden_if"] = s["hidden_if"]
        el["binding"] = binding or None

    # Surface bindings.yml entries that don't correspond to any outlet in
    # the IR — typo guard.
    for outlet in outlets_spec:
        if outlet in ignore or outlet in seen:
            continue
        new_warnings.append({
            "kind": "stale_binding",
            "outlet": outlet,
            "message": f"bindings.yml references outlet {outlet!r} not present in this window",
        })

    return new_warnings
