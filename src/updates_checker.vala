/*
 *  pamac-vala
 *
 *  Copyright (C) 2020-2023 Guillaume Benoit <guillaume@manjaro.org>
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

namespace Pamac {
	public class UpdatesChecker : Object {
		MainLoop loop;
		Config config;
		uint check_lock_timeout_id;
		GLib.File _lock;
		FileMonitor lock_monitor;
		UpdateCoordinator? coordinator;
		bool transaction_in_progress = false;
		uint64 transaction_sequence = 0;
		int64 transaction_completed_at = 0;
		uint16 _updates_nb;
		string[] _updates_list;
		public uint16 updates_nb { get { return _updates_nb; } }
		public string[] updates_list { get { return _updates_list; } }
		public uint64 refresh_period { get { return config.refresh_period; } }
		public bool no_update_hide_icon { get { return config.no_update_hide_icon; } }

		public signal void updates_available (uint16 updates_nb);
		// A new coordinated scan has started; keep the known icon but
		// make the tray react immediately rather than appearing idle.
		public signal void updates_checking ();
		public signal void updates_updating ();
		public signal void updates_operation_finished (bool success, bool verified_full_upgrade);
		// A partial check must not clear last-known updates.
		// 'deferred' means an active Pamac build prevented VCS inspection.
		public signal void updates_incomplete (bool deferred);

		public UpdatesChecker () {
			Object ();
		}

		construct {
			loop = new MainLoop ();
			config = new Config ("/etc/pamac.conf");
			string lock_str = Path.build_filename (config.db_path, "db.lck");
			_lock = GLib.File.new_for_path (lock_str);
			try {
				lock_monitor = _lock.monitor_file (FileMonitorFlags.NONE);
				lock_monitor.changed.connect (on_lock_changed);
			} catch (Error e) {
				warning (e.message);
			}
			// Receive results from checks triggered by any connected client.
			// The tray does not need to rerun VCS discovery for a completed
			// shared result, even when another process requested the check.
			try {
				coordinator = Bus.get_proxy_sync (BusType.SESSION,
					"org.manjaro.pamac.UpdateCoordinator1",
					"/org/manjaro/pamac/UpdateCoordinator1");
				coordinator.transaction_started.connect (() => {
					transaction_in_progress = true;
					transaction_sequence++;
					if (check_lock_timeout_id != 0) {
						Source.remove (check_lock_timeout_id);
						check_lock_timeout_id = 0;
					}
					updates_updating ();
				});
				coordinator.transaction_finished.connect ((success, verified) => {
					transaction_in_progress = false;
					transaction_completed_at = GLib.get_monotonic_time ();
					if (check_lock_timeout_id != 0) {
						Source.remove (check_lock_timeout_id);
						check_lock_timeout_id = 0;
					}
					updates_operation_finished (success, verified);
				});
				coordinator.check_started.connect ((profile, check_id) => {
					if (profile == "default" && !transaction_in_progress) {
						if (check_lock_timeout_id != 0) {
							Source.remove (check_lock_timeout_id);
							check_lock_timeout_id = 0;
						}
						updates_checking ();
					}
				});
			coordinator.check_finished.connect ((profile, check_id, status, lines) => {
					if (profile == "default" && !transaction_in_progress) {
						// A successful coordinated result supersedes the old
						// 5-second lock-release retry; avoid duplicate VCS work.
						if ((status == 0 || status == 100) &&
							check_lock_timeout_id != 0) {
							Source.remove (check_lock_timeout_id);
							check_lock_timeout_id = 0;
						}
						apply_shared_result (status, lines);
					}
				});
				coordinator.check_stalled.connect ((profile, check_id) => {
					if (profile == "default" && !transaction_in_progress) {
						// The worker remains alive: never clear a known update.
						updates_incomplete (false);
					}
				});
			} catch (Error e) {
				// Older/non-session installs still use pamac-checkupdates.
				message ("Shared update coordinator not available: %s", e.message);
			}
		}

		void apply_shared_result (int status, string[] lines) {
			if (status == 0) {
				_updates_nb = 0;
				_updates_list = {};
				updates_available (0);
			} else if (status == 100) {
				_updates_nb = (uint16) lines.length;
				_updates_list = lines;
				updates_available (_updates_nb);
			} else {
				// An incomplete result must never clear an existing update.
				if (lines.length > _updates_nb) {
					_updates_nb = (uint16) lines.length;
					_updates_list = lines;
					updates_available (_updates_nb);
				}
				updates_incomplete (status == 101);
			}
		}

		public void check_updates () {
			if (transaction_in_progress) {
				return;
			}
			uint64 observed_transaction_sequence = transaction_sequence;
			lock_monitor.changed.disconnect (on_lock_changed);
			config.reload ();
			if (config.refresh_period != 0) {
				// pamac-checkupdates: 0 = checked and clear, 100 = checked
				// with updates, 101 = VCS deferred, 102 = VCS check failed,
				// 103 = invalidated while checking.
				int exit_status = -1;
				uint16 found_nb = 0;
				string[] found_list = {};
				message ("check updates");
				try {
					var process = new Subprocess.newv ({"pamac-checkupdates"}, SubprocessFlags.STDOUT_PIPE);
					process.wait_async.begin (null, () => {
						if (process.get_if_exited ()) {
							exit_status = process.get_exit_status ();
							if (exit_status == 100 || exit_status == 101 || exit_status == 102 || exit_status == 103) {
								var dis = new DataInputStream (process.get_stdout_pipe ());
								try {
									string? line;
									while ((line = dis.read_line ()) != null) {
										found_nb++;
										found_list += line;
									}
								} catch (Error e) {
									warning (e.message);
									exit_status = -1;
								}
							}
						}
						loop.quit ();
					});
					loop.run ();
				} catch (Error e) {
					warning (e.message);
				}
				if (observed_transaction_sequence != transaction_sequence) {
					// A package operation started while this asynchronous check
					// was waiting; it supersedes the earlier result.
					lock_monitor.changed.connect (on_lock_changed);
					return;
				}
				if (exit_status == 0) {
					// A *complete* check is the only proof of no updates.
					_updates_nb = 0;
					_updates_list = {};
					updates_available (0);
				} else if (exit_status == 100 && found_nb > 0) {
					_updates_nb = found_nb;
					_updates_list = found_list;
					updates_available (_updates_nb);
				} else if (exit_status == 101 || exit_status == 102 || exit_status == 103) {
					// A partial result provides a lower bound, never a reason
					// to erase an existing confirmed update indication.
					if (found_nb > _updates_nb) {
						_updates_nb = found_nb;
						_updates_list = found_list;
						updates_available (_updates_nb);
					}
					updates_incomplete (exit_status == 101);
				} else {
					// A failed process is also not proof of being up to date.
					updates_incomplete (false);
				}
			}
			lock_monitor.changed.connect (on_lock_changed);
		}

		void on_lock_changed (File src, File? dest, FileMonitorEvent event_type) {
			if (transaction_in_progress ||
				(transaction_completed_at != 0 &&
				 GLib.get_monotonic_time () - transaction_completed_at < 10 * TimeSpan.SECOND)) {
				// Never schedule the old 5-second post-lock scan for a
				// coordinated package operation; its outcome is already known.
				return;
			}
			if (event_type == FileMonitorEvent.CREATED) {
				if (check_lock_timeout_id != 0) {
					Source.remove (check_lock_timeout_id);
					check_lock_timeout_id = 0;
				}
			}
			if (event_type == FileMonitorEvent.DELETED) {
				if (check_lock_timeout_id != 0) {
					Source.remove (check_lock_timeout_id);
				}
				check_lock_timeout_id = Timeout.add (5000, () => {
					check_updates ();
					check_lock_timeout_id = 0;
					return false;
				});
			}
		}
	}
}
