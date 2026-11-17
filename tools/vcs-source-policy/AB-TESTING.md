# Legacy Pamac versus lightweight VCS checker — A/B validation

**Research-only harness.** `ab_compare.py` compares two previously captured
JSON snapshots. It does **not** execute either checker, query Git, write to
Pamac state, touch AUR directories, build or install packages. Neither the
legacy capture adapter nor the new lightweight checker is implemented yet.

## Why compare both

The current Pamac VCS path calls `makepkg --nobuild --noprepare --nodeps
--skipinteg` while holding its AUR source lock, potentially downloading
very large application repositories and submodules. The proposed replacement
will check designated primary Git revisions without downloading sources.

**Never casually run two complete source-refresh scans**, especially on a
machine with tens of gigabytes of VCS sources. Existing legacy results may be
captured from normal user-triggered checks; otherwise, use isolated fixtures
with explicit disk/network limits. Production Pamac logic remains untouched.

## Test cases required before integration

1. Primary remote Git commit advances: both methods should detect the update
   and should agree on the candidate version where derivable.
2. 25+ independent submodules advance, primary commit unchanged: no new
   lightweight update should appear. If the old checker behaves differently,
   record a *reviewed difference*, not an automatic pass.
3. Parent commit advances and changes Git submodule pointers: detect the
   parent change without checking submodule remote HEADs independently.
4. AUR PKGBUILD/patch/dependency changes with upstream Git unchanged:
   packaging change must still be detected by the new design.
5. Fixed Git tag/commit: remote HEAD movement must not cause a rebuild.
6. Split package where only one of twenty members is installed: only that
   installed member is compared; never install the other nineteen.
7. Packages using external PKGBUILDs, sourced scripts, dynamic Bash,
   architecture-qualified sources: mark ambiguous cases as incomplete or
   fall back to the existing conservative checker.
8. External helper reinstall/upgrade between checks: invalidate the baseline.
9. Remote revision changes during A/B capture or force-push: results are
   inconclusive unless both methods are proven to use the same remote state.
10. Lock contention and interrupted scans: no partial results should count as
    current, and no package transaction may run concurrently.

## Captured snapshot format

Each adapter must emit one capture with:

    {
      "schema": 1,
      "method": "legacy",
      "state": {
        "installed_state_sha256": "<actual 64-hex digest>",
        "packaging_state_sha256": "<actual 64-hex digest>",
        "remote_state_sha256": "<actual 64-hex digest>",
        "configuration_sha256": "<actual 64-hex digest>"
      },
      "packages": [
        {
          "package": "emulator-git",
          "packagebase": "emulator",
          "installed_version": "r100.abc-1",
          "status": "update",
          "candidate_version": "r101.def-1"
        }
      ]
    }

The second file uses `"method": "lightweight"`. Accepted statuses:
`update`, `current`, `incomplete`, `error`, `deferred`.

**Those digests must describe the real observed states.** They are *not*
user-generated run IDs, placeholders, or hashes copied from one capture to
make the tests pass. In particular, remote-state equivalence must be grounded
in remote references actually observed for the relevant package sources.
The capture adapters must eventually document the canonical serialization and
retrieval of these fingerprints. Until then, *real* A/B measurements cannot
be declared reliable simply by submitting hand-authored digests.

## Compare saved captures (safe: no source downloads)

    python3 tools/vcs-source-policy/ab_compare.py \
        --legacy /tmp/pamac-legacy.json \
        --lightweight /tmp/pamac-lightweight.json \
        --output /tmp/pamac-vcs-comparison.json

Exit codes: `0` if all packages match with complete, comparable state;
`2` if differences or incomplete evidence exist; `1` for malformed input or
I/O errors. The JSON report records per-package results:

- `match-current` / `match-update`: direct matches with complete evidence.
- `potential-missed-upgrade`: legacy says update, new says current — **block**.
- `potential-extra-rebuild`: new says update, legacy says current — investigate.
- `version-mismatch`: both say update but the candidate versions differ.
- `inconclusive`: errors, missing split members, drift in scan states, or
  missing update data. Never considered a pass.

The legacy checker is a *comparison reference*, not an infallible oracle. A
submodule-only difference might show exactly why we're changing Pamac, but
must be reviewed and documented. Do not turn discrepancies into automatic
exceptions or deploy the lightweight checker based on a partially matching
comparison.

## Further work

- Implement **read-only, non-mutating capture adapters** for both methods.
- Capture truly comparable frozen/observed source and packaging state.
- Compare results over a representative real AUR corpus and multiple runs.
- Add disk/network/time telemetry with explicit opt-in caps and source-cache
  isolation. Do not cause a 30 GB download merely to benchmark a checker.
- Integrate lightweight decision-making only after installed revision
  provenance, policy validation, and transaction-lock tests pass.

Run the isolated comparison-harness tests:

    python3 -m unittest discover -s tools/vcs-source-policy/tests -v