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

	// This lightweight entry point is used by the tray as well as scripts.
	// Prefer the shared session coordinator; keep the original direct path
	// for headless, root, and non-D-Bus environments.
	public int checkupdates () {
		try {
			Pamac.UpdateCoordinator coordinator = Bus.get_proxy_sync (
				BusType.SESSION, "org.manjaro.pamac.UpdateCoordinator1",
				"/org/manjaro/pamac/UpdateCoordinator1");
			int status = -1;
			string[] results = {};
			bool received = false;
			var loop = new MainLoop ();
			coordinator.check.begin ("default", false, (obj, res) => {
				try {
					coordinator.check.end (res, out status, out results);
					received = true;
				} catch (Error e) {
					warning ("Coordinator check failed: %s", e.message);
				}
				loop.quit ();
			});
			loop.run ();
			if (received) {
				foreach (unowned string line in results) {
					stdout.printf ("%s\n", line);
				}
				return status;
			}
		} catch (Error e) {
			warning ("Update coordinator unavailable: %s", e.message);
		}
		return checkupdates_direct ();
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

	public int main () {
		return checkupdates ();
	}
