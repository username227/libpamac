/*
 * Pamac-Fix shared update coordinator D-Bus API.
 *
 * RequestCheck returns immediately. CheckFinished delivers the result when
 * the single direct-check worker exits. No long-running D-Bus method calls
 * or extended proxy timeouts are required.
 */
namespace Pamac {
	[DBus (name = "org.manjaro.pamac.UpdateCoordinator1")]
	public interface UpdateCoordinator : Object {
		// running=true: join/wait for the check_id's CheckFinished signal.
		// running=false: status and updates are the completed cached result.
		// Only the /etc/pamac.conf default profile is currently supported.
		public abstract void request_check (string profile, bool force,
			out bool running, out uint64 check_id, out int status,
			out string[] updates) throws DBusError, IOError;

		public abstract void invalidate () throws DBusError, IOError;

		public signal void check_started (string profile, uint64 check_id);
		public signal void check_progress (string profile, uint64 check_id,
			string package_name, uint current, uint total, string phase);
		// A stalled worker remains owned by the coordinator, never killed
		// while Git or other children may still be touching AUR sources.
		public signal void check_stalled (string profile, uint64 check_id);
		public signal void check_finished (string profile, uint64 check_id,
			int status, string[] updates);
	}
}
