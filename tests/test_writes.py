"""The typed methods that change something on the robot, against the simulator.

Each payload is compared with what the app or the verification runs sent to a real robot
(firmware 3.14.11); the fixtures are the evidence for the shapes.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from yarbo_local import (
    FakeTransport,
    PlanRunningError,
    Schedule,
    Simulator,
    YarboRobot,
    payloads,
)
from yarbo_local.exceptions import CommandRefusedError

FIXTURES = Path(__file__).resolve().parents[1] / "protocol" / "fixtures" / "3.14.11"
FIXTURE = FIXTURES / "get_device_msg-asleep.jsonl"
REF = (-14.2973, 35.3024)  # the simulator's made-up reference, not a real place


@pytest.fixture
def sim() -> Simulator:
    sim = Simulator.from_fixture(FIXTURE)
    sim.plans = [
        {"id": 1, "name": "east lawn plan", "areaIds": [4], "enable_self_order": True},
        {"id": 2, "name": "west lawn plan", "areaIds": [9], "enable_self_order": True},
    ]
    sim.site_map["areas"] = [
        {
            "id": 4,
            "name": "East Lawn",
            "algorithm_type": 0,
            "labels": [],
            "leaf_piles": [],
            "push_snow_dir": 0.0,
            "slope_degrees": 0.0,
            "snowPiles": [],
            "trimming_edges": [],
            "area": 512.0,
            "mapping_module_type": 99,
            "range": [
                {"x": 0.0, "y": 0.0, "phi": 0.0},
                {"x": 5.0, "y": 0.0, "phi": 0.0},
                {"x": 5.0, "y": 5.0, "phi": 0.0},
            ],
            "ref": {"latitude": REF[0], "longitude": REF[1]},
        },
    ]
    return sim


async def _robot(sim: Simulator) -> YarboRobot:
    transport = FakeTransport()
    sim.attach(transport)
    robot = YarboRobot(transport, serial=sim.serial)
    await robot.start(1.0)
    return robot


def _sent(sim: Simulator, name: str) -> Any:
    return next(value for sent, value in reversed(sim.log) if sent == name)


def _app_request(fixture: str, name: str) -> dict[str, Any]:
    """What was really sent to a robot, from a recorded fixture."""
    for line in (FIXTURES / fixture).read_text().splitlines():
        record = json.loads(line)
        if record["topic"].endswith(f"/app/{name}") and isinstance(record["payload"], dict):
            return dict(record["payload"])
    raise AssertionError(f"{name} not in {fixture}")


def test_the_key_lists_are_the_ones_the_app_sent() -> None:
    sent = _app_request("verify-settings-saves.jsonl", "save_global_params")
    assert sorted(sent) == sorted(payloads.APP_GLOBAL_PARAM_KEYS)
    sent = _app_request("verify-settings-saves.jsonl", "save_mower_area_params")
    assert sorted(sent) == sorted(payloads.APP_MOWER_AREA_KEYS)
    assert sent["mapping_module_type"] == payloads.MAPPING_MODULE_TYPE


def test_plan_and_schedule_payloads_match_the_app() -> None:
    fixture = "app-plan-schedule-settings-rain.jsonl"
    sent = _app_request(fixture, "save_plan")
    assert (
        payloads.plan(
            sent["name"], sent["areaIds"], plan_id=sent["id"], self_order=sent["enable_self_order"]
        )
        == sent
    )
    sent = _app_request(fixture, "save_schedule")
    assert (
        payloads.schedule(
            schedule_id=sent["id"],
            plan_id=sent["plan_id"],
            name=sent["name"],
            start_time=sent["start_time"],
            end_time=sent["end_time"],
            week_day=sent["week_day"],
            schedule_type=sent["schedule_type"],
            enabled=sent["enable"],
            return_method=sent["return_method"],
            resume_progress=sent["enable_last_progress"],
            last_progress=sent["last_progress"],
        )
        == sent
    )
    with pytest.raises(ValueError, match="HH:MM:SS"):
        payloads.schedule(
            schedule_id=1,
            plan_id=1,
            name="x",
            start_time="9:00",
            end_time="10:00:00",
            week_day=1,
            schedule_type=3,
            enabled=True,
            return_method=2,
            resume_progress=False,
            last_progress=0,
        )


def test_map_object_payloads_match_what_was_sent_to_the_robot() -> None:
    fixture = "verify-map-objects.jsonl"
    for name, build in (
        (
            "save_nogozone",
            lambda s: payloads.nogozone(
                s["name"],
                [(p["x"], p["y"], p["phi"]) for p in s["range"]],
                (s["ref"]["latitude"], s["ref"]["longitude"]),
                zone_id=None,
                enabled=s["enable"],
            ),
        ),
        (
            "save_pathway",
            lambda s: payloads.pathway(
                s["name"],
                [(p["x"], p["y"], p["phi"]) for p in s["range"]],
                (s["ref"]["latitude"], s["ref"]["longitude"]),
                pathway_id=None,
            ),
        ),
        (
            "save_sidewalk",
            lambda s: payloads.sidewalk(
                s["name"],
                [(p["x"], p["y"], p["phi"]) for p in s["range"]],
                (s["ref"]["latitude"], s["ref"]["longitude"]),
                sidewalk_id=None,
            ),
        ),
    ):
        sent = _app_request(fixture, name)
        assert build(sent) == sent, name
    with pytest.raises(ValueError, match="three points"):
        payloads.nogozone("z", [(0, 0), (1, 1)], REF, zone_id=None, enabled=True)
    with pytest.raises(ValueError, match="x, y"):
        payloads.points([(1.0,)])


def test_a_whole_record_keeps_the_app_keys_and_refuses_the_rest() -> None:
    current = {
        "gap": 0.3,
        "edge_circles": 2,
        "current_blade_height": 0,
        "id": 4,
        "first_clean_params": {"blade_height": 50, "blade_speed": 80},
    }
    record = payloads.changed_record(
        current,
        payloads.APP_MOWER_AREA_KEYS,
        {"edge_circles": 1, "first_clean_params": {"blade_height": 60}},
        fixed={"id": 4, "mapping_module_type": 99},
    )
    assert record == {
        "gap": 0.3,
        "edge_circles": 1,
        "id": 4,
        "mapping_module_type": 99,
        "first_clean_params": {"blade_height": 60, "blade_speed": 80},
    }
    assert "current_blade_height" not in record, "the app does not send it back"
    with pytest.raises(ValueError, match="current_blade_height"):
        payloads.changed_record(current, payloads.APP_MOWER_AREA_KEYS, {"current_blade_height": 5})
    with pytest.raises(ValueError, match="mapping_module_type"):
        payloads.changed_record(
            current,
            payloads.APP_MOWER_AREA_KEYS,
            {"mapping_module_type": 1},
            fixed={"mapping_module_type": 99},
        )


async def test_plans_create_edit_sort_delete(sim: Simulator) -> None:
    robot = await _robot(sim)
    new_id = await robot.save_plan("Front and back", [4, 9])
    assert new_id == 3, "the robot numbers it; the id is found by reading the plans again"
    assert "id" not in _sent(sim, "save_plan"), "no id is sent when creating"
    assert await robot.save_plan("Front only", [4], plan_id=new_id) == new_id
    assert _sent(sim, "save_plan") == {
        "id": 3,
        "areaIds": [4],
        "name": "Front only",
        "enable_self_order": True,
    }
    await robot.sort_plans([3, 1, 2])
    assert [p.id for p in await robot.plans()] == [3, 1, 2]
    await robot.save_schedule(
        plan_id=3, name="Monday", start_time="10:00:00", end_time="12:00:00", week_day=1
    )
    await robot.delete_plan(3)
    assert [p.id for p in await robot.plans()] == [1, 2]
    assert await robot.schedules() == [], "its schedule went with it"
    with pytest.raises(ValueError, match="at least one area"):
        await robot.save_plan("Empty", [])
    await robot.close()


async def test_schedules(sim: Simulator) -> None:
    robot = await _robot(sim)
    stored = await robot.save_schedule(
        plan_id=1,
        name="Monday mow",
        start_time="10:00:00",
        end_time="12:00:00",
        week_day=1,
        enabled=False,
    )
    assert isinstance(stored, Schedule)
    assert (stored.id, stored.plan_id, stored.enabled, stored.week_day) == (1, 1, False, 1)
    assert (stored.schedule_type, stored.return_method) == (3, 2), "the values the app sent"
    assert stored.raw["timezone"] == "", "the robot's own fields are kept as it returned them"
    second = await robot.save_schedule(
        plan_id=2, name="Thursday", start_time="09:00:00", end_time="11:00:00", week_day=4
    )
    assert second.id == 2, "the next free number"
    assert [s.name for s in await robot.schedules()] == ["Monday mow", "Thursday"]
    await robot.delete_schedule(1)
    assert [s.id for s in await robot.schedules()] == [2]
    await robot.close()


async def test_settings(sim: Simulator) -> None:
    robot = await _robot(sim)
    await robot.set_sound(enabled=True, volume=0.5)
    assert _sent(sim, "set_sound_param") == {"enable": True, "vol": 0.5, "mode": 0}
    await robot.set_person_detection(False)
    assert _sent(sim, "set_person_detect") == {"state": 0}
    await robot.set_child_lock(True)
    assert _sent(sim, "set_child_lock") == {"state": True}
    await robot.set_follow_mode(True)
    assert _sent(sim, "set_follow_state") == {"state": 1}
    with pytest.raises(ValueError, match=r"0\.0 to 1\.0"):
        await robot.set_sound(enabled=True, volume=50)

    sim.global_params = {key: 0 for key in payloads.APP_GLOBAL_PARAM_KEYS if key != "id"}
    sim.global_params["recharge_battery"] = 20
    sim.global_params["not_sent_by_the_app"] = 7
    record = await robot.update_global_params(recharge_battery=25)
    assert record["recharge_battery"] == 25
    assert record["id"] == 1
    assert sorted(record) == sorted(payloads.APP_GLOBAL_PARAM_KEYS)
    assert sim.global_params["recharge_battery"] == 25
    with pytest.raises(ValueError, match="not_sent_by_the_app"):
        await robot.update_global_params(not_sent_by_the_app=1)
    await robot.close()


async def test_mower_area_settings(sim: Simulator) -> None:
    sim.mower_area_params[4] = {
        "id": 4,
        "gap": 0.3,
        "edge_circles": 2,
        "current_blade_height": 0,
        "first_clean_params": {"blade_height": 50, "blade_speed": 80, "plan_speed": 0.5},
    }
    robot = await _robot(sim)
    assert (await robot.mower_area_params(4) or {})["edge_circles"] == 2
    assert await robot.mower_area_params(99) is None, "defaults for an unknown area are refused"
    record = await robot.update_mower_area_params(
        4, edge_circles=1, first_clean_params={"blade_height": 60}
    )
    assert record["mapping_module_type"] == 99
    assert record["first_clean_params"] == {
        "blade_height": 60,
        "blade_speed": 80,
        "plan_speed": 0.5,
    }
    assert "current_blade_height" not in record
    assert sim.mower_area_params[4]["edge_circles"] == 1
    with pytest.raises(ValueError, match="no area 99"):
        await robot.update_mower_area_params(99, edge_circles=1)
    await robot.close()


async def test_map_objects(sim: Simulator) -> None:
    robot = await _robot(sim)
    square = [(0, 0), (2, 0), (2, 2), (0, 2)]
    zone = await robot.save_nogozone("Flower bed", square, REF)
    assert zone["id"] == 20, "the stored record comes back with the robot's number"
    assert "id" not in _sent(sim, "save_nogozone")
    path = await robot.save_pathway("To the lawn", [(0, 0), (4, 0)], REF)
    walk = await robot.save_sidewalk("Front walk", [(0, 0), (0, 6)], REF)
    assert (path["id"], walk["id"]) == (21, 22), "one counter for all three kinds"
    moved = await robot.save_nogozone("Flower bed", [(1, 1), (3, 1), (3, 3)], REF, zone_id=20)
    assert moved["id"] == 20
    assert len(sim.site_map["nogozones"]) == 1, "an id edits in place"
    await robot.delete_nogozone(20)
    await robot.delete_pathway(21)
    await robot.delete_sidewalk(22)
    assert sim.site_map["nogozones"] == sim.site_map["pathways"] == sim.site_map["sidewalks"] == []

    stored = await robot.update_area(4, algorithm_type=1)
    assert stored["algorithm_type"] == 1
    sent = _sent(sim, "save_clean_area")
    assert sorted(sent) == sorted(payloads.APP_AREA_KEYS), "the app's keys, nothing else"
    assert sent["name"] == "East Lawn", "everything else is sent back as it was"
    with pytest.raises(ValueError, match="no area 77"):
        await robot.update_area(77, name="x")
    await robot.close()


async def test_route_preview_and_progress(sim: Simulator) -> None:
    robot = await _robot(sim)
    preview = await robot.preview_route(1)
    assert preview is not None
    assert preview.plan_id == 1
    assert preview.total_s == 600.0
    assert [(a.area_id, a.path_type) for a in preview.areas] == [(4, 0), (4, 1)]
    assert preview.areas[0].path == ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0))
    assert _sent(sim, "preview_plan_path") == {"id": 1, "percent": 0}
    assert await robot.plan_progress() is None, "no plan is being worked"
    await robot.close()


async def test_blade_height_and_charging(sim: Simulator) -> None:
    sim.snapshot["HeadMsg"] = {**sim.snapshot.get("HeadMsg", {}), "head_type": 5}
    robot = await _robot(sim)
    await robot.wake()
    await asyncio.sleep(0.05)
    await robot.set_blade_height(60)
    assert _sent(sim, "mower_target_cmd") == {"target": 60}
    await robot.start_charging()
    assert _sent(sim, "wireless_charging_cmd") == {"cmd": 1}
    with pytest.raises(ValueError, match="millimetres"):
        await robot.set_blade_height(0)
    await robot.close()


async def test_nothing_takes_the_controller_while_a_plan_runs(sim: Simulator) -> None:
    """Seen on the robot: get_controller during a mow pauses the plan."""
    sim.snapshot["StateMSG"] = {**sim.snapshot["StateMSG"], "on_going_planning": 1}
    robot = await _robot(sim)
    await robot.wake()
    await asyncio.sleep(0.05)
    assert robot.state.plan_running
    before = len(sim.log)
    with pytest.raises(PlanRunningError, match="would pause the running plan"):
        await robot.set_sound(enabled=True, volume=0.3)
    with pytest.raises(CommandRefusedError):
        await robot.save_nogozone("Bed", [(0, 0), (1, 0), (1, 1)], REF)
    assert len(sim.log) == before, "nothing at all was sent"

    await robot.set_sound(enabled=True, volume=0.3, may_pause_plan=True)
    sent = [name for name, _ in sim.log[before:]]
    assert sent == ["get_controller", "set_sound_param"], "the caller accepted the pause"

    # What a running plan needs never takes the controller.
    await robot.pause()
    await asyncio.sleep(0.05)
    await robot.resume()
    await asyncio.sleep(0.05)
    await robot.save_plan("Another", [4])
    names = [name for name, _ in sim.log[before + 2 :]]
    assert "get_controller" not in names
    await robot.close()
