from __future__ import annotations

import fcntl
from pathlib import Path
from types import TracebackType


class SingleInstanceGuard:
    """Linux advisory lock protecting the supported single-writer SQLite deployment."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._file = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self._path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exception:
            self._file.close()
            self._file = None
            raise RuntimeError(
                "Another bot instance already owns the SQLite writer lock."
            ) from exception
        self._file.seek(0)
        self._file.truncate()
        self._file.write(str(self._path))
        self._file.flush()

    def release(self) -> None:
        if self._file is None:
            return
        fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        self._file.close()
        self._file = None

    def __enter__(self) -> SingleInstanceGuard:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        _ = exc_type, exc, traceback
        self.release()
