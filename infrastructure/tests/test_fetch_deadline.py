import pytest

from tool_gateway_service.tools import fetch_public


def test_fetch_process_cannot_outlive_wall_time_budget():
    with pytest.raises(TimeoutError, match="wall-time"):
        fetch_public("https://127.0.0.1", timeout=0.0001)


def test_isolated_fetch_still_rejects_private_addresses():
    with pytest.raises(ValueError, match="Private or non-public"):
        fetch_public("https://127.0.0.1", timeout=10)
