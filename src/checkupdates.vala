/*
 *  pamac-vala
 *
 *  Copyright (C) 2023 Guillaume Benoit <guillaume@manjaro.org>
 *
 *  This program is free software; you can redistribute it and/or modify
 *  it under the terms of the GNU General Public License as published by
 *  the Free Software Foundation; either version 3 of the License, or
 *  (at your option) any later version.
 *
 *  This program is distributed in the hope that it will be useful,
 *  but WITHOUT ANY WARRANTY; without even the implied warranty of
 *  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 *  GNU General Public License for more details.
 *
 *  You should have received a get of the GNU General Public License
 *  along with this program.  If not, see <http://www.gnu.org/licenses/>.
 */

	// The coordinator responds immediately; completion is delivered via
	// CheckFinished. A second request joins the same child process.
	// Only absence of the service permits a standalone fallback.
	public int checkupdates () {
		Pamac.UpdateCoordinator coordinator;
		try {
			coordinator = Bus.get_proxy_sync (
				BusType.SESSION, "org.manjaro.pamac.UpdateCoordinator1",
				"/org/manjaro/pamac/UpdateCoordinator1");
		} catch (Error e) {
			warning ("Update coordinator unavailable: %s", e.message);
			return checkupdates_direct ();
		}

		var loop = new MainLoop ();
		bool done = false;
		uint64 observed_id = 0;
		int observed_status = 103;
		string[] observed_lines = {};
		uint64 expected_id = 0;
		coordinator.check_finished.connect ((profile, id, status, lines) => {
			if (profile != "default") {
				return;
			}
			// Also capture an extremely fast response emitted while our
			// synchronous D-Bus request is in flight, before it returns id.
			observed_id = id;
			observed_status = status;
			observed_lines = lines;
			if (expected_id != 0 && id == expected_id) {
				done = true;
				if (loop.is_running ()) {
					loop.quit ();
				}
			}
		});

		bool running = false;
		int result = 103;
		string[] results = {};
		try {
			coordinator.request_check ("default", false,
				out running, out expected_id, out result, out results);
		} catch (Error e) {
			// The service may have accepted another request already.
			// Never launch a second VCS source scan on D-Bus failure.
			warning ("Shared update request failed: %s", e.message);
			return 103;
		}

		if (running) {
			if (observed_id == expected_id) {
				done = true;
			}
			if (!done) {
				// If the service disappears, stop waiting and return an
				// incomplete status rather than silently starting a scan.
				uint watcher = Bus.watch_name (BusType.SESSION,
					"org.manjaro.pamac.UpdateCoordinator1", BusNameWatcherFlags.NONE,
					(conn, name, owner) => {
						// The coordinator is available; keep watching its owner.
					},
					(conn, name) => {
						if (!done) {
							done = true;
							observed_status = 103;
							observed_lines = {};
							if (loop.is_running ()) {
								loop.quit ();
							}
						}
					});
				loop.run ();
				Bus.unwatch_name (watcher);
			}
			result = observed_status;
			results = observed_lines;
		}

		foreach (unowned string line in results) {
			stdout.printf ("%s\n", line);
		}
		return result;
	}

	public int checkupdates_direct () {
		var config = new Pamac.Config ("/etc/pamac.conf");
		config.enable_appstream = false;
		config.enable_snap = false;
		var database = new Pamac.Database (config);
		var transaction = new Pamac.Transaction (database);
		var loop = new MainLoop ();
		if (database.need_refresh ()) {
			transaction.refresh_dbs_async.begin (() => {
				loop.quit ();
			});
			loop.run ();
		}
		var updates = database.get_updates ();
		// Exit 101: update list is incomplete because a VCS check was safely
		// deferred. Exit 102: source check failed and the list is incomplete.
		// Neither may be interpreted by the tray as "no updates available".
		int partial_status = database.vcs_updates_deferred ? 101 :
			(!database.last_vcs_refresh_success ? 102 : 0);
		uint updates_nb = updates.repos_updates.length + updates.aur_updates.length + updates.flatpak_updates.length;
		if (updates_nb == 0) {
			return partial_status;
		} else {
			// Do not refresh files databases or download packages on a partial
			// check: a concurrent Pamac build may hold the ALPM transaction lock.
			if (partial_status == 0) {
				transaction.refresh_files_dbs_async.begin (() => {
					loop.quit ();
				});
				loop.run ();
				if (config.download_updates) {
					transaction.download_updates_async.begin (() => {
						loop.quit ();
					});
					loop.run ();
				}
			}
			foreach (unowned Pamac.AlpmPackage pkg in updates.repos_updates) {
				if (pkg.installed_version != null) {
					stdout.printf ("%s  %s -> %s\n", pkg.name, pkg.installed_version, pkg.version);
				} else {
					// it's a replacer
					stdout.printf ("%s  %s\n", pkg.name, pkg.version);
				}
			}
			foreach (unowned Pamac.AURPackage pkg in updates.aur_updates) {
				stdout.printf ("%s  %s -> %s\n", pkg.name, pkg.installed_version, pkg.version);
			}
			foreach (unowned Pamac.FlatpakPackage pkg in updates.flatpak_updates) {
				unowned string? app_name = pkg.app_name;
				if (app_name == null) {
					stdout.printf ("%s  %s\n", pkg.name, pkg.version);
				} else {
					stdout.printf ("%s  %s\n", app_name, pkg.version);
				}
			}
			// Only a complete check may replace the previously confirmed
			// notification state. Partial checks retain their own exit codes.
			return partial_status != 0 ? partial_status : 100;
		}
	}

	public int main (string[] args) {
		if (args.length == 2 && args[1] == "--direct") {
			// Internal coordinator worker; never recurse into the service.
			return checkupdates_direct ();
		}
		if (args.length != 1) {
			stderr.printf ("Usage: pamac-checkupdates [--direct]\n");
			return 2;
		}
		return checkupdates ();
	}
