"""FD-confined, create-only private bundles. No path supplied by a form is opened."""
import os
from pathlib import PurePosixPath
import stat

from atraxis.installation.validation import InstallError, identifier, require

INSTALLATION_ROOT = '/var/lib/atraxis/installations'
MAX_FILE_BYTES = 262144
MAX_BUNDLE_BYTES = 2097152
MAX_BUNDLE_FILES = 54
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def open_root():
    """Open the fixed, pre-provisioned root, refusing symlinks at every component."""
    fd = os.open('/', DIRECTORY_FLAGS)
    try:
        for part in PurePosixPath(INSTALLATION_ROOT).parts[1:]:
            child = os.open(part, DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd); fd = child
            info = os.fstat(fd)
            require(info.st_uid in (0, os.geteuid()) and not stat.S_IMODE(info.st_mode) & 0o022,
                    'unsafe_root', 'destination')
        return Root(fd)
    except Exception:
        os.close(fd)
        raise InstallError('unsafe_root', 'destination') from None


class Root:
    """Trusted directory capability; frontend input may select only a child slug.

    Constructor takes ownership of an already-open trusted directory FD. This is
    not a plan field or CLI path override. It also makes disposable tests possible.
    """
    def __init__(self, fd):
        self.fd = fd
        self._check()

    def _check(self):
        try:
            info = os.fstat(self.fd)
        except OSError:
            raise InstallError('unsafe_root', 'destination') from None
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
                and stat.S_IMODE(info.st_mode) == 0o700, 'unsafe_root', 'destination')

    def close(self):
        os.close(self.fd)

    def create(self, name, files):
        identifier(name)
        self._check()  # Recheck mode/ownership immediately at the write boundary.
        allowed = {'plan.json', 'login.json', 'server.crt', 'server.key', 'complete.json'}
        require(type(files) is dict and 1 <= len(files) <= MAX_BUNDLE_FILES
                and 'complete.json' in files, 'invalid_bundle')
        files = dict(files)
        total = 0
        for filename, data in files.items():
            require(type(filename) is str and '/' not in filename and '\\' not in filename, 'invalid_bundle')
            if filename not in allowed:
                require(filename.startswith('source-') and filename.endswith(('.json', '.ca.pem')), 'invalid_bundle')
                suffix = '.ca.pem' if filename.endswith('.ca.pem') else '.json'
                identifier(filename[len('source-'):-len(suffix)])
            require(type(data) is bytes and 0 < len(data) <= MAX_FILE_BYTES, 'invalid_bundle')
            total += len(data)
        require(total <= MAX_BUNDLE_BYTES, 'invalid_bundle')
        fd = None
        complete = False
        try:
            # mkdir is exclusive: no existing directory, file or symlink is reused.
            os.mkdir(name, 0o700, dir_fd=self.fd)
            fd = os.open(name, DIRECTORY_FLAGS, dir_fd=self.fd)
            info = os.fstat(fd)
            require(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700, 'unsafe_root')
            order = [key for key in files if key != 'complete.json'] + ['complete.json']
            for filename in order:
                if filename == 'complete.json': complete = True
                out = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                              0o600, dir_fd=fd)
                with os.fdopen(out, 'wb') as stream:
                    stream.write(files[filename]); stream.flush(); os.fsync(stream.fileno())
            os.fsync(fd); os.fsync(self.fd)
        except FileExistsError:
            raise InstallError('destination_exists', 'destination') from None
        except (OSError, ValueError):
            raise InstallError('publication_uncertain' if complete else 'write_failed', 'destination') from None
        finally:
            if fd is not None: os.close(fd)
        # Failures preserve partial private state. Never delete/retry/reuse it automatically.
        return {'status': 'prepared', 'name': name, 'services_started': False}
