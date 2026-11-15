/*
 * Pamac-Fix shared update coordinator API.
 *
 * The per-user session service owns ordinary update discovery. A caller
 * requesting the same profile while a check is already running joins that
 * check; it does not start another VCS source refresh. Build/install
 * transactions still perform their own final validation under flock.
 */
namespace Pamac {
	[DBus (name = "org.manjaro.pamac.UpdateCoordinator1")]
	public interface UpdateCoordinator : Object {
		// Version 1 currently supports only "default", using /etc/pamac.conf.
		// Other scopes must never silently share incompatible results.
		public abstract async void check (string profile, bool force,
			out int exit_status, out string[] updates) throws IOError;

		// Invalidate after installs or package database changes.
		public abstract void invalidate () throws IOError;

		public signal void check_started (string profile);
		public signal void check_progress (string profile, string package_name,
			uint current, uint total, string phase);
		public signal void check_finished (string profile, int exit_status,
			string[] updates);
	}
}
