#ifndef PAMAC_AUR_BUILD_LOCK_H
#define PAMAC_AUR_BUILD_LOCK_H

/* Returns a held descriptor (>= 0), -2 if busy, -1 on other errors.
 * Keep the returned descriptor open for the entire protected operation.
 * The lock file must never be unlinked; it lives beside the build directory.
 */
int pamac_aur_lock_fd_try (const char *path);
/* Allow concurrent read-only metadata reads, but keep build/source writes exclusive. */
int pamac_aur_lock_fd_try_shared (const char *path);
void pamac_aur_lock_fd_release (int fd);

#endif
