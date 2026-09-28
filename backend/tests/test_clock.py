import pytest

from app.clock import FakeClock, SystemClock


def test_fake_clock_advances(clock: FakeClock) -> None:
    start = clock.now()
    clock.advance(2.5)
    assert clock.now() == start + 2.5


def test_fake_clock_rejects_negative(clock: FakeClock) -> None:
    with pytest.raises(ValueError):
        clock.advance(-1)


def test_system_clock_returns_epoch_seconds() -> None:
    assert SystemClock().now() > 1_600_000_000
