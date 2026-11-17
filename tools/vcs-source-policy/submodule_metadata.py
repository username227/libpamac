"""Metadata-only, fail-closed Git submodule cross-check for VCS research.

No source clones, no shell execution, and no automatic update decisions.
Matching current upstream .gitmodules is *advisory*, not proof that the
PKGBUILD respects pinned gitlink commits.
"""
from __future__ import annotations

import base64
import configparser
import json
import re
import urllib.error
import urllib.parse
import urllib.request


SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9._-]+$")


def normalized_https_repo(url: str) -> tuple[str, str] | None:
    """Compare URLs exactly except optional trailing .git and host case."""
    try:
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or
                parsed.username or parsed.password or
                parsed.port not in (None, 443) or
                parsed.query or parsed.fragment):
            return None
        path = parsed.path.rstrip("/").removesuffix(".git")
        if not path or path == "/" or ".." in path.split("/"):
            return None
        return parsed.hostname.lower(), path
    except ValueError:
        return None


def gitmodules_urls(data: str) -> list[str]:
    """Parse literal .gitmodules URLs without invoking git config."""
    if len(data.encode("utf-8")) > 131_072:
        raise ValueError("Too much .gitmodules metadata")
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.read_string(data)
    urls: list[str] = []
    for section in parser.sections():
        if section.startswith('submodule "') and section.endswith('"'):
            if parser.has_option(section, "url"):
                urls.append(parser.get(section, "url"))
    return urls


def find_submodule_matches(primary_alias: str, sources: list[dict],
                           gitmodules: str) -> set[str]:
    """Only secondary sources whose exact URLs match .gitmodules are candidates."""
    urls = {identity for url in gitmodules_urls(gitmodules)
            if (identity := normalized_https_repo(url)) is not None}
    return {source["alias"] for source in sources
            if source.get("supported") and source["alias"] != primary_alias and
            normalized_https_repo(source["url"]) in urls}


def github_metadata_for_primary(url: str, fragment: str = "") -> str | None:
    """Fetch only a small .gitmodules file from GitHub's contents API.

    The current remote ref is NOT necessarily the installed commit. This
    function provides hints only; it never authorizes skipping a source.
    """
    identity = normalized_https_repo(url)
    if not identity or identity[0] != "github.com":
        return None
    parts = identity[1].strip("/").split("/")
    if len(parts) != 2 or not all(SAFE_COMPONENT.fullmatch(p) for p in parts):
        return None
    owner, project = parts
    endpoint = ("https://api.github.com/repos/" +
                urllib.parse.quote(owner, safe="") + "/" +
                urllib.parse.quote(project, safe="") + "/contents/.gitmodules")
    if fragment:
        key, separator, ref = fragment.partition("=")
        if separator != "=" or key not in ("branch", "tag", "commit") or not ref:
            return None
        endpoint += "?" + urllib.parse.urlencode({"ref": ref})
    request = urllib.request.Request(
        endpoint, headers={"User-Agent": "pamac-fix-vcs-research/1.0",
                           "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            document_bytes = response.read(262_145)
        if len(document_bytes) > 262_144:
            return None
        document = json.loads(document_bytes)
        if document.get("encoding") != "base64":
            return None
        encoded_content = document["content"]
        if not isinstance(encoded_content, str):
            return None
        # GitHub's contents API wraps base64 in newline-separated chunks.
        raw = base64.b64decode("".join(encoded_content.split()), validate=True)
        if len(raw) > 131_072:
            return None
        data = raw.decode("utf-8")
        gitmodules_urls(data)
        return data
    except (OSError, ValueError, KeyError, UnicodeError, configparser.Error,
            base64.binascii.Error):
        return None
