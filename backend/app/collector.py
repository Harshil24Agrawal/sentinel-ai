"""Tail the log file: partial lines, truncation, rotation."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_PARTIAL_BYTES = 1 << 20  # a "line" this long without a newline is flushed, not buffered forever


class LogCollector:
    """Polls a growing file and emits complete lines.

    The file is opened and closed on every poll (portable, no inotify, and it never blocks
    the writer or a rename on Windows). ``poll_once`` does one poll synchronously, which is
    what tests drive; ``run`` loops it and feeds an asyncio queue.
    """

    def __init__(
        self,
        path: str | Path,
        queue: asyncio.Queue[str] | None = None,
        *,
        from_start: bool = False,
        poll_seconds: float = 0.1,
    ) -> None:
        self._path = Path(path)
        self.queue: asyncio.Queue[str] = queue if queue is not None else asyncio.Queue()
        self._poll_seconds = poll_seconds
        self._start_at_end = not from_start  # only for the file present at startup
        self._ident: tuple[int, int] | None = None
        self._offset = 0
        self._buffer = b""
        self._warned_missing = False

    def poll_once(self) -> list[str]:
        try:
            f = open(self._path, "rb")
        except FileNotFoundError:
            if not self._warned_missing:
                logger.warning("log file %s is missing; waiting for it to appear", self._path)
                self._warned_missing = True
            self._ident, self._offset, self._buffer = None, 0, b""
            self._start_at_end = False  # a file that appears later is read from its beginning
            return []
        try:
            st = os.fstat(f.fileno())
            ident = (st.st_dev, st.st_ino)
            if self._warned_missing:
                logger.info("log file %s appeared", self._path)
                self._warned_missing = False
            if self._ident is None:
                self._offset = st.st_size if self._start_at_end else 0
                self._start_at_end = False
            elif ident != self._ident:
                logger.info(
                    "log file %s was rotated; reading the new file from the start", self._path
                )
                self._offset, self._buffer = 0, b""
            elif st.st_size < self._offset:
                logger.info("log file %s was truncated; restarting at offset 0", self._path)
                self._offset, self._buffer = 0, b""
            self._ident = ident
            f.seek(self._offset)
            data = f.read()
        finally:
            f.close()
        if not data:
            return []
        self._offset += len(data)
        *complete, self._buffer = (self._buffer + data).split(b"\n")
        if len(self._buffer) > MAX_PARTIAL_BYTES:
            complete.append(self._buffer)
            self._buffer = b""
        lines = (chunk.decode("utf-8", errors="replace").rstrip("\r") for chunk in complete)
        return [line for line in lines if line.strip()]

    async def run(self) -> None:
        while True:
            try:
                lines = self.poll_once()
            except OSError as exc:  # e.g. a transient permission error; keep monitoring
                logger.warning("log read failed: %s", type(exc).__name__)
                lines = []
            for line in lines:
                await self.queue.put(line)
            await asyncio.sleep(self._poll_seconds)
