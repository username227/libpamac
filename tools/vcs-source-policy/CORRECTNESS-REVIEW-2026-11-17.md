# Pamac VCS correctness review — 2026-11-17

**Scope:** Research branch only; no production Pamac changes, no source clones, builds or installs. This is NOT approval to enable the lightweight update checker.

## Evidence and limits

- Frozen AUR sample: 600 randomly chosen development-suffix package names in three disjoint rounds, plus 50 unsuffixed controls. All 650 produced results; no collection errors.
- Original classifications: 407 single-source, 4 submodule-metadata candidates and 189 inconclusive among the 600 development entries.
- Revised replay: 556 single-source (92.7%), 4 submodule-metadata candidates, 2 fixed-source and 38 inconclusive among the 600. Of the 50 controls: 1 single-source, 5 fixed-source, 44 inconclusive.
- The revised replay reported 423 pkgver source-path confirmations among all 650; 422 are in the development sample. It authorized ZERO automatic lightweight checks and declared ground_truth_validated=false.
- Across development entries, 195 still require review; the remaining 405 merely have no currently flagged *static source-review* reason. The latter number does not imply their upgrade decisions are safe.
- **92.7% is source-identity coverage of the sampled VCS-suffix population, NOT 92.7% of all AUR packages, and NOT proven safe upgrade detection.**
- Counts are grounded in the user-supplied original results.json and revised replay summary. The PKGBUILD examples below were cross-checked against the present archlinux/aur GitHub mirror branches; archived PKGBUILD SHA256 equivalence to those live mirror files has not been established. Confirm against the frozen on-disk archive before final case certification.

## The 38 development packages still inconclusive

**21 without a recognized Git source** (many correctly use other VCS systems or have unusual packaging): freecalypso-tools-hg, wayra-git, cwiid-git, dcli-arch-git, olc-git, singlepage-git, emas-git, ggnfs-lasieve4e-x86_64-svn, simutrans-svn, renew-svn, dosbox-openglide-svn, nkk-fossil, wwwoffle-svn, graph-plotter-hg, jfederc-git, fuck-netease-music-git, gmp-hg, cppsplash-git, acct-svn, gpxlab-git, protonmail-bridge-free-bin-git.

**16 with multiple Git sources not proven interchangeable/pinned:** freetype2-git, shiro-plugins-git, darling-system-git, excalibar-git, lunarvim-git, edk2-ovmf-git, freelib-git, palm-novacom-git, ez2lazer-git, uuway-git, mcpelauncher-ui-git, ayugram-desktop-git, pytype-git, sparrow-wallet-git, mariadbpp-git, noaidarling-cli-extra-git.

**1 sole Git source with unsupported transport:** devt-git uses git+ssh://git@github.com/..., which the deliberately noninteractive HTTPS/Git probe cannot safely authenticate. It correctly remains inconclusive.

Some no-Git cases contain independently executed VCS operations *outside declared sources*: cwiid-git runs git clone/git pull inside build(); singlepage-git runs go get in pkgver()/build(). These MUST NOT be inferred from a '-git' suffix or treated as source-free.

## Independent Git inputs and critical submodule distinction

| Package | Current AUR PKGBUILD observation | Required interpretation |
|---|---|---|
| freetype2-git | Builds from freetype and freetype-demos; the latter is the Meson build root | Two independently moving sources; parent HEAD alone insufficient |
| palm-novacom-git | Builds novacom client and novacomd server from separate Git checkouts; pkgver() executes date | BOTH repositories matter; calendar version also breaks HEAD-only logic |
| edk2-ovmf-git | Symlinks multiple separately fetched Git checkouts into the EDK2 source tree, despite related entries in upstream .gitmodules | A .gitmodules URL match CANNOT alone authorize N |
| lunarvim-git | One moving Git source plus a second fixed #tag= source | Moving parent may be monitored, but changed packaging tags must still trigger reevaluation |
| uuway-git | One moving Git source and eight fixed #commit= inputs | Do not poll pinned inputs as branches; detect any PKGBUILD pin change |
| mcpelauncher-ui-git | Uses git submodule update on explicitly supplied sources; upstream .gitmodules uses relative URLs | Relative URL matching is a candidate-discovery improvement, not N proof |
| shiro-plugins-git | Uses git submodule update; upstream .gitmodules has git://github.com/DISTRHO/DPF, versus HTTPS AUR source | Transport alias is resolvable for human review, but not automatically safe |
| sparrow-wallet-git | Uses git submodule update; upstream .gitmodules uses relative ../../sparrowwallet URLs | Same: candidate only pending verified pinning |
| pytype-git | Multiple Git sources and submodule update; upstream .gitmodules has HTTPS URL matches | Review exact prepare()/gitlink behavior, not just URL names |
| darling-system-git / noaidarling-cli-extra-git | 154 and 153 Git sources in the respective audit packages | Keep conservative fallback until each *effective* independent input is understood |
| mariadbpp-git | Has a second Git source, submodule update, and pkgver() hardcoded to 1.0.0 | A remote SHA difference may not map to a version change |

