import pytest

from yarbo_local.exceptions import CommandRefusedError
from yarbo_local.registry import Registry


@pytest.fixture(scope="module")
def registry() -> Registry:
    return Registry.load()


def test_loads_verified_and_candidates(registry: Registry) -> None:
    assert "get_device_msg" in registry
    assert registry.get("get_device_msg").verified
    assert registry.get("get_device_msg").evidence
    assert not registry.get("start_plan").verified
    assert len(registry.verified) >= 10


def test_forbidden_are_never_registered(registry: Registry) -> None:
    for name in ("shell_cmd", "erase_map", "cmd_vel", "del_all_plan", "software_update"):
        assert registry.is_forbidden(name)
        assert name not in registry
        with pytest.raises(CommandRefusedError):
            registry.check(name, allow_candidates=True, confirmed=True)


def test_candidates_need_opt_in(registry: Registry) -> None:
    with pytest.raises(CommandRefusedError, match="candidate"):
        registry.check("start_plan", allow_candidates=False, confirmed=False)
    assert registry.check("start_plan", allow_candidates=True, confirmed=False).name == "start_plan"


def test_confirm_risk_needs_flag(registry: Registry) -> None:
    with pytest.raises(CommandRefusedError, match="confirmed"):
        registry.check("shutdown", allow_candidates=True, confirmed=False)
    assert registry.check("shutdown", allow_candidates=True, confirmed=True).risk == "confirm"


def test_unknown_command(registry: Registry) -> None:
    with pytest.raises(CommandRefusedError, match="not a registered"):
        registry.get("frobnicate")
