/*
 * Pamac-Fix single-flight update coordinator.
 *
 * The original pamac-checkupdates direct path remains the only update
 * discovery worker. The coordinator owns exactly one subprocess, coalesces
 * matching requests and broadcasts its result. Unlike the earlier prototype,
 * no Database or Transaction objects run in the session D-Bus service.
 *
 * A synchronous D-Bus RequestCheck only starts/joins a job. It NEVER waits
 * for VCS discovery; CheckFinished delivers results asynchronously.
 */
namespace Pamac {
	public class UpdateCoordinatorService : Object, UpdateCoordinator {
		bool checking = false;
		bool stalled = false;
		uint64 generation = 0;
		uint invalidation_epoch = 0;
		uint pending_epoch = 0;
		uint watchdog_source = 0;
		int last_status = 103;
		string[] last_updates = {};
		int64 completed_at = 0;
		Subprocess? worker;
		FileMonitor? local_db_monitor;

		construct {
			// A repository refresh by pamac-checkupdates --direct can briefly
			// create/delete db.lck. Watching that lock wrongly invalidated
			// the worker's own successful check (100 -> 103). Instead watch
			// installed-package database entries: transactions create, remove
			// or rename entries in <DBPath>/local, while -Sy does not.
			// Other clients can also call Invalidate explicitly. The completed
			// result cache is deliberately limited to 30 seconds.
			var config = new Config ("/etc/pamac.conf");
			var local_db = File.new_for_path (Path.build_filename (config.db_path, "local"));
			try {
				local_db_monitor = local_db.monitor_directory (FileMonitorFlags.WATCH_MOVES);
				local_db_monitor.changed.connect ((src, dst, event_type) => {
					switch (event_type) {
						case FileMonitorEvent.CREATED:
						case FileMonitorEvent.DELETED:
						case FileMonitorEvent.MOVED:
						case FileMonitorEvent.MOVED_IN:
						case FileMonitorEvent.MOVED_OUT:
						case FileMonitorEvent.RENAMED:
							// These indicate installed-package database changes,
							// not a sync-db refresh performed by our own checker.
							invalidation_epoch++;
							completed_at = 0;
							message ("Pamac update result invalidated: installed package database changed");
							break;
						default:
							break;
					}
				});
			} catch (Error e) {
				warning ("Could not monitor installed-package database: %s", e.message);
			}
		}

		private string[] copy_lines (string[] source) {
			string[] copy = {};
			foreach (unowned string line in source) {
				copy += line;
			}
			return copy;
		}

		public void invalidate () throws DBusError, IOError {
			invalidation_epoch++;
			completed_at = 0;
		}

		private void finish_check (uint64 id, int status, string[] lines) {
			if (!checking || id != generation) {
				// A late callback from a worker already failed or was stopped.
				return;
			}
			if (watchdog_source != 0) {
				Source.remove (watchdog_source);
				watchdog_source = 0;
			}
			if (pending_epoch != invalidation_epoch &&
				(status == 0 || status == 100)) {
				message ("Pamac check result incomplete: state invalidated during check");
				status = 103;
			}
			last_status = status;
			last_updates = copy_lines (lines);
			completed_at = (status == 0 || status == 100) ?
				GLib.get_monotonic_time () : 0;
			worker = null;
			checking = false;
			stalled = false;
			message ("Pamac shared update check finished: status %d, %u result lines",
				status, (uint) lines.length);
			check_finished ("default", id, status, copy_lines (lines));
		}

		public void request_check (string profile, bool force,
			out bool running, out uint64 check_id, out int status,
			out string[] updates) throws DBusError, IOError {
			if (profile != "default") {
				throw new IOError.NOT_SUPPORTED ("Only the default Pamac profile is supported");
			}
			check_id = generation;
			status = last_status;
			updates = copy_lines (last_updates);
			if (checking) {
				// A stalled worker remains tracked and MUST NOT be duplicated.
				// Clients receive incomplete status without waiting forever.
				running = !stalled;
				if (stalled) {
					status = 103;
					updates = {};
				}
				return;
			}
			if (!force && completed_at != 0 &&
				(last_status == 0 || last_status == 100) &&
				GLib.get_monotonic_time () - completed_at < 30 * TimeSpan.SECOND) {
				running = false;
				return;
			}

			generation++;
			check_id = generation;
			checking = true;
			stalled = false;
			pending_epoch = invalidation_epoch;
			running = true;
			status = 103;
			updates = {};
			check_started ("default", check_id);
			message ("Pamac shared update check started");

			try {
				// --direct bypasses the D-Bus client bridge. This is the tested
				// pre-coordinator checker, including its original flock rules.
				var proc = new Subprocess.newv (
					{"/usr/bin/pamac-checkupdates", "--direct"},
					SubprocessFlags.STDOUT_PIPE | SubprocessFlags.STDERR_PIPE);
				worker = proc;
				uint64 id = check_id;

				// This diagnostic guard stops CLI clients waiting forever,
				// without terminating Git or dropping our AUR flock early.
				// Keep ownership of this worker until it really exits, even
				// after notifying waiting clients of incomplete status.
				watchdog_source = Timeout.add_seconds (120, () => {
					watchdog_source = 0;
					if (checking && id == generation) {
						stalled = true;
						warning ("Pamac shared check worker still running; preserving its AUR lock");
						check_stalled ("default", id);
					}
					return false;
				});

				proc.communicate_utf8_async.begin (null, null, (obj, res) => {
					int finished_status = 103;
					string[] finished_lines = {};
					try {
						string? output;
						string? diagnostics;
						proc.communicate_utf8_async.end (res, out output, out diagnostics);
						if (proc.get_if_exited ()) {
							int exit_code = proc.get_exit_status ();
							if (exit_code == 0 || exit_code == 100 ||
								exit_code == 101 || exit_code == 102) {
								finished_status = exit_code;
							}
						}
						if (output != null) {
							foreach (string line in output.split ("\n")) {
								if (line.length != 0) {
									finished_lines += line;
								}
							}
						}
						if (diagnostics != null && diagnostics.strip () != "") {
							message ("Pamac check diagnostics: %s", diagnostics.strip ());
						}
						// A successful nonempty update check must return at
						// least one package line, and clear must return none.
						if ((finished_status == 100 && finished_lines.length == 0) ||
							(finished_status == 0 && finished_lines.length != 0)) {
							finished_status = 103;
						}
					} catch (Error e) {
						warning ("Pamac direct check failed: %s", e.message);
					}
					finish_check (id, finished_status, finished_lines);
				});
			} catch (Error e) {
				warning ("Could not start Pamac check worker: %s", e.message);
				finish_check (check_id, 103, {});
				running = false;
				status = 103;
			}
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
				conn.register_object<Pamac.UpdateCoordinator> (
					"/org/manjaro/pamac/UpdateCoordinator1", coordinator);
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
