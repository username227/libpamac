# Pamac-Fix shared update coordinator: runtime-fix checkpoint

Feature branch: `feature/shared-update-coordinator` (from
`integration/pamac-fix`). Do not merge before local compilation and
live concurrency tests.

## Failure that prompted the redesign

The first prototype ran `new Database()`, `new Transaction()`,
`refresh_dbs_async()` and `get_updates_async()` directly within the
user-session D-Bus service. During testing, the coordinator remained asleep
without a build flock or visible Git activity; two clients stayed waiting.
Increasing the D-Bus method timeout to 600 seconds merely prolonged the
problem. This patch *removes* the in-process discovery path and the
long-running method, rather than extending the timeout again.

The precise stack-level source of the old stall was not established.
The redesigned worker path is intended to avoid it, not proof of success
until tested.

## New architecture

- `RequestCheck(profile, force)` is a short synchronous D-Bus method
  returning `running`, `check_id`, `status`, `updates`. **No long
  D-Bus call** and no 10-minute method timeout.
- One coordinator process starts at most one
  `/usr/bin/pamac-checkupdates --direct --categorized` subprocess for
  the `default` profile. The internal worker emits tagged `R`, `A` and
  `F` lines (repo/AUR/Flatpak), and the coordinator removes those tags
  before emitting its existing default results to the tray/GTK clients.
  The public `--direct` option remains unmodified and standalone.
- Concurrent requests join the current generation. Clients listen to
  `CheckFinished(profile, check_id, status, updates)`.
- The tray also subscribes to the result. Only a complete check may
  clear its previous update indicator.
- Completed 0/100 results are cached for 30 seconds unless invalidated.
  We watch **installed-package DB entry changes** in `<DBPath>/local`,
  rather than deletion of `db.lck`. A normal sync database refresh by the
  worker may create and delete `db.lck` without changing installed packages;
  the old watcher incorrectly downgraded real update results from 100 to 103.
  In-flight installed-package changes still conservatively return 103.
  External *sync-only* refreshes are not automatically detected by this
  monitor; their cache staleness is bounded by the 30-second TTL.
- A 120-second **diagnostic** watchdog marks a worker stalled and emits
  `CheckStalled`. Waiting CLI calls return 103 (incomplete). The
  coordinator **does not kill** the worker or start another VCS scan:
  this avoids leaving Git children without their parent-held flock.
  The existing worker may still finish later and publish its result.
  The CLI has a separate 135-second fail-safe for the case where the D-Bus
  service itself stops dispatching signals. Neither guard kills the worker.
- Statuses: 0=complete/clear, 100=complete/updates,
  101=VCS deferred due to lock, 102=VCS source failure,
  103=incomplete/aborted/invalidated or worker stalled.
- Only inability to **connect to the service before any request** permits
  the old direct fallback. After a request, errors do not trigger a
  duplicate scan.
- `TryJoin(default)` is an immediate **non-starting** D-Bus call.
  It returns a running or 30-second-cached result if one exists; otherwise
  `available=false`. `TryJoin(repo-aur)` reuses the **same** worker and
  generation, but returns only the tagged repo/AUR subset. If the worker
  found only Flatpak updates, the filtered result is complete with exit
  status 0. The coordinator emits a second `CheckFinished(repo-aur, ...)`
  signal for joiners without changing the existing `default` signal.
  `pamac-checkupdates --join-only --repo-aur` wraps the filtered join;
  `pamac-checkupdates --join-only` retains its normal all-category view.
  Both return internal exit status **104** when no result is reusable;
  neither starts a worker, refreshes sync DBs, or runs makepkg.
- Read-only clients (`pamac checkupdates` and the GTK update view when
  not explicitly refreshing databases) call `--join-only` for a compatible
  default scope. The full CLI requests `--join-only --repo-aur` because it
  intentionally excludes Flatpak; GTK retains the all-category view.
  With result 104, they use their original native typed discovery path. Thus those commands **do not initiate** the background
  checker's automatic sync/files DB refreshes or downloads. They still
  benefit from checks already triggered by the tray or another client.
  Concurrent native read-only scans without a preexisting shared worker
  remain serialized by the AUR flock but are **not yet coalesced**.
- After joining a complete 0/100 result, GTK and full CLI call
  `get_updates_after_shared_check[_async]()` to reconstruct native typed
  lists from existing AUR/VCS metadata **without rerunning source refresh**.
  Those methods first reload libalpm's handles because an external checker
  may have synchronized repository databases. The source read still
  acquires the AUR flock; contention returns an incomplete result.
- `Database.updates_match_shared_output()` compares every detected
  package **and version**, not just the count, before publishing the typed
  result. A mismatch returns 103 or shows the GUI's incomplete state.
