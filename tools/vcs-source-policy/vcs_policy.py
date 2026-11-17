#!/usr/bin/env python3
"""Experimental, read-only-first VCS source policy discovery for libpamac.

Never source/execute PKGBUILDs and never download VCS application sources.
This tool does NOT decide upgrades or change libpamac's normal checker.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

SCHEMA = 1
VCS_SUFFIXES = ("-git", "-svn", "-hg", "-bzr", "-cvs", "-fossil", "-darcs")
NAME_RE = re.compile(r"^[A-Za-z0-9@._+\-]+$")
FUNCTION_RE = re.compile(r"(?m)^[ \t]*(?:function\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*(?:\(\s*\))?\s*\{")
GIT_SOURCE_RE = re.compile(r"^(?:git\+|git://)")
SHELL_INCLUDE_RE = re.compile(r"(?m)(?:^|[;&|]\s*|\n\s*)(?:source|\.)\s+[^\n;]+|\b(?:bash|sh)\s+[^\n;]+|\beval\s+")
SUBMODULE_URL_RE = re.compile(r"\bgit\s+config\s+submodule\.[^\s;]+\.url\s+(['\"]?)([^\s;\n'\"]+)\1")


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_srcinfo(text: str) -> tuple[str | None, list[str]]:
    packagebase = None
    sources: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or " = " not in line:
            continue
        key, value = line.split(" = ", 1)
        if key == "pkgbase":
            packagebase = value
        if key == "source" or key.startswith("source_"):
            sources.append(value)
    return packagebase, sources


def source_record(raw: str) -> dict | None:
    value = raw.strip()
    if "::" in value:
        alias, value = value.split("::", 1)
    else:
        alias = ""
    if not GIT_SOURCE_RE.match(value):
        return None
    # The fragment describes a makepkg checkout, not the Git transport URL.
    base, sep, fragment = value.partition("#")
    repo = base.removeprefix("git+")
    url_parts = urllib.parse.urlsplit(repo)
    if not url_parts.scheme or not url_parts.netloc or url_parts.username or url_parts.password:
        return {"alias": alias, "url": repo, "fragment": fragment, "supported": False}
    if not alias:
        alias = urllib.parse.unquote(url_parts.path.rstrip("/").split("/")[-1]).removesuffix(".git")
    return {"alias": alias, "url": repo, "fragment": fragment, "supported": bool(NAME_RE.fullmatch(alias))}


def parse_function_bodies(script: str) -> dict[str, str]:
    """Extract literal function bodies for hints; this is NOT a Bash interpreter.

    Unrecognized Bash constructs are never treated as proof of a safe skip.
    """
    funcs: dict[str, str] = {}
    for match in FUNCTION_RE.finditer(script):
        depth = 1
        position = match.end()
        start = position
        quote = ""
        escape = False
        while position < len(script) and depth:
            ch = script[position]
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif quote:
                if ch == quote:
                    quote = ""
            elif ch in "'\"":
                quote = ch
            elif ch == "#" and (position == 0 or script[position - 1].isspace()):
                newline = script.find("\n", position)
                position = len(script) if newline < 0 else newline
                continue
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            position += 1
        if depth == 0:
            funcs[match.group(1)] = script[start:position - 1]
    return funcs


def reachable_bodies(funcs: dict[str, str], root: str) -> tuple[str, list[str]]:
    seen: set[str] = set()
    missing: set[str] = set()

    def walk(name: str) -> str:
        if name in seen or name not in funcs:
            return ""
        seen.add(name)
        body = funcs[name]
        for child in funcs:
            if child != name and re.search(r"(?<![\w])" + re.escape(child) + r"(?:\s|;|$)", body):
                body += "\n" + walk(child)
        return body

    body = walk(root)
    # These constructs can call additional scripts/functions whose identities
    # a static heuristic cannot establish. Do not claim verified classification.
    for m in re.finditer(r"\b(?:source|\.|bash|sh|eval)\b[^\n;]*", body):
        if re.search(r"^(?:source|\.|bash|sh|eval)\s", m.group().strip()):
            missing.add("external or dynamic shell execution")
    return body, sorted(missing)


def path_reference(body: str, alias: str) -> bool:
    escaped = re.escape(alias)
    path = r"(?:\$\{srcdir\}|\$srcdir)/" + escaped + r"(?=['\"\s;/]|$)"
    # A literal cwd in pkgver() or `git -C` is convincing evidence of the
    # version-bearing repository; arbitrary alias mentions are NOT.
    return bool(re.search(r"\bcd\s+['\"]?" + path, body) or
                re.search(r"\bgit\s+-C\s+['\"]?" + path, body))


def srcdir_alias(expr: str) -> str | None:
    m = re.fullmatch(r"(?:\$\{srcdir\}|\$srcdir)/([A-Za-z0-9@._+\-]+)", expr)
    return m.group(1) if m else None


def classify(srcinfo: str, pkgbuild: str, installed_package: str, installed_version: str) -> dict:
    pkgbase, raw_sources = parse_srcinfo(srcinfo)
    sources = [entry for raw in raw_sources if (entry := source_record(raw)) is not None]
    reasons: list[str] = []
    aliases = [entry["alias"] for entry in sources]
    funcs = parse_function_bodies(pkgbuild)
    version_body, version_warnings = reachable_bodies(funcs, "pkgver")
    prepare_body, prepare_warnings = reachable_bodies(funcs, "prepare")
    referenced = [alias for alias in aliases if path_reference(version_body, alias)]
    supported = all(s["supported"] for s in sources) and len(set(aliases)) == len(aliases)
    if not supported:
        reasons.append("Git source URL or alias cannot be safely identified")
    if not sources:
        reasons.append("No Git sources declared in .SRCINFO")
    if "pkgver" not in funcs:
        reasons.append("No statically accessible pkgver() function")
    if version_warnings or prepare_warnings:
        reasons.append("Packaging functions delegate to other shell code")
    if len(referenced) != 1:
        reasons.append("Cannot unambiguously identify one version-bearing Git source")
    main = referenced[0] if len(referenced) == 1 else None
    submodules: list[str] = []
    # A positive hint only. `git config` in a helper or dead conditional is
    # not proof of execution, and a branch may deliberately float sources.
    if re.search(r"\bgit\s+submodule\s+(?:--\S+\s+)*update\b", prepare_body):
        for match in SUBMODULE_URL_RE.finditer(prepare_body):
            alias = srcdir_alias(match.group(2))
            if alias and alias in aliases and alias != main and alias not in submodules:
                submodules.append(alias)
    status = "inconclusive"
    suggested: list[str] = []
    if supported and main and not version_warnings and not prepare_warnings:
        if len(sources) == 1:
            status = "single-source"
            suggested = [main]
        elif len(submodules) == len(sources) - 1:
            status = "submodule-candidate"
            suggested = [main]
            reasons.append("Submodule roles inferred from shell text; human review required")
        else:
            reasons.append("Secondary Git sources are not conclusively mapped to submodules")
            suggested = [main]
    # No installed hash is inferred from the current remote HEAD or pkgver.
    return {
        "package": installed_package,
        "packagebase": pkgbase or installed_package,
        "installed_version": installed_version,
        "installed_revision": None,
        "installed_revision_provenance": "unknown",
        "pkgbuild_sha256": sha256(pkgbuild),
        "srcinfo_sha256": sha256(srcinfo),
        "classification": status,
        "sources": sources,
        "suggested_monitor": suggested,
        "identified_submodules": sorted(submodules),
        "monitor": [],
        "lightweight_check_enabled": False,
        "reasons": reasons,
    }


def apply_override(entry: dict, override: dict | None) -> None:
    if not override:
        return
    requested = override.get("monitor")
    if not isinstance(requested, list) or not requested:
        entry["reasons"].append("Ignored empty or malformed user override")
        return
    identities = {(s["alias"], s["url"]) for s in entry["sources"] if s["supported"]}
    selected: list[str] = []
    for record in requested:
        if not isinstance(record, dict) or (record.get("alias"), record.get("url")) not in identities:
            entry["reasons"].append("Stale override: source identity changed; review required")
            return
        if record["alias"] not in selected:
            selected.append(record["alias"])
    # A manual rule applies ONLY to exact source identities, never to guessed
    # new repositories. An unknown installed baseline still forbids skipping.
    entry["monitor"] = selected
    entry["classification"] = "user-policy"
    entry["reasons"].append("User selected monitored sources; installed baseline remains unknown")


def regenerate(rows: list[dict], overrides: dict | None = None) -> dict:
    policies: dict[str, dict] = {}
    overrides = (overrides or {}).get("packages", {})
    if not isinstance(overrides, dict):
        raise ValueError("Invalid overrides: packages must be an object")
    for row in rows:
        name = row["package"]
        if not NAME_RE.fullmatch(name) or not row.get("installed_version"):
            raise ValueError("Invalid package name/version")
        entry = classify(row["srcinfo"], row["pkgbuild"], name, row["installed_version"])
        key = entry["packagebase"]
        if key in policies and policies[key]["pkgbuild_sha256"] != entry["pkgbuild_sha256"]:
            raise ValueError(f"Conflicting PKGBUILDs for package base {key}")
        if key in policies:
            policies[key]["installed_packages"].append(name)
        else:
            entry["installed_packages"] = [name]
            # For packages we cannot unambiguously classify, honor only exact
            # user-reviewed source identities; preserve the override file.
            if entry["classification"] != "single-source":
                apply_override(entry, overrides.get(key))
            policies[key] = entry
    return {"schema": SCHEMA, "purpose": "classification-only", "packages": dict(sorted(policies.items()))}


def fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=15) as response:
        return json.loads(response.read(2_000_001))


def fetch_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=15) as response:
        data = response.read(2_000_001)
    if len(data) > 2_000_000:
        raise ValueError("Packaging metadata larger than 2 MB")
    return data.decode("utf-8")


def installed_rows() -> list[dict]:
    result = subprocess.run(["pacman", "-Q"], capture_output=True, text=True, check=True)
    packages = []
    for line in result.stdout.splitlines():
        if " " not in line:
            continue
        name, version = line.split(" ", 1)
        if name.endswith(VCS_SUFFIXES):
            packages.append((name, version.strip()))
    if not packages:
        return []
    query = urllib.parse.urlencode([("arg[]", name) for name, _ in packages])
    api = fetch_json("https://aur.archlinux.org/rpc/v5/info?" + query)
    by_name = {item["Name"]: item for item in api.get("results", [])}
    packaging_cache: dict[str, tuple[str, str]] = {}
    rows = []
    for name, version in packages:
        packagebase = by_name.get(name, {}).get("PackageBase")
        if not packagebase:
            # Not on AUR; avoid inventing metadata or calling a source a primary.
            rows.append({"package": name, "installed_version": version,
                         "srcinfo": f"pkgbase = {name}\n", "pkgbuild": ""})
            continue
        if not NAME_RE.fullmatch(packagebase):
            raise ValueError("Unexpected AUR package base")
        if packagebase not in packaging_cache:
            escaped = urllib.parse.quote(packagebase)
            path = f"https://aur.archlinux.org/cgit/aur.git/plain/"
            packaging_cache[packagebase] = (
                fetch_text(path + "PKGBUILD?h=" + escaped),
                fetch_text(path + ".SRCINFO?h=" + escaped))
        pkgbuild, srcinfo = packaging_cache[packagebase]
        rows.append({"package": name, "installed_version": version,
                     "pkgbuild": pkgbuild, "srcinfo": srcinfo})
    return rows


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=".vcs-sources-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    analyze_cmd = sub.add_parser("analyze", help="Analyze one local PKGBUILD/.SRCINFO without executing shell")
    analyze_cmd.add_argument("--pkgbuild", type=Path, required=True)
    analyze_cmd.add_argument("--srcinfo", type=Path, required=True)
    analyze_cmd.add_argument("--package", required=True)
    analyze_cmd.add_argument("--installed-version", default="unknown")
    regen = sub.add_parser("regenerate", help="Reconstruct classification database; never change package state")
    source = regen.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifest", type=Path, help="JSON input with locally saved packaging files")
    source.add_argument("--installed", action="store_true", help="Query pacman and fetch small AUR packaging files")
    regen.add_argument("--overrides", type=Path, default=Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "pamac" / "vcs-overrides.json")
    regen.add_argument("--output", type=Path, default=Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "pamac" / "vcs-sources.json")
    regen.add_argument("--dry-run", action="store_true", help="Print proposed output, do not write any files")
    args = parser.parse_args(argv)
    try:
        if args.command == "analyze":
            info = classify(args.srcinfo.read_text(), args.pkgbuild.read_text(), args.package, args.installed_version)
            print(json.dumps(info, indent=2, sort_keys=True))
        else:
            if args.installed:
                rows = installed_rows()
            else:
                manifest = json.loads(args.manifest.read_text())
                rows = [{"package": item["package"],
                         "installed_version": item["installed_version"],
                         "srcinfo": Path(item["srcinfo"]).read_text(),
                         "pkgbuild": Path(item["pkgbuild"]).read_text()}
                        for item in manifest["packages"]]
            overrides = json.loads(args.overrides.read_text()) if args.overrides.exists() else {}
            output = regenerate(rows, overrides)
            if args.dry_run:
                print(json.dumps(output, indent=2, sort_keys=True))
            else:
                atomic_write_json(args.output, output)
                print(f"Wrote {len(output['packages'])} package bases to {args.output}")
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        print(f"vcs-policy: {exc}; existing database left untouched", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
