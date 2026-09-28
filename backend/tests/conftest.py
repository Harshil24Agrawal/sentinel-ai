import pytest

from app.clock import FakeClock


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=1_000_000.0)
