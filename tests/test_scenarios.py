"""The demo scenarios are the acceptance tests: each must pass from an empty state."""

import pytest

from switchyard.demo.scenarios import SCENARIOS, run_scenario


@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_scenario(name, tmp_path):
    result = await run_scenario(name, tmp_path / name)
    failed = [c.description for c in result.checks if not c.passed]
    assert result.error is None, result.error
    assert result.passed, f"failed checks: {failed}"