- The GTK manager's explicit refresh button retains its original
  repository-sync and native typed/VCS discovery path. It invalidates
  any coordinator cached result after syncing, but does not start the
  coordinator's side-effecting worker. Software mode and preferences
  with different AUR/Flatpak scope also keep the original path.
- The full CLI shares only unmodified `pamac checkupdates` default-scope
  repo/AUR results that are **already** in flight or cached. Explicit
  `--aur`, `--no-aur`, `--devel`, `--no-devel`, `--builddir`, `AURDEST`,
  root, `--refresh-tmp-files-dbs` and `--download-updates` retain
  their original direct path. Configured `DownloadUpdates` still disables
  reuse. **`EnableFlatpak` no longer prevents sharing**; the coordinator
  filters worker output using category tags, never inferring type from a
  package's display name or version. Direct custom-scope VCS checks
  report 101/102 rather than falsely reporting "up to date".
- `pamac upgrade` always performs its own transaction-time update
  discovery and verification under flock; cached update notification
  data is **not** a safe substitute for transaction validation.
- No source branch has been merged; all of the GTK and CLI code is
  currently pending local compilation and runtime tests.

## Tests before considering a merge

1. Build `libpamac-full-fix` from this feature branch. Confirm
   `pamac-checkupdates`, `pamac-update-coordinator`, and session
   D-Bus service install and compile without new Vala errors.
2. Stop the **old** running `pamac-update-coordinator` after installing
   the new package. D-Bus will activate the new binary on demand.
