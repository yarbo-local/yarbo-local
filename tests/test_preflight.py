"""Pre-flight checks, and the controls that are verified: dock, resume, pause and start."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from yarbo_local import (
    FakeTransport,
    PlanStartError,
    PreflightError,
    Registry,
    Simulator,
    YarboRobot,
)
from yarbo_local.models import RobotState
from yarbo_local.preflight import COMMANDS, Action, check

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "protocol"
    / "fixtures"
    / "3.14.11"
    / "get_device_msg-asleep.jsonl"
)


def state_of(*, battery_status: int = 1, head: int = 5, rtk: int = 4, **codes: int) -> RobotState:
    frame = {
        "StateMSG": codes,
        "BatteryMSG": {"status": battery_status},
        "HeadMsg": {"head_type": head},
        "RTKMSG": {"status": rtk},
    }
    return RobotState().with_frame(frame, 0.0)[0]


def keys(action: Action, state: RobotState, *, connected: bool = True) -> list[str]:
    return [r.key for r in check(action, state, connected=connected)]


def test_offline_refuses_everything() -> None:
    for action in Action:
        assert keys(action, state_of(), connected=False) == ["robot_offline"]


def test_dock() -> None:
    assert keys(Action.DOCK, state_of(on_going_planning=1)) == []
    assert keys(Action.DOCK, state_of(planning_paused=7, error_code=902)) == [], (
        "a fault must not block the trip home: cmd_recharge is what clears a latched fault"
    )
    assert keys(Action.DOCK, state_of(on_going_recharging=1)) == ["already_returning"]
    assert keys(Action.DOCK, state_of(battery_status=3)) == ["already_docked"]


def test_resume() -> None:
    assert keys(Action.RESUME, state_of(planning_paused=4)) == []
    assert keys(Action.RESUME, state_of(planning_paused=7, error_code=902)) == []
    assert keys(Action.RESUME, state_of()) == ["nothing_to_resume"]
    assert keys(Action.RESUME, state_of(on_going_planning=1)) == ["plan_running"]
    # The pause code lingers on the way home and on the dock; that is not a paused plan.
    assert keys(Action.RESUME, state_of(planning_paused=7, on_going_recharging=1)) == [
        "nothing_to_resume"
    ]
    assert keys(Action.RESUME, state_of(planning_paused=7, battery_status=3)) == [
        "nothing_to_resume"
    ]


def test_start_collects_every_reason() -> None:
    assert keys(Action.START, state_of()) == []
    assert keys(Action.START, state_of(on_going_planning=1)) == ["plan_running"]
    assert keys(Action.START, state_of(planning_paused=4)) == ["plan_running"]
    assert keys(Action.START, state_of(head=0, rtk=1, error_code=902, on_going_recharging=2)) == [
        "returning",
        "fault_active",
        "rtk_not_ready",
        "no_head",
    ]
    assert keys(Action.START, state_of(battery_status=3)) == [], "the robot undocks itself"


def test_pause_and_stop() -> None:
    assert keys(Action.PAUSE, state_of(on_going_planning=1)) == []
    assert keys(Action.PAUSE, state_of()) == ["no_plan_running"]
    assert keys(Action.STOP, state_of(planning_paused=4)) == [], "a paused plan can be stopped"
    assert keys(Action.STOP, state_of()) == ["no_plan_running"]


def test_controls_exist_only_for_verified_commands() -> None:
    registry = Registry.default()
    offered = {action for action in Action if registry.sendable(COMMANDS[action])}
    assert offered == set(Action), "every moving action is verified on 3.14.11"
    assert not registry.sendable("cmd_vel"), "forbidden"
    assert not registry.sendable("no_such_command")
    assert not registry.sendable("cmd_buzzer"), "still a candidate"
    assert registry.sendable("cmd_buzzer", allow_candidates=True)


@pytest.fixture
def sim() -> Simulator:
    return Simulator.from_fixture(FIXTURE)


async def _robot(sim: Simulator) -> YarboRobot:
    transport = FakeTransport()
    sim.attach(transport)
    robot = YarboRobot(transport, serial=sim.serial)
    await robot.start(1.0)
    return robot


async def test_dock_wakes_takes_the_controller_and_sends_the_verified_payload(
    sim: Simulator,
) -> None:
    sim.snapshot["StateMSG"] = {**sim.snapshot["StateMSG"], "planning_paused": 7, "error_code": 902}
    robot = await _robot(sim)
    assert robot.can(Action.DOCK)
    await robot.dock()
    await asyncio.sleep(0.05)
    sent = [name for name, _ in sim.log]
    assert sent == ["set_working_state", "get_controller", "cmd_recharge"], sent
    assert sim.log[-1] == ("cmd_recharge", {"cmd": 2})
    assert robot.state.returning
    assert robot.state.fault is None, "as on the real robot, going home clears the fault"
    with pytest.raises(PreflightError) as refused:
        await robot.dock()
    assert [r.key for r in refused.value.refusals] == ["already_returning"]
    await robot.close()


async def test_resume_and_its_refusal(sim: Simulator) -> None:
    robot = await _robot(sim)
    with pytest.raises(PreflightError, match="No plan is paused"):
        await robot.resume()
    assert [name for name, _ in sim.log] == [], "a refused action sends nothing at all"

    sim.snapshot["StateMSG"] = {**sim.snapshot["StateMSG"], "planning_paused": 4}
    await robot.wake()
    await asyncio.sleep(0.05)
    await robot.resume()
    await asyncio.sleep(0.05)
    assert robot.state.plan_running
    assert robot.can(Action.PAUSE)
    assert robot.can(Action.STOP)
    await robot.close()


async def test_pause_then_resume_as_the_app_does(sim: Simulator) -> None:
    sim.snapshot["StateMSG"] = {**sim.snapshot["StateMSG"], "on_going_planning": 1}
    robot = await _robot(sim)
    await robot.wake()
    await asyncio.sleep(0.05)
    await robot.pause()
    await asyncio.sleep(0.05)
    assert sim.log[-1] == ("pause", {})
    assert robot.state.pause_reason == "manual"
    assert not robot.state.plan_running
    await robot.resume()
    await asyncio.sleep(0.05)
    assert robot.state.plan_running
    await robot.close()


def _ready_to_mow(sim: Simulator) -> None:
    sim.snapshot["HeadMsg"] = {**sim.snapshot.get("HeadMsg", {}), "head_type": 5}
    sim.snapshot["RTKMSG"] = {**sim.snapshot.get("RTKMSG", {}), "status": "4"}


async def test_start_plan_sends_the_id_and_never_the_schema(sim: Simulator) -> None:
    _ready_to_mow(sim)
    robot = await _robot(sim)
    with pytest.raises(ValueError, match="plan id"):
        await robot.act(Action.START)
    with pytest.raises(ValueError, match="percent"):
        await robot.start_plan(1, percent=100)
    assert [name for name, _ in sim.log] == [], "nothing is sent without a plan"
    sim.plans.append({"id": 1, "name": "east lawn plan", "areaIds": [4]})
    plan_id = 1
    await robot.wake()
    await asyncio.sleep(0.05)
    await robot.start_plan(plan_id)
    await asyncio.sleep(0.05)
    assert sim.log[-1] == ("start_plan", {"id": plan_id, "percent": 0})
    assert robot.state.planning_code == 2, "calculating the route, as on the real robot"
    await robot.close()


async def test_a_start_the_robot_cannot_route_is_reported_in_words(sim: Simulator) -> None:
    _ready_to_mow(sim)
    # As on the real robot, the code from an earlier failure is still there before the start.
    sim.snapshot["StateMSG"] = {**sim.snapshot["StateMSG"], "on_going_planning": -12}
    robot = await _robot(sim)
    await robot.wake()
    await asyncio.sleep(0.05)
    with pytest.raises(PlanStartError) as failed:
        await robot.start_plan(999, confirm_within=0.3)
    assert failed.value.error is not None
    assert failed.value.error.key == "route_failed", "what the app shows as WP005"
    assert "WP005" in failed.value.error.hint
    assert not robot.state.plan_running
    await robot.close()


def test_plan_errors_get_words_only_when_the_app_was_seen_saying_them() -> None:
    assert state_of(on_going_planning=0).plan_error is None
    assert state_of(on_going_planning=1).plan_error is None
    known = state_of(on_going_planning=-12).plan_error
    assert known is not None
    assert known.description == "Failed to calculate route"
    unknown = state_of(on_going_planning=-24).plan_error
    assert unknown is not None
    assert unknown.key is None
    assert unknown.description == "Plan error -24", "borrowed meanings are not shown as fact"


async def test_stop_ends_the_plan_where_it_is(sim: Simulator) -> None:
    sim.snapshot["StateMSG"] = {**sim.snapshot["StateMSG"], "on_going_planning": 3}
    robot = await _robot(sim)
    await robot.wake()
    await asyncio.sleep(0.05)
    await robot.stop()
    await asyncio.sleep(0.05)
    assert sim.log[-1] == ("stop", {})
    assert not robot.state.plan_running
    assert robot.state.pause_reason is None, "a stop is not a pause"
    with pytest.raises(PreflightError):
        await robot.stop()
    await robot.close()
