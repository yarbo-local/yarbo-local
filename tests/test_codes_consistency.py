"""The constants in models.py must agree with protocol/codes.yaml."""

from pathlib import Path

import yaml

from yarbo_local import models

CODES = yaml.safe_load(
    (Path(__file__).resolve().parents[1] / "protocol" / "codes.yaml").read_text(encoding="utf-8")
)


def _ints(table: dict[object, object]) -> dict[int, object]:
    return {k: v for k, v in table.items() if isinstance(k, int)}


def test_head_types_match() -> None:
    assert _ints(CODES["HeadMsg.head_type"]) == models.HEAD_TYPES


def test_rtk_usable_matches_plan_start_rule() -> None:
    assert set(CODES["RTKMSG.status"]["plan_start_requires"]) == models.RTK_USABLE


def test_wired_charging_states_match() -> None:
    assert set(CODES["BodyMsg.recharge_state"]["blocks_plan_start"]) == models.WIRED_CHARGING_STATES


def test_planning_codes_are_known() -> None:
    known = set(_ints(CODES["StateMSG.on_going_planning"]))
    assert models.PLANNING_RUNNING <= known
    assert models.PLANNING_COMPLETED <= known
    # 12 (waypoint_complete) is treated as completed, not running, on purpose.
    assert 12 in models.PLANNING_COMPLETED


def test_recharging_in_transit_matches_rule() -> None:
    table = CODES["StateMSG.on_going_recharging"]
    expected = {k for k in _ints(table) if k > 0 and k != 4}
    assert expected == models.RECHARGING_IN_TRANSIT
