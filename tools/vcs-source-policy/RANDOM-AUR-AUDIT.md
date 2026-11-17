# Randomized AUR VCS source-classification audit (pre-merge gate)

**Research only.** No arbitrary PKGBUILDs are executed or sourced; no `makepkg`,
`pacman`, Pamac, Git source clone/fetch, build, install, or package upgrade occurs.
The script downloads only the AUR compressed package index, AUR package metadata
(RPC), and AUR *packaging* snapshots. An optional, capped GitHub `.gitmodules`
request only retrieves a small text file. No Pamac production code is modified.

## Audit design

Use a frozen AUR `packages.gz` index to sample **three non-overlapping random
sets of 200 development-suffix package names** (600 distinct package names),
plus **50 random unsuffixed controls**. The population is the entire current
AUR package-name index, filtered by conventional `-git`, `-svn`, `-hg`, `-bzr`,
`-cvs`, `-fossil`, and `-darcs` suffixes. Every selected name has equal
probability; samples are drawn without replacement using a saved RNG seed.
The suffix is a *sampling-frame criterion*, **not proof of VCS semantics**.
Some genuinely moving VCS packages lack a suffix; they are excluded from the
primary random sample. The unsuffixed controls help reveal that limitation,
but are not an exhaustive survey of atypical packages.

AUR RPC resolves split package members to their shared packagebase. The scanner
then reads PKGBUILD and `.SRCINFO` from one metadata-only AUR snapshot. It
never executes shell. For each sampled package it records a result or an
explicit missing-RPC/fetch/classification error; **no missing or difficult
package is silently replaced**. It caches snapshots by packagebase and checks
that the selected split member is declared in `.SRCINFO`.

The audit saves the exact sample manifest, index archive and its SHA256,
packaging texts, text hashes, snapshot hashes, individual rows and per-round
summaries. This permits auditing selection bias and rerunning the exact same
packaging texts after future parser changes, without network or AUR drift.

## First live audit: three rounds of 200

From the `research/vcs-source-policy` clone:

```bash
python3 -m unittest discover -s tools/vcs-source-policy/tests -v

python3 tools/vcs-source-policy/aur_random_audit.py \
  --rounds 3 --sample-size 200 --negative-controls 50 \
  --seed 20261117 --delay 0.3 \
  --output-dir /tmp/pamac-vcs-audit-20261117 \
  > /tmp/pamac-vcs-audit-summary.json
```

The output directory must be empty/new. A missing AUR package, inaccessible
snapshot, malformed `.SRCINFO`, or failed fetch is retained as an error and
will cause exit status `2`. A fatal issue (e.g. AUR package index cannot be
retrieved) uses exit status `1`. Exit status `0` means the metadata collection
finished without fetch/classification errors, **not** that the new checker is
safe, accurate, or ready to merge.

For an optional second pass using frozen source texts and a **bounded**
GitHub `.gitmodules` inspection, run a **new** audit with
`--inspect-gitmodules --max-gitmodules 40` from the start (40 is a hard cap on
additional current-source metadata requests, not on AUR snapshots). All
resulting `N` proposals remain unapproved and `lightweight_check_enabled`
remains false. The current-branch `.gitmodules` is not guaranteed to describe
the installed package; a URL match does not prove pinned gitlink behavior.

After a parser change, replay without contacting either AUR or GitHub:

```bash
python3 tools/vcs-source-policy/aur_random_audit.py \
  --replay /tmp/pamac-vcs-audit-20261117 \
  --output-dir /tmp/pamac-vcs-audit-replay
```

To rerun with a fresh random selection, choose a **new seed and output dir**.
Rerunning with the same frozen packaging and the same code should produce the
same package classifications (timestamps will differ).

Review only the summary on screen; full observations are retained in:

- `results.json`: complete per-package outcomes and per-round totals
- `results.csv`: short tabular classifications
- `sample.json`: exact selected sample names and seed
- `packages.gz`: the exact AUR sampling population at selection time
- `packagebases.json`: AUR RPC split-package mappings
- `packaging/<packagebase>/PKGBUILD`, `.SRCINFO`, `text-hashes.json`,
  `archive.sha256` and optionally `.gitmodules`

## Interpreting the results

**Coverage is not accuracy.** Count `single-source`, `inconclusive`,
`submodule-candidate`, any `submodule-metadata-candidate`, unknown Git
fragments, non-Git VCS sources, pinned tags/commits, and every proposed `N`.
No AUR package in this study has an observed installed revision, so there are
**no real upgrade decisions** and no legitimate false-negative rate yet.
Classifications must not be labeled true/false without independent review.

Also group failures by packagebase, type of VCS, complexity (single/multi
source), split packages, and static source fragments. Treat unavailable
metadata as **inconclusive**, never as a successful `current` result.

## Replay the same 650 archived AUR packages after classifier fixes

The existing audit directory preserves `sample.json` and
`packaging/<packagebase>/PKGBUILD`, `.SRCINFO`, and retrieved
`.gitmodules` files. Reprocess these **exact same source texts** offline:

    python3 tools/vcs-source-policy/aur_random_audit.py \
      --replay /tmp/pamac-vcs-audit-20261117 \
      --inspect-gitmodules \
      --output-dir /tmp/pamac-vcs-audit-20261117-replay-v2 \
      > /tmp/pamac-vcs-audit-20261117-replay-v2-summary.json

The revised results include `source_identification_method` and
`pkgver_source_reference_confirmed`. An identifiable unique Git source
is **not** the same as verified version logic or a permitted update check.
Fixed `#commit=` and `#tag=` sources are reported separately as
`fixed-source`. No remote source checkout, package build, or installation
is performed by replay.

## Required evidence before considering a merge

1. **Broad coverage:** run at least three distinct 200-package rounds, audit
   the entire list of errors, include non-Git VCS and pinned-reference cases,
   and repeat later with a different index/seed. If many selections fail to
   fetch, fix collection rather than excluding them.
2. **Manual ground-truth review:** inspect **every proposed `N` exclusion**,
   especially `prepare()` logic, `git submodule update --remote`, nested
   scripts, and the parent repository's gitlink pins. Randomly inspect
   apparently confident `single-source` results and all suspicious cases.
   Record independent pass/fail/uncertain judgments and correction reasons.
3. **Fail-closed invariants:** uncertain relationships, unknown installed
   revisions, stale policies, changed PKGBUILDs, missing sources, or changed
   provenance **must fall back** to Pamac's established checker.
4. **Live differential A/B:** separately compare the actual legacy and new
   upgrade *decisions* under matching installed, packaging and remote states,
   with candidate versions and all incomplete/failed scans accounted for.
   No check should initiate 10–30 GiB downloads just to produce a benchmark.
5. **No missed upgrades:** any *verified* false negative blocks the merge.
   Extra rebuilds and version disagreements also require investigation.
   Passing a random audit cannot prove zero bugs; exact safeguards and a
   conservative fallback remain essential.

**Do not merge this branch because it classifies a large percentage of cases.**
The principal question is whether the cases it claims to understand are
correctly classified and whether it *always* refuses to skip on uncertainty.