"""Private POSIX lock shared by account storage and inventory profiles."""
from contextlib import contextmanager
import fcntl
import os
import stat
from atraxis.accounts import AccountError

@contextmanager
def private_lock(path):
    if os.getuid() != os.geteuid():
        raise AccountError('account_store_not_private')
    metadata = path.parent.lstat()
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077):
        raise AccountError('account_store_not_private')
    descriptor = os.open(str(path) + '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(descriptor, 'r+b') as lock:
        metadata = os.fstat(lock.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077 or metadata.st_nlink != 1):
            raise AccountError('account_store_not_private')
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield

