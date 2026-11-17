# Experimental VCS source policies — research only

This is a non-integrated prototype. It does NOT change Pamac's production
update checker, transaction safety, package selection, or installation logic.
It never executes PKGBUILDs or clones application source repositories.

## Purpose

Pamac currently executes makepkg --nobuild --noprepare --nodeps --skipinteg
when refreshing VCS packages. This can download enormous Git repositories
and submodules before Pamac knows whether an upgrade is warranted.

The eventual goal is a lightweight remote revision check of only the sources
explicitly permitted to trigger rebuilds. Independent advances of a
submodule must NOT trigger rebuilding its parent.

## One editable JSON per installed development package

The default output directory is:

    ~/.local/share/pamac/vcs-sources/

For example, an installed package emulator-gui-git produces
emulator-gui-git.json, even if its package base emulator-suite creates twenty
split packages. If the other nineteen are not installed, none gets a JSON.

The JSON retains the shared packagebase, the installed package name and
version, the PKGBUILD and .SRCINFO hashes, every declared source, and a
simple "rebuild": "Y" or "rebuild": "N" value. This is editable for
EVERY development package, including confidently classified packages.

Example excerpt:

    {
      "schema": 2,
      "package": "emulator-gui-git",
      "packagebase": "emulator-suite",
      "sources": [
        {
          "source": "emulator::git+https://example.org/emulator.git",
          "role": "primary-candidate",
          "rebuild": "Y"
        },
        {
          "source": "audio::git+https://example.org/audio.git",
          "role": "submodule-candidate",
          "rebuild": "N"
        }
      ]
    }

Actual files include identity hashes, classification reasons and explicit
unknown installed-revision status. All automatic lightweight checks remain
DISABLED in this prototype. Suspected submodules initially get N; uncertain
Git sources get Y. These remain hints until validated against real packages.

## Regeneration: keep, overwrite, or back up first

The CLI presents these explicit modes via --existing:

    # Preview, with no writes:
    python3 tools/vcs-source-policy/vcs_policy.py regenerate --installed --dry-run

    # Keep existing JSON byte-for-byte (default):
    python3 tools/vcs-source-policy/vcs_policy.py regenerate --installed --existing keep

    # Replace all current installed package JSON policies:
    python3 tools/vcs-source-policy/vcs_policy.py regenerate --installed --existing overwrite

    # Back up each existing JSON beside itself, then replace:
    python3 tools/vcs-source-policy/vcs_policy.py regenerate --installed --existing backup

A backup filename looks like:

    emulator-gui-git.json.bak-20261116T220000000000Z

Backups stay in the SAME folder, with collision avoidance, and are not
active policy JSONs. We never silently delete them. We create new package
files in all three modes.

In keep mode, every retained JSON is compared against current source
identities and PKGBUILD/.SRCINFO hashes. When sources change or metadata
drifts, output marks the file keep-stale. Its content is left alone and a
warning appears; a future lightweight checker MUST refuse to trust that
stale policy until reconciled. User Y/N choices are never reset implicitly.

Atomic replacement applies to each individual JSON, not the entire folder
as a transaction. Existing unrelated files are untouched. The prototype
refuses symlinked target JSON files. Obsolete JSONs for previously installed
packages remain untouched but must NOT be treated as installed packages:
current pacman inventory is authoritative.

## Optional Git submodule metadata cross-check

The new research-only `--inspect-gitmodules` option fetches a **small**
`.gitmodules` file using GitHub's public contents API for a recognized
primary GitHub source in a multi-Git PKGBUILD. It neither clones application
sources nor runs packaging scripts. Source URLs are matched to exact HTTPS
host/path identities, allowing only the conventional trailing `.git` difference.

    python3 tools/vcs-source-policy/vcs_policy.py regenerate \
      --installed --inspect-gitmodules --dry-run \
      > /tmp/pamac-vcs-submodule-preview.json

Cross-reference matches receive role `submodule-candidate` and a proposed
`"rebuild": "N"`. **That N is not approved for use**: the package gets
`review_required: true` and `lightweight_check_enabled: false`.
The remote file may be newer than the installed parent's commit. Moreover,
a PKGBUILD might use `git submodule update --remote`, explicitly overriding
the parent's pinned submodule revision. Before we can activate N, we must
inspect the actual PKGBUILD's `prepare()` and confirm that it honors parent
gitlink pins. A matching URL alone proves only submodule *identity*.

If metadata is missing, access fails or the primary isn't hosted on GitHub,
classification remains conservative and the script does not assume an N.
An optional offline fixture manifest can specify a `"gitmodules"` path
alongside `"pkgbuild"` and `"srcinfo"`. Changes in inspected `.gitmodules`
content invalidate existing kept-file fingerprints.

## Analysis and testing

    python3 tools/vcs-source-policy/vcs_policy.py analyze \
      --pkgbuild /path/to/PKGBUILD --srcinfo /path/to/.SRCINFO \
      --package emulator-gui-git --installed-version r100.abcdef-1

    python3 -m unittest discover -s tools/vcs-source-policy/tests -v

The --installed mode reads pacman -Q and fetches small AUR packaging metadata.
It does NOT download the application sources, run makepkg, or install anything.

A fully offline fixture manifest can replace --installed:

    {
      "packages": [
        {
          "package": "emulator-gui-git",
          "installed_version": "r100.abcdef-1",
          "pkgbuild": "/path/to/PKGBUILD",
          "srcinfo": "/path/to/.SRCINFO"
        }
      ]
    }

Run regenerate --manifest /path/to/manifest.json --dry-run.

## Current classifier limitations

The prototype statically identifies some literal pkgver() directory
references and in-file helper function calls, and recognizes some git
submodule configuration in prepare(). It does NOT interpret arbitrary
Bash code. PKGBUILDs that call external PKGBUILDs or scripts are inconclusive.
This is essential: confident guesses about dynamic scripts would be unsafe.

Installed packages without standard VCS suffixes are not yet discovered,
and known current installed Git revisions are not yet established. A
database regeneration MUST NOT assume that upstream HEAD equals the revision
the user installed using Pamac, paru, yay, makepkg, or another tool.

## Requirements before any production integration

1. Build a broad real-world, adversarial test corpus with complicated
   PKGBUILDs and submodules, and confirm split-package membership.
2. Record trustworthy installed Git revisions and AUR packaging provenance.
3. Add bounded git ls-remote checks for monitored sources, respecting refs
   and branches without checking excluded submodule HEADs.
4. Independently detect packaging changes and stale .SRCINFO.
5. Revalidate policies on every use, including manually edited ones. If a
   source is unresolved, use Pamac's existing conservative checker.
6. Preserve cross-process AUR locks and all existing transaction safeguards.
7. Expose configuration by CLI and documentation, NOT by GTK controls.

No production libpamac files or transaction paths were modified.
