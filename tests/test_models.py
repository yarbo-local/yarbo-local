import json
from pathlib import Path

from yarbo_local.models import (
    Activity,
    Feedback,
    Heartbeat,
    RobotState,
    deep_merge,
    parse_gga,
    parse_gps_ref,
    parse_plans,
    parse_site_map,
)
from yarbo_local.simulator import load_snapshot

FIXTURES = Path(__file__).resolve().parents[1] / "protocol" / "fixtures" / "3.14.11"


def _snapshot() -> dict:  # type: ignore[type-arg]
    _, snap = load_snapshot(FIXTURES / "get_device_msg-asleep.jsonl")
    return snap


def test_parse_gga() -> None:
    fix = parse_gga("$GNGGA,132203.00,4807.0380,N,01131.0000,E,4,12,0.9,545.4,M,46.9,M,,*47")
    assert fix is not None
    assert round(fix.latitude, 4) == 48.1173
    assert round(fix.longitude, 4) == 11.5167
    assert fix.quality == 4
    assert fix.rtk_fixed
    assert fix.satellites == 12
    south = parse_gga("$GPGGA,1,4807.0380,S,01131.0000,W,1,8,1.0,10.0,M,0,M,,")
    assert south is not None
    assert south.latitude < 0
    assert south.longitude < 0
    assert parse_gga("") is None
    assert parse_gga("$GNRMC,1,2,3") is None


def test_state_from_real_snapshot() -> None:
    state, changed = RobotState().with_frame(_snapshot(), 1.0)
    assert changed
    assert state.firmware == "3.14.11"
    assert state.body_firmware == "3.2.26"
    assert state.head_firmware is None  # "0.0.0" means no head
    assert state.battery == 100
    assert state.battery_health == 100
    assert state.wired_charging  # recharge_state 3
    assert not state.wireless_charging  # BatteryMSG.status 1
    assert state.charging
    assert state.head_type == 0
    assert state.head_name == "none"
    assert not state.has_mower_head
    assert state.rtk_status == 1
    assert not state.rtk_usable
    assert state.position is not None
    assert round(state.position[0], 1) == 14.2
    assert state.fix is not None
    assert state.fix.quality == 1
    assert state.volume == 1.0
    assert state.person_detection is True
    assert state.child_lock is False
    assert state.network_path == "halow"
    assert state.error_code == 0
    assert state.activity is Activity.CHARGING


def test_activity_derivation() -> None:
    base, _ = RobotState().with_frame(_snapshot(), 1.0)
    asleep, _ = base.with_heartbeat(Heartbeat(0), 2.0)
    assert asleep.activity is Activity.CHARGING  # docked and charging wins over sleeping

    def with_state(**fields: object) -> RobotState:
        frame = {
            "StateMSG": dict(fields),
            "BodyMsg": {"recharge_state": 0},
            "BatteryMSG": {"status": 1},
        }
        state, _ = base.with_frame(frame, 3.0)
        return state

    assert with_state(on_going_planning=1).activity is Activity.WORKING
    assert with_state(on_going_planning=2).activity is Activity.CALCULATING_ROUTE
    assert with_state(on_going_planning=3).activity is Activity.HEADING_TO_AREA
    assert with_state(on_going_planning=11).activity is Activity.WAYPOINT
    assert with_state(on_going_planning=5).activity is Activity.COMPLETED
    assert with_state(on_going_planning=1, planning_paused=5).activity is Activity.PAUSED
    assert with_state(on_going_recharging=2).activity is Activity.RETURNING
    assert with_state(error_code=17).activity is Activity.ERROR
    idle = with_state(on_going_planning=0)
    assert idle.activity is Activity.IDLE
    sleeping, _ = idle.with_heartbeat(Heartbeat(0), 4.0)
    assert sleeping.activity is Activity.SLEEPING


def test_deep_merge_reports_changes() -> None:
    base = {"A": {"x": 1, "y": 2}, "b": 3}
    merged, changed = deep_merge(base, {"A": {"y": 2}})
    assert not changed
    assert merged == base
    merged, changed = deep_merge(base, {"A": {"y": 5}, "c": 1})
    assert changed
    assert merged["A"]["y"] == 5
    assert merged["c"] == 1
    assert base["A"]["y"] == 2


def test_feedback_decodes_blobs() -> None:
    fb = Feedback.from_wire({"topic": "get_map", "state": 0, "msg": "", "data": '{"areas": []}'})
    assert fb.ok
    assert fb.payload == {"areas": []}
    err = Feedback.from_wire({"topic": "x", "state": -2, "msg": "boom", "data": ""})
    assert not err.ok
    assert err.msg == "boom"


def test_site_map_from_fixture() -> None:
    rec = next(
        json.loads(line)
        for line in (FIXTURES / "get_map-asleep.jsonl").read_text().splitlines()
        if '"get_map"' in line and "data_feedback" in line
    )
    fb = Feedback.from_wire(rec["payload"])
    site = parse_site_map(fb.payload)
    assert site.zones == ()
    assert len(site.charging) == 1
    dock = site.charging[0]
    assert dock.id == 1
    assert dock.straight_phi is not None
    assert 2.9 < dock.straight_phi < 2.92
    assert dock.start_point is not None


def test_plans_and_gps_ref_shapes() -> None:
    assert parse_plans({"data": []}) == []
    assert parse_plans([]) == []
    plans = parse_plans(
        {"data": [{"id": 7, "name": "Front", "areaIds": [1, 2], "enable_self_order": False}]}
    )
    assert plans[0].id == 7
    assert plans[0].area_ids == (1, 2)
    assert plans[0].self_order is False
    ref = parse_gps_ref(
        {"ref": {"latitude": 10.5, "longitude": -20.25}, "hgt": 3.0, "rtkFixType": 1}
    )
    assert ref is not None
    assert ref.fixed
    assert ref.height == 3.0
    assert parse_gps_ref({"ref": {"latitude": 0, "longitude": 0}}) is None
