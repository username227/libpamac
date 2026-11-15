#include "aur_build_lock.h"

#include <errno.h>
#include <fcntl.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>

int
pamac_aur_lock_fd_try (const char *path)
{
    int fd = open (path, O_CREAT | O_RDWR | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (fd < 0)
        return -1;

    struct stat st;
    if (fstat (fd, &st) != 0 || !S_ISREG (st.st_mode) ||
        st.st_uid != geteuid ()) {
        close (fd);
        return -1;
    }

    if (flock (fd, LOCK_EX | LOCK_NB) == 0)
        return fd;

    int saved_errno = errno;
    close (fd);
    if (saved_errno == EWOULDBLOCK || saved_errno == EAGAIN)
        return -2;
    return -1;
}

void
pamac_aur_lock_fd_release (int fd)
{
    if (fd >= 0)
        close (fd);
}
