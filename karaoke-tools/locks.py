"""One lock for every read-modify-write of the wiki files and session state,
shared by all karaoke-tools processes: the MCP server (the agent) and the Mini
App server both write songs.csv, relations.csv and STATE_ROOT files.

Inside a process it's re-entrant (a thread may take it again, e.g. catalog_upsert
calling the feedback hook); across processes it's an fcntl.flock on
STATE_ROOT/wiki.lock, held while the outermost holder is inside.
"""

import fcntl
import os
import threading

from common import state_root

LOCK_FILE = "wiki.lock"


class SharedLock:
    def __init__(self):
        self._rlock = threading.RLock()
        self._depth = 0
        self._fd = None

    def __enter__(self):
        self._rlock.acquire()
        try:
            if self._depth == 0:
                root = state_root()
                root.mkdir(parents=True, exist_ok=True)
                fd = os.open(root / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)  # blocks until the other process is done
                self._fd = fd
            self._depth += 1
        except BaseException:
            self._rlock.release()
            raise
        return self

    def __exit__(self, *exc):
        try:
            self._depth -= 1
            if self._depth == 0 and self._fd is not None:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
                self._fd = None
        finally:
            self._rlock.release()
        return False


LOCK = SharedLock()
