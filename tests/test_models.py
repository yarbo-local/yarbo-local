import json
from pathlib import Path

from yarbo_local.models import (
    METERS_PER_DEGREE,
    Activity,
    Fault,
    Feedback,
    Heartbeat,
    RobotState,
    deep_merge,
    local_to_wgs84,
    parse_area_params,
    parse_gga,
    parse_gps_ref,
    parse_plans,
    parse_site_map,
    wgs84_to_local,
)
from yarbo_local.simulator import load_site_map, load_snapshot

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
    assert not state.charging  # BatteryMSG.status 1, charging_status 0: off the charger
    assert state.battery_current == -0.3  # discharging at rest
    assert state.battery_voltage == 41.0
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
    assert state.activity is Activity.IDLE


def test_activity_derivation() -> None:
    base, _ = RobotState().with_frame(_snapshot(), 1.0)
    asleep, _ = base.with_heartbeat(Heartbeat(0), 2.0)
    assert asleep.activity is Activity.SLEEPING
    charging, _ = base.with_frame(
        {"StateMSG": {"charging_status": 2}, "BatteryMSG": {"status": 3}}, 2.5
    )
    assert charging.charging
    assert charging.activity is Activity.CHARGING
    # On the dock but not charging: before charging starts (1), full (0), held for rain (6).
    for value in (0, 1, 6):
        docked, _ = base.with_frame(
            {"StateMSG": {"charging_status": value}, "BatteryMSG": {"status": 3}}, 2.6
        )
        assert not docked.charging, value
    # 2026-09-26: a start held on the dock for rain reads paused 9 and charging_status 6.
    rain, _ = base.with_frame(
        {
            "StateMSG": {"on_going_planning": 0, "planning_paused": 9, "charging_status": 6},
            "BatteryMSG": {"status": 1},
        },
        2.7,
    )
    assert rain.pause_reason == "rain"
    assert rain.activity is Activity.PAUSED
    # Without charging_status, the battery's own status decides.
    bare = RobotState()
    bare, _ = bare.with_frame({"BatteryMSG": {"status": 3}}, 2.8)
    assert bare.charging

    def with_state(**fields: object) -> RobotState:
        frame = {
            "StateMSG": dict(fields),
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


def test_frame_conversion_roundtrip_and_axes() -> None:
    ref = (42.0, -71.0)
    lat, lon = local_to_wgs84(ref, 10.0, 0.0)
    assert lat == 42.0
    assert lon < -71.0  # +x is west
    lat, lon = local_to_wgs84(ref, 0.0, 10.0)
    assert lat > 42.0  # +y is north
    assert lon == -71.0
    x, y = wgs84_to_local(ref, *local_to_wgs84(ref, -12.5, 33.25))
    assert round(x, 6) == -12.5
    assert round(y, 6) == 33.25


def test_real_map_with_area_and_pathway() -> None:
    site = parse_site_map(load_site_map(FIXTURES / "get_map-area-pathway.jsonl"))
    assert [(z.family, z.id, z.name) for z in site.zones] == [
        ("areas", 1, "Area 1"),
        ("pathways", 2, "Pathway 1"),
    ]
    area, pathway = site.areas[0], site.pathways[0]
    assert len(area.points) == 25
    assert area.closed
    assert area.area_m2 is not None
    assert round(area.area_m2, 1) == 126.4
    assert not pathway.closed
    assert pathway.area_m2 is None
    assert round(pathway.length_m, 1) == 16.9
    assert pathway.start_id == 1
    assert len(site.charging) == 1
    assert site.reference is not None
    bounds = site.bounds()
    assert bounds is not None
    assert bounds[3] - bounds[1] > 30  # the driveway runs about 34 m north-south

    geo = site.to_geojson()
    assert geo["type"] == "FeatureCollection"
    kinds = {f["properties"]["family"]: f["geometry"]["type"] for f in geo["features"]}
    assert kinds == {"areas": "Polygon", "pathways": "LineString", "dock": "Point"}
    ring = next(f for f in geo["features"] if f["properties"]["family"] == "areas")["geometry"]
    assert ring["coordinates"][0][0] == ring["coordinates"][0][-1]
    assert len(ring["coordinates"][0]) == 26
    summary = site.summary()
    assert summary["zones"][0]["area_m2"] == 126.4
    assert "ref" not in str(summary)


def test_map_frame_orientation_against_rtk_in_the_mapping_fixture() -> None:
    """y is north in metres; x grows as longitude falls (west).

    Redaction shifts latitude and longitude by whole degrees, which keeps latitude
    differences in metres exact but not longitude differences, so only the north
    axis is checked metrically and the east-west axis by sign.
    """
    records = [
        json.loads(line)
        for line in (FIXTURES / "mapping-area-app.jsonl").read_text().splitlines()
        if line.strip()
    ]
    ref = next(
        (r["payload"]["ref"]["latitude"], r["payload"]["ref"]["longitude"])
        for r in records
        if r["topic"].endswith("/app/save_clean_area")
    )
    checked = 0
    for rec in records:
        if not rec["topic"].endswith("/device/DeviceMSG"):
            continue
        state, _ = RobotState().with_frame(rec["payload"], 0.0)
        fix, pos = state.fix, state.position
        if fix is None or pos is None or fix.quality != 4:
            continue
        north = (fix.latitude - ref[0]) * METERS_PER_DEGREE
        assert abs(north - pos[1]) < 0.5
        if abs(pos[0]) > 3:
            assert (fix.longitude - ref[1]) * pos[0] < 0  # x and east have opposite signs
        checked += 1
    assert checked > 50


def test_area_params_rejects_defaults_for_unknown_id() -> None:
    assert parse_area_params({"id": 1, "gap": 0.3}, 1) == {"id": 1, "gap": 0.3}
    assert parse_area_params({"id": 0, "gap": 0.3}, 99) is None
    assert parse_area_params("nope", 1) is None


def test_fault_902_from_the_real_tilt() -> None:
    """The robot sent 902 when the app said "Tilted or flipped over"; say that, not "error"."""
    state = RobotState()
    seen: list[tuple[int, str | None, str | None]] = []
    for line in (FIXTURES / "mower-pro-fault-902-tilted.jsonl").read_text().splitlines():
        rec = json.loads(line)
        if not rec["topic"].endswith("/device/DeviceMSG"):
            continue
        state, _ = state.with_frame(rec["payload"], rec["t"])
        fault = state.fault
        seen.append((state.error_code, fault.description if fault else None, state.pause_reason))
    assert seen[0] == (0, None, None)
    assert (902, "Tilted or flipped over", "fault") in seen
    assert seen[-1] == (0, None, None)


def test_fault_wording() -> None:
    assert Fault.from_code(0) is None
    tilted = Fault.from_code(902)
    assert tilted is not None
    assert tilted.identified
    assert tilted.key == "tilted"
    unknown = Fault.from_code(901)
    assert unknown is not None
    assert not unknown.identified
    assert unknown.description == "Fault 901"
    assert "Yarbo app" in unknown.hint
    base, _ = RobotState().with_frame({"StateMSG": {"planning_paused": 6}}, 1.0)
    assert base.pause_reason == "stuck"
    odd, _ = base.with_frame({"StateMSG": {"planning_paused": 42}}, 2.0)
    assert odd.pause_reason == "unknown"


def _frame(x: float, y: float, **state: int) -> dict:  # type: ignore[type-arg]
    return {"CombinedOdom": {"x": x, "y": y, "phi": 0.0}, "StateMSG": state}


def test_a_running_plan_that_does_not_move_is_waiting() -> None:
    """As with the car on the pathway: planning 3, no pause code, no error, standing still."""
    state = RobotState()
    state, _ = state.with_frame(_frame(0.0, 0.0, on_going_planning=3), 100.0)
    assert not state.waiting
    state, _ = state.with_frame(_frame(5.0, 0.0, on_going_planning=3), 110.0)
    for at in (120.0, 150.0, 169.0):
        state, _ = state.with_frame(_frame(5.2, 0.1, on_going_planning=3), at)
    assert state.still_seconds == 59.0
    assert not state.waiting, "under a minute is a turn or a short stop"
    state, changed = state.with_frame(_frame(5.2, 0.1, on_going_planning=3), 171.0)
    assert state.waiting
    assert changed, "listeners hear about it even though the frame itself is the same"
    assert state.activity is Activity.WAITING

    crept, _ = state.with_frame(_frame(5.8, 0.1, on_going_planning=3), 200.0)
    assert crept.waiting, "a creep of under a metre is still waiting"
    moved, changed = state.with_frame(_frame(7.0, 0.1, on_going_planning=3), 200.0)
    assert not moved.waiting
    assert changed
    assert moved.activity is Activity.HEADING_TO_AREA


def test_waiting_needs_a_running_plan_without_a_pause_code() -> None:
    def still(**codes: int) -> RobotState:
        state, _ = RobotState().with_frame(_frame(1.0, 1.0, **codes), 0.0)
        state, _ = state.with_frame(_frame(1.0, 1.0, **codes), 120.0)
        return state

    assert still(on_going_planning=1).waiting
    # Hours on the dock, then a start: stillness counts only from when the plan drives.
    parked, _ = RobotState().with_frame(_frame(1.0, 1.0, on_going_planning=0), 0.0)
    parked, _ = parked.with_frame(_frame(1.0, 1.0, on_going_planning=0), 7200.0)
    started, _ = parked.with_frame(_frame(1.0, 1.0, on_going_planning=3), 7201.0)
    assert not started.waiting
    assert started.still_seconds == 1.0
    resumed, _ = still(on_going_planning=0, planning_paused=1).with_frame(
        _frame(1.0, 1.0, on_going_planning=3, planning_paused=0), 121.0
    )
    assert not resumed.waiting, "a resume starts the clock again"
    assert not still(on_going_planning=0).waiting, "at rest on the dock"
    assert not still(on_going_planning=0, planning_paused=1).waiting, "paused says why"
    assert not still(on_going_planning=2).waiting, "calculating the route"
    assert RobotState().still_seconds is None


def test_stop_button() -> None:
    """2026-09-15: 1 at the press, 3 three seconds later, 0 once it was pulled out."""
    assert RobotState().stop_button_engaged is None

    def button(value: int) -> RobotState:
        return RobotState().with_frame({"BodyMsg": {"body_stop_button_state": value}}, 0.0)[0]

    assert button(0).stop_button_engaged is False
    assert button(1).stop_button_engaged is True
    assert button(3).stop_button_engaged is True
    assert button(3).stop_button_state == 3


def test_the_real_blocked_pathway_reads_as_waiting() -> None:
    """2026-10-04: a car stood on the pathway. The plan said running; the robot stood still."""
    state = RobotState()
    started = None
    waiting_at = None
    for line in (FIXTURES / "pathway-blocked-waiting.jsonl").read_text().splitlines():
        record = json.loads(line)
        if not record["topic"].endswith("/DeviceMSG"):
            continue
        started = started or record["t"]
        state, _ = state.with_frame(record["payload"], record["t"])
        if state.waiting and waiting_at is None:
            waiting_at = record["t"] - started
    assert state.planning_code == 3
    assert state.paused_code == 0
    assert state.error_code == 0, "the robot itself reports nothing wrong"
    assert state.waiting
    assert state.activity is Activity.WAITING
    assert waiting_at is not None
    assert 100 < waiting_at < 125, "it drove for about a minute, then stood for a minute"
