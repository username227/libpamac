# Optional unused Flatpak dependency cleanup

## Scope

Pamac currently uses `Flatpak.Installation.system()`. Its uninstall
transaction removes selected apps but does not automatically remove all
unused runtimes/extensions. This feature adds an **off-by-default** option
`RemoveUnusedFlatpakDeps` saved in `/etc/pamac.conf` and exposed at
**Preferences → Third Party → Flatpak**.

When enabled, **after** a successful Flatpak transaction containing at least
one app removal, the Flatpak backend calls
`Flatpak.Installation.list_unused_refs(null, cancellable)` against the
post-removal installation and submits the returned refs in a second
`Flatpak.Transaction`. This models `flatpak uninstall --unused --system`
rather than the narrower end-of-life-only heuristic of
`Flatpak.Transaction.set_include_unused_uninstall_ops(true)`.

This removes *all* system-installation runtimes/extensions that Flatpak
considers unused, including previously orphaned ones—not only refs associated
with the just-removed app. Flatpak's own dependency and pin rules identify the
eligible refs. It does **not** delete application user data or clean
`--user` Flatpaks, which Pamac does not currently manage through this plugin.

The setting is loaded and persisted in both the GUI's configuration class
and the privileged daemon's configuration class. The daemon passes a boolean
to the internal Flatpak plugin interface; this does not change the public
D-Bus method signatures or CLI package source.

## Build order

The libpamac feature branch `fix/flatpak-unused-deps-on-remove`
is based on `fix/gtk-background-vcs-snapshot-handoff`.
The GTK feature branch `fix/flatpak-unused-deps-on-remove`
is based on `fix/background-check-responsive-browsing`.

Rebuild and install the **libpamac** feature branch first, then the **GTK**
feature branch. The installed `pamac-flatpak` plugin and daemon must both
come from the same libpamac build. CLI does not need rebuilding.

## Test matrix (needs actual Flatpak installation)

1. Enable the preference and close/reopen Preferences; confirm it persists
   through `/etc/pamac.conf` and a daemon reload. Disable and verify again.
2. With the option **off**, remove an isolated test app. Verify Pamac
   preserves the previous uninstall behavior.
3. With the option **on**, remove a test app whose runtime is used by no
   other app and is not pinned. Verify the app is removed, the unused runtime
   is removed by the **second** operation, and both appear in progress details.
4. Repeat with two apps sharing a runtime. Removing the first must **not**
   remove the runtime. Removing the last consumer should remove it.
5. Confirm an explicitly pinned runtime is never selected.
6. Confirm existing unrelated orphaned runtimes are also removed
   (same scope as `flatpak uninstall --unused --system`).
7. Confirm normal Flatpak install and update transactions **without** app
   removal do not run unused cleanup.
8. Confirm a failed/cancelled primary removal does **not** run cleanup.
   A failed secondary cleanup is reported rather than silently succeeding.
9. Confirm the preference is disabled when Flatpak support is off, and that
   the ordinary ALPM `Remove unrequired dependencies` setting is unaffected.
10. Confirm an application removal through a mixed Pamac transaction does not
    change the repo/AUR transaction selection or the verified VCS cache path.

Use disposable test Flatpaks/a test machine for removal tests. Do not
expect the native Flatpak API's full cleanup rules to be limited to runtimes
installed with the selected application.

## Not yet validated

At authoring time, the changes are committed for source review but were
**not compiled or run** on the user's Arch Linux system. Do not merge
until both packages build and the removal cases pass.
