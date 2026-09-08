"""Reentrant process-wide advisory locks using the available OS implementation."""

from __future__ import annotations

import errno
import os
import threading
import time
from pathlib import Path
from typing import BinaryIO, Dict, Optional, Tuple

from . import errors

try:
    import fcntl
except ImportError:
    fcntl = None

try:
    import msvcrt
except ImportError:
    msvcrt = None


if os.name == "nt" and msvcrt is not None:
    _backend = "msvcrt"
elif fcntl is not None:
    _backend = "fcntl"
elif msvcrt is not None:
    _backend = "msvcrt"
else:
    _backend = "noop"

_locks: Dict[str, Tuple[BinaryIO, int]] = {}
_locks_guard = threading.Lock()


def _acquire(handle: BinaryIO) -> None:
    if _backend == "msvcrt":
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    elif _backend == "fcntl":
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release(handle: BinaryIO) -> None:
    if _backend == "msvcrt":
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    elif _backend == "fcntl":
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class FileLock:
    """Hold an advisory lock, counting nested acquisitions by resolved path."""

    def __init__(self, path: Path, timeout: float = 10.0, poll: float = 0.05):
        self.path = Path(path).expanduser().resolve()
        self.timeout = timeout
        self.poll = poll
        self._key = os.path.normcase(str(self.path))
        self._depth = 0

    def __enter__(self) -> FileLock:
        deadline = time.monotonic() + self.timeout
        handle: Optional[BinaryIO] = None
        try:
            while True:
                with _locks_guard:
                    held = _locks.get(self._key)
                    if held is not None:
                        shared_handle, count = held
                        _locks[self._key] = (shared_handle, count + 1)
                        self._depth += 1
                        return self
                    if handle is None:
                        self.path.parent.mkdir(parents=True, exist_ok=True)
                        handle = self.path.open("a+b")
                        if _backend == "msvcrt":
                            # Windows locks a byte starting at the current offset.
                            handle.seek(0, os.SEEK_END)
                            if handle.tell() == 0:
                                handle.write(b"\0")
                                handle.flush()
                    try:
                        _acquire(handle)
                    except OSError as exc:
                        if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                            raise
                    else:
                        _locks[self._key] = (handle, 1)
                        handle = None  # The registry owns this handle until final exit.
                        self._depth += 1
                        return self
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise errors.LockBusy(f"Timed out waiting for lock: {self.path}")
                time.sleep(min(max(self.poll, 0.0), remaining))
        finally:
            if handle is not None:
                handle.close()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        with _locks_guard:
            if self._depth == 0:
                return
            self._depth -= 1
            handle, count = _locks[self._key]
            if count > 1:
                _locks[self._key] = (handle, count - 1)
                return
            try:
                _release(handle)
            finally:
                try:
                    handle.close()
                finally:
                    del _locks[self._key]
