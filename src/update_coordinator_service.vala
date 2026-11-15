/*
 * Pamac-Fix per-user update coordinator (initial default-profile backend).
 *
 * One session D-Bus owner executes the actual update query; simultaneous
 * requests join it. This is deliberately separate from the transaction-wide
 * AUR flock, which still protects all build-directory mutations.
 */
namespace Pamac {
	public class UpdateCoordinatorService : Object, UpdateCoordinator {
		bool checking = false;
		int last_status = -1;
		string[] last_updates = {};
		int64 completed_at = 0;
		uint invalidation_epoch = 0;
		FileMonitor? db_lock_monitor;
		bool own_db_refresh = false;

		construct {
			// External pacman transactions invalidate a cached check as soon
			// as their database lock disappears. Do not treat our own database
			// refresh as an external package change.
			var config = new Config ("/etc/pamac.conf");
			var db_lock = File.new_for_path (Path.build_filename (config.db_path, "db.lck"));
			try {
				db_lock_monitor = db_lock.monitor_file (FileMonitorFlags.NONE);
				db_lock_monitor.changed.connect ((src, dst, event_type) => {
					if (event_type == FileMonitorEvent.DELETED && !own_db_refresh) {
						invalidation_epoch++;
						completed_at = 0;
					}
				});
			} catch (Error e) {
				warning ("Could not monitor pacman database lock: %s", e.message);
			}
		}

		private string[] copy_lines (string[] source) {
			string[] copy = {};
			foreach (unowned string line in source) {
				copy += line;
			}
			return copy;
		}

		public void invalidate () throws IOError {
			invalidation_epoch++;
			completed_at = 0;
		}

		public async void check (string profile, bool force,
			out int exit_status, out string[] updates) throws IOError {
			if (profile != "default") {
				throw new IOError.NOT_SUPPORTED (
					"Only the default Pamac update profile is supported");
			}

			// Callers joining an active check receive precisely its results.
			// Do not contend for the AUR flock or start another source refresh.
			if (checking) {
				while (checking) {
					Timeout.add (100, () => {
						check.callback ();
						return false;
					});
					yield;
				}
				exit_status = last_status;
				updates = copy_lines (last_updates);
				return;
			}

			// A short-lived complete result avoids serial duplicate 48-second
			// checks from the GUI, CLI and tray. A manual forced check or an
			// invalidation always bypasses this cache.
			if (!force && completed_at != 0 &&
				(last_status == 0 || last_status == 100) &&
				GLib.get_monotonic_time () - completed_at < 30 * TimeSpan.SECOND) {
				exit_status = last_status;
				updates = copy_lines (last_updates);
				return;
			}

			checking = true;
			uint start_epoch = invalidation_epoch;
			check_started (profile);
			int result = -1;
			string[] found = {};

			// Match pamac-checkupdates' default scope. Per-user overrides and
			// explicit CLI flags require separate, fully keyed profiles before
			// clients with differing settings may share this service.
			var config = new Config ("/etc/pamac.conf");
			config.enable_appstream = false;
			config.enable_snap = false;
			var database = new Database (config);
			database.vcs_refresh_progress.connect ((package_name, current, total, phase) => {
				check_progress (profile, package_name, current, total, phase);
			});

			var transaction = new Transaction (database);
			if (database.need_refresh ()) {
				own_db_refresh = true;
				yield transaction.refresh_dbs_async ();
				own_db_refresh = false;
			}

			var available = yield database.get_updates_async ();
			foreach (unowned AlpmPackage pkg in available.repos_updates) {
				if (pkg.installed_version != null) {
					found += "%s  %s -> %s".printf (pkg.name, pkg.installed_version, pkg.version);
				} else {
					found += "%s  %s".printf (pkg.name, pkg.version);
				}
			}
			foreach (unowned AURPackage pkg in available.aur_updates) {
				found += "%s  %s -> %s".printf (pkg.name, pkg.installed_version, pkg.version);
			}
			foreach (unowned FlatpakPackage pkg in available.flatpak_updates) {
				unowned string? app_name = pkg.app_name;
				if (app_name == null) {
					found += "%s  %s".printf (pkg.name, pkg.version);
				} else {
					found += "%s  %s".printf (app_name, pkg.version);
				}
			}

			// Preserve pamac-checkupdates' existing optional behavior:
			// refresh the files databases and pre-download packages only
			// when an update check has completed successfully.
			if (!database.vcs_updates_deferred &&
				database.last_vcs_refresh_success && found.length > 0) {
				own_db_refresh = true;
				yield transaction.refresh_files_dbs_async ();
				if (config.download_updates) {
					yield transaction.download_updates_async ();
				}
				own_db_refresh = false;
			}

			if (database.vcs_updates_deferred) {
				result = 101;
			} else if (!database.last_vcs_refresh_success) {
				result = 102;
			} else {
				result = found.length == 0 ? 0 : 100;
			}
			// An installation can invalidate an in-flight check. Do not
			// publish its possibly stale result as complete.
			if (start_epoch != invalidation_epoch && (result == 0 || result == 100)) {
				result = 103;
			}
			last_status = result;
			last_updates = copy_lines (found);
			completed_at = (result == 0 || result == 100) ?
				GLib.get_monotonic_time () : 0;
			checking = false;
			check_finished (profile, result, found);
			exit_status = result;
			updates = found;
		}
	}
}

int main (string[] args) {
	var loop = new MainLoop ();
	var coordinator = new Pamac.UpdateCoordinatorService ();
	Bus.own_name (BusType.SESSION,
		"org.manjaro.pamac.UpdateCoordinator1",
		BusNameOwnerFlags.NONE,
		(conn) => {
			try {
				conn.register_object<Pamac.UpdateCoordinator> ("/org/manjaro/pamac/UpdateCoordinator1", coordinator);
			} catch (IOError e) {
				warning ("Could not register Pamac update coordinator: %s", e.message);
				loop.quit ();
			}
		},
		null,
		() => {
			warning ("Could not acquire Pamac update coordinator D-Bus name");
			loop.quit ();
		});
	loop.run ();
	return 0;
}
