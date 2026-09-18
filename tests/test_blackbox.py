"""The flight recorder: bounded, tells the story, and gives away nothing private."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from yarbo_local.blackbox import FlightRecorder
from yarbo_local.session import MessageEvent

SERIAL = "260799999X99Z999"


def event(at: float, side: str, leaf: str, value: Any, *, echo: bool = False) -> MessageEvent:
    raw = json.dumps(value).encode()
    return MessageEvent(
        at=at,
        topic=f"snowbot/{SERIAL}/{side}/{leaf}",
        serial=SERIAL,
        side=side,
        leaf=leaf,
        value=value,
        encoding="json",
        size=len(raw),
        echo=echo,
        raw=raw,
    )


def frame(planning: int, **extra: Any) -> dict[str, Any]:
    return {"StateMSG": {"on_going_planning": planning, **extra}, "BatteryMSG": {"status": 1}}


def test_state_frames_are_kept_only_around_a_change() -> None:
    box = FlightRecorder()
    for second in range(60):
        box.on_message(event(100.0 + second, "device", "DeviceMSG", frame(1)))
    box.on_message(event(160.0, "device", "DeviceMSG", frame(0, planning_paused=1)))
    states = [r for r in box.dump() if r.get("leaf") == "DeviceMSG"]
    assert [r["t"] for r in states] == [100.0, 159.0, 160.0], "first, the one before, the change"
    assert states[-1]["value"]["StateMSG.planning_paused"] == 1
    assert set(states[-1]["value"]) <= {k for r in states for k in r["value"]}


def test_commands_and_replies_are_all_kept_and_joystick_streams_are_not() -> None:
    box = FlightRecorder()
    box.on_message(event(1.0, "app", "start_plan", {"id": 1, "percent": 0}))
    box.on_message(event(1.1, "app", "pause", {}, echo=True))
    box.on_message(event(1.2, "device", "data_feedback", {"topic": "get_map", "state": 0}))
    for i in range(50):
        box.on_message(event(2.0 + i / 100, "app", "cmd_vel", {"vel": 0.0, "rev": 0.0}))
    kept = [(r["from"], r["leaf"]) for r in box.dump()]
    assert kept == [("app", "start_plan"), ("us", "pause"), ("device", "data_feedback")]


def test_feedback_topics_are_thinned() -> None:
    box = FlightRecorder()
    for i in range(100):  # 2 Hz for 50 s
        box.on_message(event(10.0 + i / 2, "device", "plan_feedback", {"planId": 1, "n": i}))
    assert len(box.dump()) == 5


def test_it_is_bounded_in_count_and_age() -> None:
    box = FlightRecorder(max_records=10, max_age=100.0)
    for i in range(30):
        box.on_message(event(float(i), "app", "read_all_plan", {"n": i}))
    assert [r["value"]["n"] for r in box.dump()] == list(range(20, 30))
    box.on_message(event(500.0, "app", "read_all_plan", {"n": 99}))
    assert [r["value"]["n"] for r in box.dump()] == [99], "older than max_age is dropped"


def test_maps_and_paths_are_cut_to_their_size() -> None:
    box = FlightRecorder()
    path = [{"x": float(i), "y": float(i)} for i in range(400)]
    reply = {"topic": "get_map", "state": 0, "data": "A" * 5000}
    box.on_message(event(1.0, "device", "data_feedback", reply))
    box.on_message(event(2.0, "device", "recharge_feedback", {"state": 1, "path": path}))
    first, second = box.dump()
    assert first["value"]["data"] == "<5000 chars>"
    assert second["value"]["path"] == "<400 items>"


def test_notes_sit_in_the_timeline() -> None:
    box = FlightRecorder()
    box.on_message(event(1.0, "app", "start_plan", {"id": 1, "percent": 0}))
    box.note(2.0, "start refused", key="rtk_not_ready")
    assert box.dump()[-1] == {"t": 2.0, "note": "start refused", "key": "rtk_not_ready"}


def test_a_real_afternoon_leaves_nothing_private_behind() -> None:
    """Replay an unredacted-shaped stream: serial in topics and values, coordinates in GGA."""
    fixture = (
        Path(__file__).resolve().parents[1]
        / "protocol/fixtures/3.14.11/plan-pause-resume-app.jsonl"
    )
    box = FlightRecorder()
    for line in fixture.read_text().splitlines():
        rec = json.loads(line)
        side, leaf = rec["topic"].split("/")[2:4]
        value = rec["payload"]
        if isinstance(value, dict) and leaf == "DeviceMSG":
            value = {**value, "StateMSG": {**value.get("StateMSG", {}), "plan_msg": f"sn {SERIAL}"}}
        box.on_message(event(rec["t"], side, leaf, value))
    text = json.dumps(box.dump())
    assert SERIAL not in text
    assert "gngga" not in text.lower(), "positions are not part of the story and are never kept"
    kinds = {r.get("leaf") for r in box.dump()}
    assert {"pause", "resume", "DeviceMSG"} <= kinds
