#!/usr/bin/env python3
"""Read-only research probe for primary Git source revisions.

This does not change Pamac or interpret an installed pkgver as verified build
provenance. Results are *remote observations*, not upgrade decisions.
No cloning, fetching, checkout, makepkg, or installation is ever performed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Callable
import urllib.parse


FULL_COMMIT = re.compile(r"^[0-9a-fA-F]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
PACKAGE_NAME = re.compile(r"^[A-Za-z0-9@._+\-]+$")
SHORT_COMMIT = re.compile(r"(?:^|[.+_])g?([0-9a-fA-F]{7,40})$")
SAFE_BRANCH = re.compile(r"^[A-Za-z0-9._/+-]+$")
ALLOWED_SCHEMES = {"https", "git"}


def reported_commit(version: str) -> str | None:
    """Recognize a common pkgver *hint*, never a verified installed SHA."""
    upstream = version.rsplit("-", 1)[0]
    if ":" in upstream:
        upstream = upstream.split(":", 1)[1]
    match = SHORT_COMMIT.search(upstream)
    return match.group(1).lower() if match else None


def source_target(source: str) -> dict:
    """Resolve an explicitly declared Git source without Git URL rewrites."""
    raw = source.split("::", 1)[-1] if "::" in source else source
    if not raw.startswith(("git+", "git://")):
        raise ValueError("Not a Git VCS source")
    raw = raw.removeprefix("git+")
    url, marker, fragment = raw.partition("#")
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme not in ALLOWED_SCHEMES or not parsed.hostname or
            parsed.username or parsed.password or parsed.port not in (None, 443, 9418) or
            parsed.query or parsed.fragment or not parsed.path.startswith("/") or
            any(part == ".." for part in parsed.path.split("/"))):
        raise ValueError("Unsupported or potentially interactive Git remote URL")
    if parsed.scheme == "https" and parsed.port not in (None, 443):
        raise ValueError("Non-standard HTTPS port not supported")
    if parsed.scheme == "git" and parsed.port not in (None, 9418):
        raise ValueError("Non-standard Git port not supported")
    ref = None
    ref_kind = "default-branch"
    if marker:
        field, sep, value = fragment.partition("=")
        if not sep or field not in ("branch", "tag", "commit") or not value:
            raise ValueError("Unsupported checkout selector")
        if field == "branch":
            if (not SAFE_BRANCH.fullmatch(value) or value.startswith("-") or
                    value.startswith("/") or "//" in value or ".." in value or
                    value.endswith(("/", ".", ".lock"))):
                raise ValueError("Unsafe Git branch selector")
            ref = "refs/heads/" + value
            ref_kind = "branch"
        else:
            # Tags and commits are fixed references; packaging updates are
            # handled independently by the original conservative checker.
            ref_kind = "pinned-" + field
    return {"url": url, "ref": ref, "ref_kind": ref_kind}


def parse_remote_output(output: str, target: dict) -> dict:
    """Require one unambiguous Git object ID and a stable explicit ref."""
    result: dict[str, str] = {}
    symref = None
    for line in output.splitlines():
        if line.startswith("ref: "):
            parts = line[5:].split("\t")
            if len(parts) != 2 or parts[1] != "HEAD" or symref is not None:
                raise ValueError("Unrecognized remote symbolic ref")
            symref = parts[0]
            continue
        parts = line.split("\t")
        if len(parts) != 2 or not FULL_COMMIT.fullmatch(parts[0]) or parts[1] in result:
            raise ValueError("Malformed or duplicate git ls-remote result")
        result[parts[1]] = parts[0].lower()
    if target["ref_kind"] == "default-branch":
        if not symref or not symref.startswith("refs/heads/") or "HEAD" not in result:
            raise ValueError("Remote default branch is not identifiable")
        return {"commit": result["HEAD"], "ref": symref}
    if target["ref_kind"] == "branch":
        wanted = target["ref"]
        if set(result) != {wanted}:
            raise ValueError("Requested branch missing from remote")
        return {"commit": result[wanted], "ref": wanted}
    raise ValueError("Fixed-source selectors must not query remote HEAD")


def ls_remote(target: dict, timeout: int = 12) -> dict:
    """Call git ls-remote only, without source objects or credential prompts."""
    if target["ref_kind"].startswith("pinned-"):
        raise ValueError("No moving ref to monitor")
    if not 1 <= timeout <= 60:
        raise ValueError("Remote timeout must be between 1 and 60 seconds")
    command = ["git", "-c", "protocol.allow=never",
               "-c", "protocol.https.allow=always",
               "-c", "protocol.git.allow=always", "ls-remote"]
    if target["ref_kind"] == "default-branch":
        command += ["--symref", target["url"], "HEAD"]
    else:
        command += ["--refs", target["url"], target["ref"]]
    env = dict(os.environ)
    env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_GLOBAL=os.devnull, GIT_SSH_COMMAND="false",
               GIT_ASKPASS="false", GCM_INTERACTIVE="never")
    try:
        completed = subprocess.run(command, check=False, text=True,
                                   capture_output=True, timeout=timeout,
                                   env=env, stdin=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise RuntimeError("Git remote query failed: " + type(exc).__name__) from exc
    if completed.returncode != 0:
        # Do not print remote stderr: Git credentials or tokens may occur there.
        raise RuntimeError("git ls-remote returned failure")
    return parse_remote_output(completed.stdout, target)


def parse_approvals(values: list[str]) -> dict[str, str]:
    approved = {}
    for value in values:
        name, marker, sha = value.partition("=")
        if (not marker or not PACKAGE_NAME.fullmatch(name) or
                not SHA256.fullmatch(sha) or name in approved):
            raise ValueError("Use unique PACKAGE=PKGBUILD_SHA256 review approvals")
        approved[name] = sha
    return approved


def assess_package(row: dict, policy: dict, *, approve: dict[str, str],
                   query: Callable[[dict], dict] = ls_remote) -> dict:
    """Observe only confidently identified main Git source; never decide upgrade."""
    package = row["package"]
    result = {"package": package, "packagebase": policy["packagebase"],
              "installed_version": row["installed_version"],
              "classification": policy["classification"], "status": "incomplete",
              "installed_revision_provenance": "unknown",
              "observed_ref": None, "remote_commit": None,
              "reported_installed_commit": reported_commit(row["installed_version"]),
              "reason": None}
    reasons = []
    if policy["installed_version"] != row["installed_version"]:
        reasons.append("Installed version changed during metadata collection")
    sources = policy["sources"]
    primary = [s for s in sources if s["role"] == "primary-candidate" and s["rebuild"] == "Y"]
    unresolved = [s for s in sources if s["role"] == "unresolved-git-source"]
    if len(primary) != 1 or unresolved:
        reasons.append("Primary or secondary Git source relationship unresolved")
    if policy["classification"] not in ("single-source", "submodule-metadata-candidate", "fixed-source"):
        reasons.append("Classifier could not establish complete source roles")
    # Identifying a sole Git repository is NOT permission to turn a moving
    # remote HEAD into an upgrade decision, or even to treat the reported
    # installed version as a reliable source binding. Require the independent
    # pkgver() check for all moving-source probes.
    if (policy["classification"] != "fixed-source" and
            not policy.get("pkgver_source_reference_confirmed", False)):
        reasons.append("pkgver() Git source binding has not been independently verified")
    if (policy["classification"] == "single-source" and
            policy.get("review_required", False)):
        reasons.append("PKGBUILD requires manual review before even an observational probe")
    if policy["classification"] == "submodule-metadata-candidate" and (
            approve.get(package) != policy.get("pkgbuild_sha256")):
        reasons.append("Submodule pinning requires review of this exact PKGBUILD hash")
    if reasons:
        result["reason"] = "; ".join(reasons)
        return result
    try:
        target = source_target(primary[0]["source"])
    except ValueError as exc:
        result["reason"] = str(exc)
        return result
    if target["ref_kind"].startswith("pinned-"):
        result["status"] = "pinned-source"
        result["reason"] = "Fixed commit/tag; updates require packaging metadata comparison"
        return result
    try:
        remote = query(target)
        if not FULL_COMMIT.fullmatch(remote["commit"]):
            raise ValueError("Invalid remote commit evidence")
        result["remote_commit"] = remote["commit"].lower()
        result["observed_ref"] = remote["ref"]
        hint = result["reported_installed_commit"]
        if hint:
            if result["remote_commit"].startswith(hint):
                result["status"] = "remote-match"
                result["reason"] = ("Remote HEAD matches commit abbreviation in installed "
                                    "version; packaging/build provenance remains unverified")
            else:
                result["status"] = "remote-difference"
                result["reason"] = ("Remote HEAD differs from reported installed version; "
                                    "ancestry, packaging changes and candidate version not proven")
        else:
            result["status"] = "incomplete"
            result["reason"] = "No recognizable Git commit abbreviation in installed version"
    except (ValueError, KeyError, RuntimeError) as exc:
        result["status"] = "error"
        result["reason"] = str(exc)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed", action="store_true", help="Inspect locally installed VCS packages")
    parser.add_argument("--manifest", type=Path, help="Local offline fixture manifest")
    parser.add_argument("--inspect-gitmodules", action="store_true",
                        help="Inspect small GitHub .gitmodules metadata without cloning")
    parser.add_argument("--reviewed-submodule", action="append", default=[],
                        metavar="PACKAGE=PKGBUILD_SHA256",
                        help="Research-only: acknowledge manual review of exact PKGBUILD")
    parser.add_argument("--timeout", type=int, default=12, help="Seconds per Git remote check (1-60)")
    parser.add_argument("--output", type=Path, help="Write read-only JSON report to this file")
    options = parser.parse_args(argv)
    if options.installed == bool(options.manifest):
        parser.error("Choose exactly one of --installed or --manifest")
    if not 1 <= options.timeout <= 60:
        parser.error("--timeout must be 1 through 60")
    try:
        import vcs_policy
        if options.installed:
            rows = vcs_policy.installed_rows()
        else:
            manifest = json.loads(options.manifest.read_text(encoding="utf-8"))
            rows = [{"package": item["package"],
                     "installed_version": item["installed_version"],
                     "pkgbuild": Path(item["pkgbuild"]).read_text(encoding="utf-8"),
                     "srcinfo": Path(item["srcinfo"]).read_text(encoding="utf-8"),
                     "gitmodules": (Path(item["gitmodules"]).read_text(encoding="utf-8")
                                    if item.get("gitmodules") else None)}
                    for item in manifest["packages"]]
        if options.inspect_gitmodules:
            cache = {}
            for row in rows:
                if row.get("gitmodules") is not None:
                    continue
                info = vcs_policy.classify(row["srcinfo"], row["pkgbuild"],
                                           row["package"], row["installed_version"])
                if len(info["suggested_monitor"]) != 1 or len(info["sources"]) < 2:
                    continue
                main = info["suggested_monitor"][0]
                source = next((x for x in info["sources"]
                               if x["alias"] == main and x["supported"]), None)
                if not source:
                    continue
                key = (source["url"], source["fragment"])
                if key not in cache:
                    cache[key] = vcs_policy.submodule_metadata.github_metadata_for_primary(*key)
                row["gitmodules"] = cache[key]
        policies = vcs_policy.package_policies(rows)
        approve = parse_approvals(options.reviewed_submodule)
        if not set(approve).issubset(policies):
            raise ValueError("Approved package is absent from installed package inventory")
        records = [assess_package(row, policies[row["package"]], approve=approve,
                                  query=lambda target: ls_remote(target, options.timeout))
                   for row in rows]
        result = {"schema": 1, "purpose": "research-remote-observation-only",
                  "created_at": datetime.now(timezone.utc).isoformat(),
                  "no_upgrade_decisions": True,
                  "packages": sorted(records, key=lambda r: r["package"])}
        text = json.dumps(result, indent=2, sort_keys=True) + "\n"
        if options.output:
            options.output.write_text(text, encoding="utf-8")
        else:
            print(text, end="")
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        print("vcs-probe: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())