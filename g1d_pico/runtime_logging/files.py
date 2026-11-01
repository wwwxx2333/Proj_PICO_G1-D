"""Serialize retention and protect active logs with flock; never follow symlinks."""
from datetime import datetime
import fcntl
import os
from pathlib import Path
import re
import stat
import uuid

NAME = re.compile(r'run_\d{8}_\d{6}_\d{6}_[0-9a-f]{8}\.log\Z')


def create_log(directory: Path, max_files: int):
    directory.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(directory / '.rotation.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        candidates = []
        for path in directory.iterdir():
            if NAME.fullmatch(path.name):
                info = path.lstat()
                if stat.S_ISREG(info.st_mode):
                    candidates.append((info.st_mtime_ns, path.name, path))
        count, removed = len(candidates), 0
        for _, _, path in sorted(candidates):
            if count < max_files:
                break
            fd = None
            try:
                fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if path.lstat().st_ino != os.fstat(fd).st_ino:
                    continue
                path.unlink()
                count -= 1
                removed += 1
            except BlockingIOError:
                pass  # Active session, even if older than every closed log.
            except FileNotFoundError:
                count -= 1
            finally:
                if fd is not None:
                    os.close(fd)
        if count >= max_files:
            raise RuntimeError('All available log slots are active; refusing to delete active logs or exceed max_files')
        filename = datetime.now().strftime('run_%Y%m%d_%H%M%S_%f_') + uuid.uuid4().hex[:8] + '.log'
        path = directory / filename
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            handle = os.fdopen(fd, 'w', encoding='utf-8', buffering=65536)
        except BaseException:
            os.close(fd)
            raise
        return path, handle, removed
    finally:
        os.close(lock_fd)
