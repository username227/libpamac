# Experimental VCS source policy — research only

This is a **non-integrated prototype** for libpamac's proposed lightweight
VCS update discovery. It does **not** replace `Database.refresh_vcs_sources`,
change the AUR transaction lock, decide whether a package needs upgrading,
execute a PKGBUILD, or download application source repositories.

## Why it exists

`src/database.vala:refresh_vcs_sources()` currently executes
`makepkg --nobuild --noprepare --nodeps --skipinteg` before regenerating
`.SRCINFO`. `--nobuild` still downloads/refreshes VCS sources. Repeating this
for development packages with dozens of submodules costs disk/network space.

Merely applying `git ls-remote` to **all** `source=()` Git repositories is
not acceptable: an independently advancing submodule must not cause a rebuild
when the primary repository has not advanced.

## Safe initial boundary

The prototype consumes `.SRCINFO` and the text of a PKGBUILD. It **does not**
source, execute or `eval` either. It identifies literal `pkgver()` Git
repository paths, traverses some in-file Bash helper-function references,
and looks for literal `git config submodule.<name>.url ...` and `git submodule
update` instructions. Results are **hints**, not a universal Bash interpreter.

A package with 25 recognized submodules is labeled `submodule-candidate`,
**not** trusted for automatic lightweight updates. An external sourced
PKGBUILD, dynamic alias, unrecognized helper, or conflicting source identity
is classified `inconclusive`. A package with one Git source and an explicit
literal `pkgver()` directory is labeled `single-source`.

**Every output currently has `lightweight_check_enabled: false` and
`installed_revision: null`.** Nothing is skipped even for `single-source`.
That is deliberate until a trustworthy installed-commit baseline and
transaction-aware checker exist.

## Inspect one PKGBUILD

```sh
python3 tools/vcs-source-policy/vcs_policy.py analyze \
  --pkgbuild /path/to/PKGBUILD --srcinfo /path/to/.SRCINFO \
  --package emulator-git --installed-version r100.abcdef-1
```

## Preview a complete fresh database from the local pacman inventory

```sh
python3 tools/vcs-source-policy/vcs_policy.py regenerate --installed --dry-run \
  | jq '.packages | to_entries[] | {package: .key, state: .value.classification, primary: .value.suggested_monitor, reasons: .value.reasons}'
```

`--installed` uses **read-only** `pacman -Q`, selects names with recognized
VCS suffixes and fetches only small AUR RPC metadata, `PKGBUILD` and
`.SRCINFO` files. It does *not* call `makepkg`, clone a Git source, run
`git ls-remote`, install packages or write anything under `--dry-run`.
Packages absent from AUR are retained as inconclusive; failures retrieving
metadata abort regeneration without overwriting the previous file.
Packages using VCS sources but lacking conventional VCS suffixes aren't yet
discovered; this is an explicit limitation of the prototype.

To write the entire **generated** database atomically:

```sh
python3 tools/vcs-source-policy/vcs_policy.py regenerate --installed
```

Default output is `$XDG_DATA_HOME/pamac/vcs-sources.json`, normally
`~/.local/share/pamac/vcs-sources.json`. The generated file is replaced from
scratch on every successful regeneration. It never uses prior generated
classifications as the source of truth.

A fully offline fixture manifest is also supported:

```json
{
  "packages": [
    {
      "package": "emulator-git",
      "installed_version": "r100.abcdef-1",
      "pkgbuild": "/path/to/PKGBUILD",
      "srcinfo": "/path/to/.SRCINFO"
    }
  ]
}
```

Run `regenerate --manifest /path/to/manifest.json --dry-run`.

## User-reviewed overrides

User edits belong in `$XDG_CONFIG_HOME/pamac/vcs-overrides.json` (normally
`~/.config/pamac/vcs-overrides.json`), **not** in the disposable generated
file. Example:

```json
{
  "schema": 1,
  "packages": {
    "emulator-git": {
      "monitor": [
        {"alias": "emulator", "url": "https://example.org/emulator.git"}
      ]
    }
  }
}
```

The prototype applies overrides only to classifications it could not
independently reduce to a single source. Alias, URL and optional Git ref fragment must all match the
*currently retrieved* `.SRCINFO` entry. Stale identities are rejected rather
than silently reassigned. User configuration is never overwritten by
regeneration. An override does not manufacture a known installed Git commit,
or turn on lightweight checks yet.

## Testing

```sh
python3 -m unittest discover -s tools/vcs-source-policy/tests -v
```

The fixtures include 25 submodules represented through an in-PKGBUILD helper
function, an externally sourced PKGBUILD, two independent Git sources,
changed source identities, stale-package removal on regeneration, split
packages, unknown installed baseline, changed packaging instructions and a
hostile command substitution that must not execute.

## Work remaining before libpamac integration

1. **Adversarial real-world classifier corpus.** Test complicated AUR
   PKGBUILDs, sourced secondary PKGBUILDs, nested submodule configuration,
   conditional Git references, indirect functions and architecture-specific
   `source_*` arrays. Prefer false `inconclusive` over falsely ignoring a
   source. Static hints are never proof of arbitrary Bash semantics.
2. **Full packaging provenance.** Independently track the AUR packagebase
   revision and relevant packaging files, not only raw PKGBUILD text.
   `.SRCINFO` can be stale. A changed build instruction or dependency must
   trigger a conservative re-evaluation, even without a main-Git change.
3. **Installed-revision provenance.** Identify a last-successful installed
   primary commit from trustworthy build records where available. An
   arbitrary manual, yay, paru or makepkg installation cannot be assumed
   to equal the remote HEAD during a `--gendb`-style initialization.
   Unknown must remain unknown until explicit reconciliation or conservative
   checking; never silently reset the baseline after regeneration.
4. **Reference-aware probe.** For confirmed primary refs, use a bounded,
   sanitized `git ls-remote` check, respecting PKGBUILD branch/tag/commit
   fragments, remote failures, force pushes and Git's URL schemes. Do not
   check submodule HEADs independently. For unclassifiable packages, retain
   Pamac's existing complete checker.
5. **Atomic state and transaction lifecycle.** Keep user overrides,
   generated classification and installed revision state separate. Commit a
   baseline only after verified successful installation. Preserve existing
   libpamac advisory locks and coordinator semantics; avoid partial GUI or
   CLI update lists. Add A/B integration tests against original libpamac.
6. **CLI visibility, no GTK settings.** Add explicit dry-run/regeneration and
   explanation commands in CLI/man documentation. Do not add GUI controls.

No existing production files were modified for this prototype.
