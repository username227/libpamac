#!/usr/bin/env python3
"""Compare independently captured legacy and experimental VCS update decisions.

This tool does not run either checker, contact the network, or modify packages.
A match is only meaningful when both captures attest to the same installed,
packaging, and remote source snapshot. Incomplete evidence is never a pass.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sys

SCHEMA = 1
HEX_SHA256 = re.compile(r"^[a-f0-9]{64}$")
STATUS = {"update", "current", "incomplete", "error", "deferred"}
CONTEXT_FIELDS = ("installed_state_sha256", "packaging_state_sha256",
                  "remote_state_sha256", "configuration_sha256")


class InvalidCapture(ValueError):
    pass


def load_capture(path: Path, expected_method: str) -> dict:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise InvalidCapture(f"{path}: unsupported capture schema")
    if doc.get("method") != expected_method:
        raise InvalidCapture(f"{path}: expected method {expected_method}")
    state = doc.get("state")
    if not isinstance(state, dict):
        raise InvalidCapture(f"{path}: missing shared-state evidence")
    for field in CONTEXT_FIELDS:
        if not isinstance(state.get(field), str) or not HEX_SHA256.fullmatch(state[field]):
            raise InvalidCapture(f"{path}: invalid or absent {field}")
    packages = doc.get("packages")
    if not isinstance(packages, list):
        raise InvalidCapture(f"{path}: packages must be a list")
    index = {}
    for item in packages:
        if not isinstance(item, dict) or not isinstance(item.get("package"), str):
            raise InvalidCapture(f"{path}: invalid package entry")
        name = item["package"]
        if not name or name in index:
            raise InvalidCapture(f"{path}: empty/duplicate package {name!r}")
        if item.get("status") not in STATUS:
            raise InvalidCapture(f"{path}: invalid status for {name}")
        if not isinstance(item.get("installed_version"), str) or not item["installed_version"]:
            raise InvalidCapture(f"{path}: missing installed version for {name}")
        if not isinstance(item.get("packagebase"), str) or not item["packagebase"]:
            raise InvalidCapture(f"{path}: missing package base for {name}")
        candidate = item.get("candidate_version")
        if item["status"] == "update" and (not isinstance(candidate, str) or not candidate):
            raise InvalidCapture(f"{path}: upgrade candidate version missing for {name}")
        index[name] = item
    return {"state": state, "packages": index}


def compare(legacy: dict, lightweight: dict) -> dict:
    """Return an evidence-conscious comparison; only matched states can pass."""
    context_diff = [field for field in CONTEXT_FIELDS
                    if legacy["state"].get(field) != lightweight["state"].get(field)]
    results = []
    for name in sorted(set(legacy["packages"]) | set(lightweight["packages"])):
        old = legacy["packages"].get(name)
        new = lightweight["packages"].get(name)
        category = "inconclusive"
        reasons = []
        if context_diff:
            reasons.append("Incomparable scan states: " + ", ".join(context_diff))
        elif old is None or new is None:
            reasons.append("Package absent from one capture")
        elif (old["installed_version"] != new["installed_version"] or
              old["packagebase"] != new["packagebase"]):
            reasons.append("Installed version or package-base identity changed")
        elif old["status"] in ("incomplete", "deferred", "error") or new["status"] in ("incomplete", "deferred", "error"):
            reasons.append("One checker was incomplete, deferred, or failed")
        elif old["status"] == new["status"] == "current":
            category = "match-current"
        elif old["status"] == new["status"] == "update":
            if old["candidate_version"] == new["candidate_version"]:
                category = "match-update"
            else:
                category = "version-mismatch"
                reasons.append("Both detect upgrade but disagree on candidate version")
        elif old["status"] == "update" and new["status"] == "current":
            category = "potential-missed-upgrade"
            reasons.append("Legacy checker reports an upgrade but lightweight checker does not")
        elif old["status"] == "current" and new["status"] == "update":
            category = "potential-extra-rebuild"
            reasons.append("Lightweight checker reports an upgrade absent from legacy result")
        results.append({"package": name,
                        "legacy": old["status"] if old else "missing",
                        "lightweight": new["status"] if new else "missing",
                        "category": category, "reasons": reasons})
    counts = dict(sorted(Counter(row["category"] for row in results).items()))
    clean = (not context_diff and len(results) > 0 and
             all(row["category"] in ("match-current", "match-update") for row in results))
    return {"schema": SCHEMA, "comparable": not context_diff,
            "matched_without_discrepancies": clean,
            "state_mismatches": context_diff, "counts": counts, "packages": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy", required=True, type=Path,
                        help="Captured output of original Pamac checker")
    parser.add_argument("--lightweight", required=True, type=Path,
                        help="Captured output of new read-only checker")
    parser.add_argument("--output", type=Path,
                        help="Optional JSON report (no changes to Pamac)")
    args = parser.parse_args(argv)
    try:
        old = load_capture(args.legacy, "legacy")
        new = load_capture(args.lightweight, "lightweight")
        report = compare(old, new)
        output = json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.output:
            args.output.write_text(output, encoding="utf-8")
        else:
            sys.stdout.write(output)
        return 0 if report["matched_without_discrepancies"] else 2
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"vcs A/B comparison failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())