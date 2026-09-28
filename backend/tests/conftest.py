import copy
from types import SimpleNamespace

import pytest
from helpers import WARMED, ManagedGenRig, outage

from app.clock import FakeClock


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=1_000_000.0)


# ---- pipelines shared by the generator-backed tests (each scenario runs once per session) ----
# Only slow tests use them, at the generator's realistic traffic volumes.


@pytest.fixture(scope="session")
def warmed() -> ManagedGenRig:
    """The whole pipeline after WARMED seconds of normal traffic. Never mutated: scenarios
    run on deep copies of it."""
    rig = ManagedGenRig(seed=7)
    rig.run(WARMED)
    return rig


@pytest.fixture(scope="session")
def db_outage(warmed: ManagedGenRig) -> SimpleNamespace:
    return outage(warmed, "db_degradation", 75)


@pytest.fixture(scope="session")
def auth_outage(warmed: ManagedGenRig) -> SimpleNamespace:
    return outage(warmed, "auth_outage", 50)


@pytest.fixture(scope="session")
def silent_outage(warmed: ManagedGenRig) -> SimpleNamespace:
    return outage(warmed, "silent_service", 60)


@pytest.fixture(scope="session")
def blip_run(warmed: ManagedGenRig) -> ManagedGenRig:
    rig = copy.deepcopy(warmed)
    rig.gen.start_scenario("noisy_blip")
    rig.run(70)  # the blip stays in the 60s window for 60s: this covers all of it
    return rig
