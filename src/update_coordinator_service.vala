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
		bool transaction_active = false;
		uint64 current_transaction_id = 0;
		uint64 generation = 0;
		uint64 invalidation_epoch = 0;
		uint64 pending_epoch = 0;
		bool recheck_queued = false;
		uint watchdog_source = 0;
		int last_status = 103;
		string[] last_updates = {};
		string[] last_repo_aur_updates = {};
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

		public uint64 begin_external_check () throws DBusError, IOError {
			if (transaction_active) {
				return 0;
			}
			// A token fences the future result against every package change,
			// other explicit refresh, or queued post-installation check.
			invalidation_epoch++;
			completed_at = 0;
			return invalidation_epoch;
		}

		public bool publish_external_check (uint64 epoch,
			string[] updates, string[] repo_aur_updates) throws DBusError, IOError {
			if (transaction_active || epoch == 0 || epoch != invalidation_epoch) {
				return false;
			}
			// Reject malformed/empty entries and ensure the repo/AUR profile
			// is truly a subset of the complete default-profile result.
			foreach (unowned string line in updates) {
				if (line.strip () == "") {
					return false;
				}
			}
			foreach (unowned string line in repo_aur_updates) {
				if (line.strip () == "") {
					return false;
				}
				bool found = false;
				foreach (unowned string full_line in updates) {
					if (line == full_line) {
						found = true;
						break;
					}
				}
				if (!found) {
					return false;
				}
			}
			// Fence older workers; their final callback may not replace this
			// newer verified snapshot.
			invalidation_epoch++;
			last_status = updates.length == 0 ? 0 : 100;
			last_updates = copy_lines (updates);
			last_repo_aur_updates = copy_lines (repo_aur_updates);
			completed_at = GLib.get_monotonic_time ();
			message ("Pamac external update result published: %u lines",
				(uint) updates.length);
			check_finished ("default", generation, last_status, copy_lines (last_updates));
			check_finished ("repo-aur", generation,
				repo_aur_status (last_status, last_repo_aur_updates),
				copy_lines (last_repo_aur_updates));
			return true;
		}

		// This is a state signal, not an update check. Invalidate any prior
		// snapshot but retain the tray's last-known icon/count while packages
		// are being prepared, downloaded, built and installed.
		public uint64 begin_transaction () throws DBusError, IOError {
			current_transaction_id++;
			transaction_active = true;
			recheck_queued = false;
			invalidation_epoch++;
			completed_at = 0;
			transaction_started ();
			return current_transaction_id;
		}

		public void finish_transaction (uint64 transaction_id,
			bool success, bool full_default_upgrade) throws DBusError, IOError {
			if (!transaction_active || transaction_id == 0 ||
				transaction_id != current_transaction_id) {
				return;
			}
			transaction_active = false;
			invalidation_epoch++;
			completed_at = 0;
			recheck_queued = false;
			bool verified_complete = success && full_default_upgrade;
			transaction_finished (success, verified_complete);
			if (verified_complete) {
				// A complete default-profile sysupgrade just checked and
				// applied its selected repo/AUR/Flatpak upgrades. Reuse that
				// outcome directly instead of starting another VCS scan.
				// Keep the in-flight worker's ID intact until it exits;
				// otherwise its callback could leave checking stuck forever.
				if (!checking) {
					generation++;
				}
				last_status = 0;
				last_updates = {};
				last_repo_aur_updates = {};
				completed_at = GLib.get_monotonic_time ();
				check_finished ("default", generation, 0, {});
				check_finished ("repo-aur", generation, 0, {});
			} else {
				// A partial/custom/failed operation cannot prove zero updates.
				// Preserve the last-known icon; invalidate reuse of its cache.
				last_status = 103;
			}
		}

		// Explicit callers may still request a new check, but transaction
		// completion itself must never invoke this secondary worker.
		public void recheck_after_transaction () throws DBusError, IOError {
			if (transaction_active) {
				return;
			}
			invalidate ();
			if (checking) {
				// A transaction can finish while another worker still owns the
				// AUR source lock. Start exactly one new scan after it exits.
				recheck_queued = true;
				return;
			}
			bool running;
			uint64 check_id;
			int status;
			string[] updates;
			request_check ("default", true, out running, out check_id,
				out status, out updates);
		}

		private void finish_check (uint64 id, int status, string[] lines,
			string[] repo_aur_lines) {
			// Incoming array parameters are unowned in Vala. Use owned
			// local snapshots so an externally published result can safely
			// replace an older worker's result before broadcasting it.
			string[] final_lines = copy_lines (lines);
			string[] final_repo_aur_lines = copy_lines (repo_aur_lines);
			if (!checking || id != generation) {
				// A late callback from a worker already failed or was stopped.
				return;
			}
			if (watchdog_source != 0) {
				Source.remove (watchdog_source);
				watchdog_source = 0;
			}
			if (transaction_active) {
				// A pre-existing worker has been superseded by an ongoing
				// package operation. Never overwrite its visible progress.
				worker = null;
				checking = false;
				stalled = false;
				return;
			}
			if (pending_epoch != invalidation_epoch) {
				if (completed_at != 0 &&
					(last_status == 0 || last_status == 100)) {
					// An external GTK/CLI check published a newer complete
					// snapshot. Do not let an older worker overwrite it.
					status = last_status;
					final_lines = copy_lines (last_updates);
					final_repo_aur_lines = copy_lines (last_repo_aur_updates);
				} else if (status == 0 || status == 100) {
					message ("Pamac check result incomplete: state invalidated during check");
					status = 103;
				}
			}
			last_status = status;
			last_updates = copy_lines (final_lines);
			last_repo_aur_updates = copy_lines (final_repo_aur_lines);
			completed_at = (status == 0 || status == 100) ?
				GLib.get_monotonic_time () : 0;
			worker = null;
			checking = false;
			stalled = false;
			message ("Pamac shared update check finished: status %d, %u result lines",
				status, (uint) final_lines.length);
			check_finished ("default", id, status, copy_lines (final_lines));
			check_finished ("repo-aur", id,
				repo_aur_status (status, final_repo_aur_lines), copy_lines (final_repo_aur_lines));
			if (recheck_queued) {
				recheck_queued = false;
				Idle.add (() => {
					try {
						bool running;
						uint64 next_id;
						int next_status;
						string[] next_updates;
						request_check ("default", true, out running, out next_id,
							out next_status, out next_updates);
					} catch (Error e) {
						warning ("Could not start queued Pamac check: %s", e.message);
					}
					return false;
				});
			}
		}

		// A complete worker with Flatpak-only updates is a clean result
		// for an observer that intentionally excludes Flatpak.
		private int repo_aur_status (int status, string[] repo_aur_lines) {
			return status == 100 && repo_aur_lines.length == 0 ? 0 : status;
		}

		private bool has_newer_published_snapshot () {
			return checking && pending_epoch != invalidation_epoch &&
				completed_at != 0 &&
				(last_status == 0 || last_status == 100) &&
				GLib.get_monotonic_time () - completed_at < 30 * TimeSpan.SECOND;
		}

		// Read-only clients can reuse a running or recently finished job,
		// but must never initiate the standalone worker: that worker may
		// refresh pacman sync/files DBs, unlike native "checkupdates".
		public void try_join (string profile,
			out bool available, out bool running, out uint64 check_id,
			out int status, out string[] updates) throws DBusError, IOError {
			if (profile != "default" && profile != "repo-aur") {
				throw new IOError.NOT_SUPPORTED ("Unknown Pamac result profile");
			}
			check_id = generation;
			status = profile == "repo-aur" ?
				repo_aur_status (last_status, last_repo_aur_updates) : last_status;
			updates = copy_lines (profile == "repo-aur" ?
				last_repo_aur_updates : last_updates);
			available = false;
			running = false;
			if (transaction_active) {
				// Reject concurrent read-only checks rather than racing the
				// transaction with another VCS/source inspection.
				available = true;
				status = 103;
				updates = {};
				return;
			}
			if (has_newer_published_snapshot ()) {
				// An older worker is still exiting, but a newer GTK/CLI
				// snapshot has already been verified and broadcast.
				available = true;
				return;
			}
			if (checking) {
				available = true;
				running = !stalled;
				if (stalled) {
					status = 103;
					updates = {};
				}
				return;
			}
			if (completed_at != 0 &&
				(last_status == 0 || last_status == 100) &&
				GLib.get_monotonic_time () - completed_at < 30 * TimeSpan.SECOND) {
				available = true;
			}
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
			if (transaction_active) {
				running = false;
				status = 103;
				updates = {};
				return;
			}
			if (!force && has_newer_published_snapshot ()) {
				running = false;
				return;
			}
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
					{"/usr/bin/pamac-checkupdates", "--direct", "--categorized"},
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
						check_stalled ("repo-aur", id);
					}
					return false;
				});

				proc.communicate_utf8_async.begin (null, null, (obj, res) => {
					int finished_status = 103;
					string[] finished_lines = {};
					string[] finished_repo_aur_lines = {};
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
								if (line.length == 0) {
									continue;
								}
								// Internal worker output is classified at the
								// source. Never infer a category from display
								// text (replacement-package lines can resemble
								// Flatpak updates). Invalid protocol data must
								// not publish a complete result.
								bool repo_aur = line.has_prefix ("R\t") ||
									line.has_prefix ("A\t");
								bool flatpak = line.has_prefix ("F\t");
								if ((!repo_aur && !flatpak) || line.length <= 2) {
									warning ("Invalid classified update worker output");
									finished_status = 103;
									finished_lines = {};
									finished_repo_aur_lines = {};
									break;
								}
								string display_line = line.substring (2);
								finished_lines += display_line;
								if (repo_aur) {
									finished_repo_aur_lines += display_line;
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
					finish_check (id, finished_status, finished_lines,
					finished_repo_aur_lines);
				});
			} catch (Error e) {
				warning ("Could not start Pamac check worker: %s", e.message);
				finish_check (check_id, 103, {}, {});
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
