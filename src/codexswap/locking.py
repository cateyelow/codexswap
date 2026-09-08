"""Advisory locks that exclude other processes and other threads alike."""

from __future__ import annotations

import errno
import os
import threading
import time
from pathlib import Path
from typing import BinaryIO, Dict, NamedTuple, Optional

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


class _Held(NamedTuple):
    handle: BinaryIO
    owner: int  # threading.get_ident() of the thread inside the critical section
    depth: int


_locks: Dict[str, _Held] = {}
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
    """Exclude other processes with an OS lock, other threads with the registry.

    Reentrancy is per thread and per resolved path, so nesting the same path in one
    call stack succeeds while a second thread waits for its turn. The registry, not
    the OS lock, is what keeps threads apart: POSIX record locks are owned by the
    process, so a same-process contender can silently pass them.
    """

    def __init__(self, path: Path, timeout: float = 10.0, poll: float = 0.05):
        self.path = Path(path).expanduser().resolve()
        self.timeout = timeout
        self.poll = poll
        self._key = os.path.normcase(str(self.path))
        # Keyed by thread: one instance may be entered from several threads in turn.
        self._depths: Dict[int, int] = {}

    def __enter__(self) -> FileLock:
        deadline = time.monotonic() + self.timeout
        owner = threading.get_ident()
        handle: Optional[BinaryIO] = None
        try:
            while True:
                with _locks_guard:
                    held = _locks.get(self._key)
                    if held is not None and held.owner == owner:
                        _locks[self._key] = held._replace(depth=held.depth + 1)
                        self._depths[owner] = self._depths.get(owner, 0) + 1
                        return self
                    if held is None:
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
                            _locks[self._key] = _Held(handle, owner, 1)
                            handle = None  # The registry owns this handle until final exit.
                            self._depths[owner] = self._depths.get(owner, 0) + 1
                            return self
                    # Another thread of this process holds it, or another process does.
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise errors.LockBusy(f"Timed out waiting for lock: {self.path}")
                time.sleep(min(max(self.poll, 0.0), remaining))
        finally:
            if handle is not None:
                handle.close()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        owner = threading.get_ident()
        with _locks_guard:
            if self._depths.get(owner, 0) == 0:
                return
            self._depths[owner] -= 1
            if self._depths[owner] == 0:
                del self._depths[owner]
            held = _locks[self._key]
            if held.depth > 1:
                _locks[self._key] = held._replace(depth=held.depth - 1)
                return
            try:
                _release(held.handle)
            finally:
                try:
                    held.handle.close()
                finally:
                    del _locks[self._key]