Four packages in the audit proposed six advisory N flags: gibsonclient-git (1), hugetracker-git (3), ticemu-git (1), xmonk.lv2-git (1). NONE is approved for real skip decisions. Their PKGBUILD behavior and parent gitlinks must be checked against frozen packaging snapshots.

## Reproducible spot-check of 30 single-Git entries

Using the original results.json order, Python random.Random(20261117), and sequential random.sample(..., 15) first from the old confident single-moving group, then from the old inconclusive single-moving group:

- **Original confident cohort (15):** gucken-git, rshijack-git, opengamepadui-git, xcorners-git, ccsh-compiler-git, namebind-git, bcm2-utils-git, hydownloader-systray-git, iease-music-daihy-electron-git, cinnamon-git, blas-openblas-git, dracula-alacritty-git, bear-git, qdre-compositor-git, libaacs-git.
- **Previously inconclusive cohort (15):** gigabyte-laptop-wmi-dkms-git, schemes-git, minus_games_server-git, wmderland-git, musiqt-git, devt-git, bash-ctypes-git, gofile-downloader-git, emacs-ess-git, relive-git, kyocera-ppd-git, nay-git, app2unit-git, waybar-module-sysmon-git, ubase-git.

Important checks against current mirror:

- qdre-compositor-git: pkgver() uses git rev-list --all --count. A non-HEAD ref can change its version with unchanged HEAD.
- blas-openblas-git: pkgver() chooses the latest Git version tag (git tag -l --sort -v:refname); tags are extra mutable inputs beyond HEAD.
- Many otherwise confident Git packages use git describe --tags; the tag ref set/placements may change without HEAD. A HEAD-only equivalence condition is therefore not universally sufficient.
- namebind-git: Git source includes a ?signed URL query. vcs_policy.source_record() identifies it, whereas vcs_probe.source_target() intentionally rejects URL queries. This is a known *identity vs probe eligibility* mismatch, not a safe candidate to enable.
- hydownloader-systray-git and relive-git: git submodule update is executed during build(); such checkout behavior needs accounting even with only one explicit Git source.
- minus_games_server-git, wmderland-git and kyocera-ppd-git: no pkgver() function was found in mirror snapshot; unique Git identity alone does not establish version dynamics.
- devt-git: SSH credential-based source correctly not supported by the noninteractive probe.
- The 30 were *spot-checked for structural counterexamples*, not independently certified for actual update decisions. No false-negative rate can be computed from this review.

## Requirements before any no-update decision can be trusted

1. **Installed provenance:** capture the full commit SHA and selected ref at actual build/install time; bind to package name/version/pkgrel, PKGBUILD and .SRCINFO hashes, source identities and any effective submodule gitlinks. Store tiny metadata, NOT source trees. Existing packages without trustworthy provenance use legacy fallback.
2. **Packaging drift:** independently refresh and compare complete packaging metadata, including pins, patches, build inputs and architecture-specific sources. A changed PKGBUILD or .SRCINFO requires conservative processing even if HEAD is unchanged.
3. **Complete remote inputs:** source dependencies can include multiple independent branches, mutable tags, `git rev-list --all`, calendar/clock state, network commands embedded in PKGBUILD, or non-Git VCS. A known primary Git source does not establish a closed set of moving inputs.
4. **Submodule proof:** check the *actual parent gitlink revisions* and exact prepare()/build() behavior; reject --remote updates or independently symlinked/overridden checkouts. A .gitmodules URL match alone is only a candidate hint.
5. **Fail closed:** unknown provenance, inaccessible remote, invalid selectors, stale hashes, incompatible URL schemes, or unsupported semantics must not silently count as current.
6. **Strict differential A/B:** compare upgrade/no-upgrade decisions and candidate version outcomes against legacy Pamac under equivalent installed/packaging/remote state. Treat any verified missed upgrade as a blocker.

**Next research implementation:** classify HEAD-only vs tag-sensitive/all-ref/time-sensitive pkgver semantics separately, audit actual transport compatibility, and design a minimal build-time provenance manifest. Preserve `lightweight_check_enabled=false` until strict differential validation succeeds.
