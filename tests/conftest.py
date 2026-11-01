from __future__ import annotations

import pytest

from switchyard.config import load_settings
from switchyard.engine import Engine


class Clock:
    def __init__(self, start: float = 1_793_000_000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def settings(tmp_path):
    return load_settings(
        tmp_path / "home", mode="offline", retry_base_seconds=1.0, retry_max_seconds=10.0, lease_seconds=30.0
    )


@pytest.fixture
def engine(settings, clock):
    eng = Engine(settings, clock=clock, worker_id="test-worker")
    yield eng
    eng.close()
