# Shared update coordinator — development checkpoint

Branch: `feature/shared-update-coordinator` (based on `integration/pamac-fix`).

## Motivation

The CLI, manager and tray currently run independent update queries. A VCS
source refresh can take around a minute; duplicating it wastes time and can
make the tray show an outdated or falsely definitive icon. Keep the
transaction-wide AUR `flock`: the coordinator handles *requests and results*,
not permission to modify build files.

## Stage 1 implemented (not yet compiled/tested)

- Session-bus service: `org.manjaro.pamac.UpdateCoordinator1`.
- D-Bus activation via `pamac-update-coordinator` installed by libpamac.
- `Check(profile, force)` for the **default** `/etc/pamac.conf` scope only.
- Simultaneous calls for this profile join the same in-flight check.
- Complete results cached for 30 seconds, unless invalidated.
- Emits started/progress/finished signals with the checked VCS package,
  ordinal and phase; results include update lines.
- Watches pacman's database lock to invalidate caches after external
  transactions, with separate `Invalidate()` for future clients.
- Returns **0** for complete/clear, **100** for complete/updates,
  **101** for safely deferred VCS, **102** for failed VCS, and **103** for
  results invalidated during the check.
- `pamac-checkupdates` tries the session service and falls back to the
  prior direct implementation when unavailable. The tray also listens for
  shared results, preserving its existing incomplete-check behavior.
- The GUI's own VCS check now displays per-package and per-stage progress.

The 30-second cache is a UX optimization, **not** transaction authorization.
Installing/upgrading packages always requires a new dependency/version
verification under the existing lock.

## Deliberately not finished

- The GTK manager still fetches its own typed `Pamac.Updates` objects.
  It now displays progress, but its full query has **not yet been moved**
  to the service. It must not deserialize text display lines into
  authoritative package objects without a typed, versioned protocol.
- `pamac-cli checkupdates` and `pamac upgrade` still use their existing
  code and support flags that may not match the default profile. Do not
  share results for different AUR, VCS, build-dir, Flatpak or ignore options
  until those are represented in the request key.
- A robust scheduled retry for statuses 101–103, GUI incomplete-state banner,
  and explicit end-of-transaction invalidation remain to be implemented.
- Translation coverage, process failures/timeouts, cache races, and owner
  reconnects require full live testing.

## Verification checklist before any merge

1. Build libpamac feature branch (including the new coordinator binary,
   GIR/VAPI and D-Bus service file). Rebuild GUI against that library.
2. After installation, introspect the user-session service:
   `gdbus introspect --session --dest org.manjaro.pamac.UpdateCoordinator1 --object-path /org/manjaro/pamac/UpdateCoordinator1`.
3. Invoke `Check('default', false)` twice concurrently. Confirm the
   second joins and only one exclusive AUR flock appears in `lslocks`.
4. Run `pamac-checkupdates`; compare status and package lines with the
   legacy direct checker, including a known `-git` update.
5. Test deferred/failed checks (101/102), invalidation during checking
   (103), and a subsequent successful retry. An incomplete result must
   never clear a previously known update.
6. Verify the tray changes state from a coordinator result without
   triggering another source refresh.
7. Confirm GUI VCS progress names and phases, and no regression to
   build/install lock lifetime.
8. Test headless/non-session fallback and cases with
   `DownloadUpdates` configured.
9. Use the ten-rebuild IPTVnator regression suite before considering the
   existing source-directory corruption issue resolved.

Never merge this prototype into `integration/pamac-fix` solely on a
static code review; compiling and interactive tests are required.
