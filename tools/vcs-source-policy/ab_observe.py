#!/usr/bin/env python3
"""Compare saved Pamac --quiet output with VCS remote observations.

This is a diagnostic, NEVER a valid A/B pass: the two checkers have not
attested to identical remote refs, packaging state, or installed binaries.
It never runs Pamac or reads/writes package configuration.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

# pamac checkupdates --aur --devel --quiet: name  installed -> candidate
UPDATE_LINE = re.compile(r"^([^\s]+)\s{2,}(\S+)\s+->\s+(\S+)\s*$")
SUCCEEDED = {0, 100}


def legacy_lines(text: str) -> tuple[dict, list[str]]:
    updates = {}
    ignored = []
    for line in text.splitlines():
        if not line.strip():
            continue
        match = UPDATE_LINE.fullmatch(line)
        if not match:
            ignored.append(line)
            continue
        name, installed, candidate = match.groups()
        if name in updates:
            raise ValueError("Duplicate package in legacy output: " + name)
        updates[name] = {"installed_version": installed, "candidate_version": candidate}
    return updates, ignored


def compare_observations(probe: dict, legacy_stdout: str, exit_code: int) -> dict:
    if probe.get("schema") != 1 or probe.get("purpose") != "research-remote-observation-only":
        raise ValueError("Unrecognized lightweight research probe report")
    if not isinstance(probe.get("packages"), list):
        raise ValueError("Probe packages must be a list")
    updates, unparsed = legacy_lines(legacy_stdout)
    rows = []
    seen = set()
    for pkg in probe["packages"]:
        name = pkg["package"]
        if name in seen:
            raise ValueError("Duplicate probe package: " + name)
        seen.add(name)
        legacy = updates.get(name)
        observation = pkg["status"]
        category = "inconclusive"
        reasons = []
        if exit_code not in SUCCEEDED:
            reasons.append("Pamac check incomplete or failed (exit " + str(exit_code) + ")")
        elif legacy is not None and legacy["installed_version"] != pkg["installed_version"]:
            reasons.append("Installed package version differs between runs")
        elif observation == "remote-match" and legacy is None:
            category = "both-show-no-remote-update"
            reasons.append("Absence from Pamac output is not proof the package was checked")
        elif observation == "remote-difference" and legacy is not None:
            category = "both-signal-possible-update"
            reasons.append("Candidate version and identical remote state are not verified")
        elif observation == "remote-difference" and legacy is None:
            category = "possible-missed-update"
            reasons.append("Git remote differs; verify ancestry and Pamac coverage")
        elif observation == "remote-match" and legacy is not None:
            category = "possible-extra-legacy-rebuild"
            reasons.append("AUR packaging changes may explain Pamac's update decision")
        else:
            reasons.append("Lightweight probe has no decisive remote signal")
        rows.append({"package": name, "remote_observation": observation,
                     "legacy": ("reported-update" if legacy else "not-listed"),
                     "candidate_version": legacy["candidate_version"] if legacy else None,
                     "category": category, "reasons": reasons})
    return {"schema": 1, "purpose": "exploratory-ab-diagnostic",
            "strictly_comparable": False, "validated_ab_pass": False,
            "legacy_exit_code": exit_code, "unparsed_legacy_lines": unparsed,
            "legacy_packages_not_in_probe": sorted(set(updates) - seen),
            "results": rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", type=Path, required=True)
    parser.add_argument("--legacy-stdout", type=Path, required=True,
                        help="Previously saved output of pamac checkupdates --aur --devel --quiet")
    parser.add_argument("--legacy-exit-code", type=int, required=True,
                        help="Captured exit status: 0/100 complete, 101-103 incomplete")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        trial = compare_observations(
            json.loads(args.probe.read_text(encoding="utf-8")),
            args.legacy_stdout.read_text(encoding="utf-8"),
            args.legacy_exit_code)
        text = json.dumps(trial, sort_keys=True, indent=2) + "\n"
        if args.output:
            args.output.write_text(text, encoding="utf-8")
        else:
            print(text, end="")
        return 0
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print("ab-observe: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())