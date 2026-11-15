namespace Pamac {
    [CCode (cname = "pamac_aur_build_lock_try", cheader_filename = "aur_build_lock.h")]
    internal extern int aur_build_lock_try (string path);

    [CCode (cname = "pamac_aur_build_lock_release", cheader_filename = "aur_build_lock.h")]
    internal extern void aur_build_lock_release (int fd);

    // One advisory lock per build directory, shared by all Pamac processes.
    // The file is a sibling of the build directory, not a disposable child.
    internal class AurBuildLock : Object {
        string lock_path;
        int fd = -1;

        public AurBuildLock (string build_dir) {
            lock_path = build_dir + ".pamac-aur.lock";
        }

        // 1 = acquired, 0 = another process holds it, -1 = hard failure.
        public int try_acquire () {
            if (fd >= 0) {
                return 1;
            }
            // The configured build directory may not exist yet. The lock is
            // alongside it, so make sure its parent exists before opening it.
            if (DirUtils.create_with_parents (Path.get_dirname (lock_path), 0700) != 0) {
                warning ("Cannot create directory for Pamac AUR lock: %s", lock_path);
                return -1;
            }
            int result = aur_build_lock_try (lock_path);
            if (result >= 0) {
                fd = result;
                return 1;
            }
            return result == -2 ? 0 : -1;
        }

        public async bool acquire_async (Cancellable? cancellable = null) {
            while (true) {
                if (cancellable != null && cancellable.is_cancelled ()) {
                    return false;
                }
                int result = try_acquire ();
                if (result == 1) {
                    return true;
                }
                if (result < 0) {
                    warning ("Could not acquire Pamac AUR lock: %s", lock_path);
                    return false;
                }
                Timeout.add (200, () => {
                    acquire_async.callback ();
                    return false;
                });
                yield;
            }
        }

        public void release () {
            if (fd >= 0) {
                aur_build_lock_release (fd);
                fd = -1;
            }
        }

        ~AurBuildLock () {
            release ();
        }
    }
}
