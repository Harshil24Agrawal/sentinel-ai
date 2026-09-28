import asyncio
import logging
import os
from pathlib import Path

import pytest

from app.collector import LogCollector


def append(path: Path, text: str) -> None:
    with open(path, "ab") as f:
        f.write(text.encode())


@pytest.fixture
def log(tmp_path: Path) -> Path:
    return tmp_path / "app.log"


def running(log: Path, **kwargs: float) -> LogCollector:
    """A collector that started against an empty file, i.e. already tailing it."""
    log.write_text("")
    c = LogCollector(log, **kwargs)
    assert c.poll_once() == []
    return c


def test_appended_lines_are_read(log: Path) -> None:
    c = running(log)
    assert c.poll_once() == []
    append(log, "one\ntwo\n")
    assert c.poll_once() == ["one", "two"]
    append(log, "three\n")
    assert c.poll_once() == ["three"]
    assert c.poll_once() == []


def test_starts_at_end_by_default(log: Path) -> None:
    log.write_text("old1\nold2\n")
    c = LogCollector(log)
    assert c.poll_once() == []
    append(log, "new\n")
    assert c.poll_once() == ["new"]


def test_from_start_reads_existing_content(log: Path) -> None:
    log.write_text("old1\nold2\n")
    assert LogCollector(log, from_start=True).poll_once() == ["old1", "old2"]


def test_partial_line_is_held_until_newline(log: Path) -> None:
    c = running(log)
    append(log, "complete\npart")
    assert c.poll_once() == ["complete"]
    append(log, "ial line")
    assert c.poll_once() == []
    append(log, " done\n")
    assert c.poll_once() == ["partial line done"]


def test_crlf_and_blank_lines(log: Path) -> None:
    c = running(log)
    append(log, "a\r\n\r\n\nb\r\n")
    assert c.poll_once() == ["a", "b"]


def test_invalid_utf8_does_not_crash(log: Path) -> None:
    c = running(log)
    with open(log, "ab") as f:
        f.write(b"bad \xff byte\n")  # a lone 0xff is not valid UTF-8
    (line,) = c.poll_once()
    assert line.startswith("bad ") and line.endswith(" byte")


def test_truncation_resets_offset(log: Path) -> None:
    c = running(log)
    append(log, "aaaaaaaaaa\nbbbbbbbbbb\n")
    assert len(c.poll_once()) == 2
    log.write_text("new\n")  # same file, now shorter than the stored offset
    assert c.poll_once() == ["new"]
    append(log, "after\n")
    assert c.poll_once() == ["after"]


def test_truncation_drops_a_stale_partial_line(log: Path) -> None:
    c = running(log)
    append(log, "stale partial without newline")
    assert c.poll_once() == []
    log.write_text("fresh\n")
    assert c.poll_once() == ["fresh"]


def test_rotation_reopens_and_reads_new_file_from_start(log: Path) -> None:
    c = running(log)
    append(log, "before\n")
    assert c.poll_once() == ["before"]
    os.replace(log, log.with_name("app.log.1"))
    log.write_text("fresh1\nfresh2\n")
    assert c.poll_once() == ["fresh1", "fresh2"]
    append(log, "fresh3\n")
    assert c.poll_once() == ["fresh3"]


def test_missing_file_is_waited_for_and_warned_once(
    log: Path, caplog: pytest.LogCaptureFixture
) -> None:
    c = LogCollector(log)
    with caplog.at_level(logging.WARNING, logger="app.collector"):
        assert c.poll_once() == []
        assert c.poll_once() == []
        assert c.poll_once() == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    log.write_text("hello\n")  # appears later: everything in it is new
    assert c.poll_once() == ["hello"]


def test_file_that_disappears_then_returns_is_read_from_start(log: Path) -> None:
    c = running(log)
    append(log, "x\n")
    assert c.poll_once() == ["x"]
    log.unlink()
    assert c.poll_once() == []
    log.write_text("y\n")
    assert c.poll_once() == ["y"]


async def test_run_feeds_the_queue(log: Path) -> None:
    c = running(log, poll_seconds=0.01)
    task = asyncio.create_task(c.run())
    try:
        append(log, "alpha\nbeta\n")
        got = [await asyncio.wait_for(c.queue.get(), 2) for _ in range(2)]
        assert got == ["alpha", "beta"]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
