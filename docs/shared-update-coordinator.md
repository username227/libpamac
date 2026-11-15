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
- GUI VCS progress is already enabled for its own update path, but it
  still performs independent discovery. The native CLI and GUI typed
  results remain to be migrated; do not treat this as finished
  three-client coordination.

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

## Not part of this checkpoint

- Typed update result protocol for the GTK manager and full CLI.
- Automatic GUI retry when an AUR build releases its flock.
- CLI streaming progress from the worker; the current direct subprocess
  sends results only on completion.
- Translation coverage and GUI incomplete-check banner.
- Production deployment and upstream PR preparation.

**The currently installed integration branch remains untouched.**
