"""OS-held, per-user exclusion for account-changing commands.

The observed account profile has no stable account ID. Serialize all managed
sessions for a user rather than keying by a token or a copyable config path.
Never unlink the lock file: waiters must continue to lock the same inode.
"""
import errno
import os
from pathlib import Path


def lock_path():
    if os.name == 'posix':
        import pwd
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    else:
        home = Path.home()
    return home / '.cache' / 'xianniu-linux' / 'account.lock'


class AccountLock:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else lock_path()
        self.fd = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == 'nt':
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b'\0')
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(fd)
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise ValueError('Another account operation or acceleration session is running. '
                                 'Exit it normally before starting, logging in or retrying stop.') from None
            raise
        self.fd = fd
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
