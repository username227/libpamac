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
  `/usr/bin/pamac-checkupdates --direct` subprocess for the
  `default` profile. The CLI direct option invokes the pre-existing,
  standalone implementation and never calls the coordinator.
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
- The GTK manager and full `pamac checkupdates` CLI now join a
  shared default-profile check via the tested `pamac-checkupdates`
  bridge. After the worker exits with 0/100, they call libpamac's
  `get_updates_after_shared_check[_async]()` to reconstruct native
  typed lists from existing AUR/VCS metadata **without rerunning source
  refresh**. Those methods first reload libalpm's database handles because
  the coordinator worker may have synchronized repository databases in
  a separate process. The source read still acquires the AUR flock; if it cannot,
  the client reports an incomplete result rather than reading mutable data.
- `Database.updates_match_shared_output()` compares every detected
  package **and version**, not just the count, before publishing a typed
  result. A mismatch returns 103 or shows the GUI's incomplete state;
  it never silently announces `up to date`.
- The GTK manager's explicit refresh button still refreshes repository
  databases first, invalidates the coordinator result cache, then joins
  one VCS refresh. Software mode and changed-scope preferences keep
  the original direct typed query. The GUI does not currently stream
  per-package VCS progress from the coordinator's subprocess.
- The full CLI shares only ordinary `pamac checkupdates` with a
  matching default profile. Explicit `--aur`/`--no-aur`,
  `--devel`/`--no-devel`, `--builddir`, `AURDEST`, root,
  `--refresh-tmp-files-dbs` and `--download-updates` retain their
  native path to preserve exact semantics. Configured Flatpak support
  also prevents CLI reuse, because full CLI intentionally disables
  Flatpak while the standalone checker honors `pamac.conf`.
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

## New GTK and full CLI verification (not yet performed)

**Build sequence matters:** install the feature `libpamac` with the new
public `Database` methods before compiling the feature `pamac` and
`pamac-cli` against the generated `Pamac-11.vapi`. Confirm all three
PKGBUILDs target `feature/shared-update-coordinator` and retain the
integrated build/SONAME fixes; do not merge integration yet.

1. Compile all three projects; resolve any Vala or Meson errors on their
   respective **feature** branches before installing clients.
2. From a default non-root environment, invalidate the coordinator cache,
   run `pamac-checkupdates` and `pamac checkupdates --quiet`, and
   compare package lines and exit statuses (100 with updates / 0 without).
   `pamac checkupdates` must retain native verbose formatting and
   ignored/out-of-date package sections.
3. Confirm `pamac checkupdates` joins a concurrent
   `pamac-checkupdates`: at most one `--direct` worker.
4. Verify `pamac checkupdates --aur --devel` and
   `pamac checkupdates --builddir=...` stay on the original
   option-specific path, and neither shares a default-profile result
   when the requested scope differs. Do not test a custom builddir on
   the normal source directory without backups.
5. Close and reopen the GTK manager. On its Updates page, confirm the
   version shown for `iptvnator-git` exactly matches the standalone
   checker. Opening an already-checked GUI view should use the 30-second
   cache and **not** spawn a second VCS refresh.
6. Click GTK Refresh explicitly. It must sync pacman databases,
   invalidate the shared cache, start exactly one VCS scan and display
   the typed package lists. If refresh fails, it must not display a
   false `Your system is up to date` page.
7. Hold `/var/tmp/pamac-build-<user>.pamac-aur.lock` externally,
   invalidate and start the default CLI/GUI checks. The shared worker
   should return 101, full CLI should preserve that incomplete status,
   and GTK should show its incomplete state without discarding old
   update data. No source check should race the held flock.
8. Exercise appstream/software mode, Flatpak-enabled config, ignored
   updates, and GUI notification interactions. These must preserve
   the original typed package behavior and option semantics.
9. Verify `pamac upgrade` still does fresh transaction-time checks,
   never installs based solely on the coordinator's cached results.
   Use dry-run/controlled test environments rather than a real upgrade.

## Not part of this checkpoint

- Dedicated typed D-Bus serialization (clients currently reconstruct
  from source metadata under flock after a shared check).
- Full sharing for alternate/custom CLI profiles.
- Per-package VCS progress streaming from the coordinator subprocess.
- Automatic GUI retry when an AUR build releases its flock.
- Production deployment and upstream PR preparation.

**All integration branches remain unchanged.**
