"""Pre-flight checks, and the two controls that are verified: return to dock and resume."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from yarbo_local import FakeTransport, PreflightError, Registry, Simulator, YarboRobot
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
    assert offered == {Action.DOCK, Action.RESUME}, (
        "pause, stop and start_plan have no capture yet; they must not be offered. When one is "
        "verified in commands.yaml this set grows, and so does every user interface."
    )
    assert not registry.sendable("cmd_vel"), "forbidden"
    assert not registry.sendable("no_such_command")
    assert registry.sendable("start_plan", allow_candidates=True)


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
    assert not robot.can(Action.PAUSE)
    await robot.close()