3. `gdbus introspect --session --dest
   org.manjaro.pamac.UpdateCoordinator1 --object-path
   /org/manjaro/pamac/UpdateCoordinator1`
   must expose **RequestCheck** and **CheckFinished**/**CheckStalled**,
   NOT the old blocking Check method.
4. `pamac-checkupdates --direct` must still work independently.
   A normal `pamac-checkupdates` must return matching update lines
   and a valid exit status without a timeout warning or fallback.
5. Launch two normal `pamac-checkupdates` processes concurrently;
   confirm the coordinator spawns exactly **one** `--direct`
   subprocess and both return identical results.
6. Verify that during VCS discovery only one process owns the
   AUR flock, and a separately started build still safely defers its
   update check rather than sharing mutable files.
7. Check status 101/102 and the 120-second stalled signal:
   state 103 must never clear a known update; no second worker
   may be started while the original one is alive.
8. Verify that the worker's own repository-db refresh does **not**
   return 103 when a real update exists. Verify a real local-package
   installation or removal invalidates the cache, including during a check.
   Explicit `Invalidate()` also must invalidate it.
9. Verify subsequent completed checks can drive the tray without a
   second VCS refresh. Test owner disappearance and headless fallback.

## New GTK and full CLI verification (test checklist; see runtime ledger below)

**Build sequence matters:** install feature `libpamac` with its new D-Bus
`TryJoin` API and public `Database` methods before compiling feature
`pamac` and `pamac-cli` against the generated `Pamac-11.vapi`. Confirm
all three PKGBUILDs target `feature/shared-update-coordinator`.
No integration merges or production releases are authorized.

1. Compile all three projects and inspect compiler results on their feature
   branches. Restart the old coordinator after installing the new library.
2. With no valid cache, call `TryJoin(default)` and `TryJoin(repo-aur)`.
   Both must return `available=false`. Both `pamac-checkupdates --join-only`
   and `pamac-checkupdates --join-only --repo-aur` must exit **104**
   immediately and spawn **no** direct worker.
3. With `EnableFlatpak` configured, invalidate and run `pamac-checkupdates`
   to seed a complete 0/100 result, then immediately run both
   `pamac-checkupdates --join-only` (all categories) and
   `pamac-checkupdates --join-only --repo-aur` (filtered), followed by
   `pamac checkupdates --quiet`. The full CLI and filtered join must show
   identical repo/AUR package versions and exit statuses, while the
   unfiltered join retains any Flatpak updates. No second worker may start.
   Test a case with **only Flatpak updates**: the filtered join/CLI must
   return 0 with no package lines while the default result returns 100.
4. With the cache expired and no active coordinator worker,
   `pamac checkupdates` and `pamac checkupdates --quiet` must retain
   their original native typed formatting and non-refresh behavior.
   Compare with `pamac-checkupdates --direct` only for package versions;
   the latter is intentionally allowed to refresh databases.
5. Start a normal `pamac-checkupdates` in the background and join it
   with the full CLI or GTK Updates view. Confirm one direct worker and
   identical final versions, with the native typed list still including
   ignored/out-of-date metadata.
6. Verify `pamac checkupdates --aur --devel` and alternative `--builddir`
   use the direct option-specific path; no default result leaks into
   incompatible profiles. Custom-scope VCS contention must return 101.
7. On the GTK Updates page, verify the version of `iptvnator-git`
   matches the worker when joining a cached result, and the native typed
   path works when no shared result exists. An explicit GUI Refresh must
   sync repositories, invalidate the default result, and preserve its
   original update display and one-pass VCS behavior.
8. Hold `/var/tmp/pamac-build-<user>.pamac-aur.lock` externally and
   trigger default or custom CLI/GUI checks. Incomplete status must never
   be presented as up-to-date; no source read may race the held flock.
9. Verify software mode, Flatpak and DownloadUpdates config changes,
   ignored packages, AUR version mismatch detection, and existing tray
   notifications. `pamac upgrade` still makes fresh transaction-time
   decisions; use dry-run/controlled environments rather than real upgrade.

### GTK contention regression (controlled-lock test passed; transaction prompt test still recommended)

The GTK Updates view must display **no partial or stale update list**
while another Pamac transaction owns the AUR build lock, including while
the CLI waits at its transaction-confirmation prompt. Instead it shows
an inline waiting state (no modal error), polling **only** the advisory
lock state once per second via `Database.probe_aur_build_lock()`.
On release, `Database.get_updates_after_aur_unlock_async()` reloads
libalpm local/sync database handles and runs a **fresh complete** native
VCS check. It populates the Updates page only on success. A new lock
race re-enters waiting; leaving Updates or closing cancels the timer.
No previously verified or partial package list is selectable meanwhile.

Test with a controlled flock held by another process: open the GTK
Updates page, verify waiting/no modal, release flock, verify full
automatic refresh and enabled update actions only after completion.
Also test while `pamac upgrade` waits for [y/N], then finish or
cancel and check automatic recovery. Ensure a hard source error
(still status 102/103) is not misreported as success.

## Runtime verification ledger — 2026-11-15

These are results observed during local testing, not a blanket proof of all
possible update profiles or failure paths.

- The three feature packages compiled/installed on the user's Arch host,
  including the Vala fix for the full CLI's communicate_utf8 signature.
- Concurrent standalone pamac-checkupdates requests used one categorized
  coordinator worker. The 30-second reuse cache was observed.
- With EnableFlatpak configured and ordinary repository/AUR updates present,
  default, filtered repo-aur, and full CLI results matched, with native
  versions validated. A synthetic or real **Flatpak-only** update case remains
  untested.
- A cached CLI join was traced: it spawned the join-only bridge, without a
  redundant Git, makepkg, or direct VCS worker.
- With an externally held AUR build flock, GTK displayed an inline waiting
  state without partial update lists or modal warnings. Releasing the flock
  automatically initiated a complete native VCS refresh, visibly progressed,
  and displayed a full "up to date" result.
- GTK explicit Refresh completed a native full check and reported up to date;
  an observed join-only 104 was consistent with invalidation but occurred
  too late to distinguish it from normal 30-second cache expiry. Immediate
  tray synchronization after native GTK/CLI completion remains **out of
  scope**, tracked for follow-up work.
- GTK joined a running coordinator and completed at ~95% after its worker
  exited 0. When the full CLI joined simultaneously, an original contention
  race caused GTK to run another VCS refresh after the worker completed.
- The last race was addressed by shared (LOCK_SH) AUR flock acquisition for
  typed metadata-only readers; source refreshes and actual builds still
  require LOCK_EX. With the patched libpamac installed, the same three-client
  scenario showed GTK proceed directly to 95% and up to date **without**
  an additional individual-package VCS refresh. All three terminal/GUI
  clients completed successfully during observed runs.
- A process snapshot during three-client concurrency showed exactly one
  /usr/bin/pamac-checkupdates --direct --categorized subprocess, plus
  one default join-only GTK client and one repo-aur CLI join-only client.
- Two real-world iptvnator-git builds succeeded, adding transaction
  regression evidence (not a substitute for exhaustive concurrency tests).

Remaining before feature promotion:

1. Verify an explicit Invalidate() immediately removes a *fresh* coordinator
   result (separate from 30-second TTL expiration).
2. Recheck exclusive AUR lock contention under the new LOCK_SH reader change:
   a true writer must still block metadata readers, and incomplete status
   must never appear as "up to date".
3. Review the final branch commits, perform clean integrated builds and
   installations after fast-forwarding each integration branch.
4. Where practical, exercise Flatpak-only filtering and local package DB
   invalidation without artificially changing real installed package state.
   Those two cases should be documented as untested if no safe fixture is
   available; do not claim full coverage.
5. Do not kill a coordinator while its VCS worker is active merely to test
   restarts; parent/child lifecycle failure injection needs controlled
   fixtures and should not jeopardize the user's AUR build environment.

## Not part of this checkpoint

- Dedicated typed D-Bus serialization (clients currently reconstruct
  from source metadata under flock after a shared check).
- Full sharing for alternate/custom CLI profiles.
- Per-package VCS progress streaming from the coordinator subprocess.
- Production deployment and upstream PR preparation.

**No integration branches have been merged or changed by this feature work.**
