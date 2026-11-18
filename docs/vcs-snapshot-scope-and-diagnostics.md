# VCS-only fingerprints and diagnostic reuse reasons

A full-screen GTK check retains a verified VCS source snapshot for two minutes.
Transactions may reuse source work when the selected VCS names are a subset
of the checked set and the source fingerprints match.

Previously `Database.vcs_source_stamp()` included all installed packages,
all IgnorePkg entries, and the mtime of the entire pacman local package
database. An unrelated repository upgrade or package-reason edit therefore
forced an expensive repeat of every VCS clone/makepkg source check even if
none of the VCS sources had changed.

This branch fingerprints just installed VCS names/versions, the relevant
`PKGBUILD` and `.SRCINFO` files, and AUR source configuration. The needed
VCS package coverage check is unchanged. The transaction reloads installed
ALPM package information before validating its snapshot; an external
installed VCS package replacement is still detected, even though a change
to an unrelated pacman package no longer invalidates source freshness.

`PAMAC_DEBUG_VCS_REUSE=1` provides explicit rejection reasons for
snapshot handoff and transaction reuse: unavailable scan/config, expired
two-minute timestamp, busy source lock, source/version fingerprint
change, or a newly needed VCS package absent from the original scan.

Important: genuinely changed VCS build files or installed VCS versions
still trigger a new source scan, regardless of the age window. Never
force reuse when the content or package coverage cannot be verified.

Manual checks:

1. Scan 19 development packages, then Apply within two minutes;
   expect one VCS source pass (reuse on transaction).
2. Change only a non-VCS pacman package between scan and Apply;
   verify the transaction still reuses checked VCS sources.
3. Change an installed VCS package version, or modify its
   `PKGBUILD`/`.SRCINFO`, before Apply. Require a rescan.
4. Wait over two minutes and Apply; require a rescan.
5. Enable a previously ignored VCS package not in the source snapshot;
   require a rescan because the source coverage expanded.
6. Force AUR build-lock contention and ensure the fallback respects
   exclusive source locking.
7. Try transactions with and without `PAMAC_DEBUG_VCS_REUSE=1`, check
   that logging is deterministic and no new user-facing dialogs appear.

Not yet compiled or tested locally. Separate from integration.
