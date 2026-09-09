"""Session behaviour against the simulator on a fake transport."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import pytest

from yarbo_local import codec, topics
from yarbo_local.client import YarboRobot
from yarbo_local.exceptions import (
    CommandError,
    CommandRefusedError,
    ConnectionLostError,
    ReplyTimeoutError,
)
from yarbo_local.models import Activity
from yarbo_local.session import Session
from yarbo_local.simulator import Simulator
from yarbo_local.transport import FakeTransport

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "protocol"
    / "fixtures"
    / "3.14.11"
    / "get_device_msg-asleep.jsonl"
)


@pytest.fixture
def sim() -> Simulator:
    return Simulator.from_fixture(FIXTURE)


@pytest.fixture
def transport(sim: Simulator) -> FakeTransport:
    t = FakeTransport()
    sim.attach(t)
    return t


async def _running(session: Session) -> asyncio.Task[None]:
    task = asyncio.create_task(session.run())
    await asyncio.sleep(0)  # let it connect
    return task


async def test_learns_serial_and_state_from_traffic(
    sim: Simulator, transport: FakeTransport
) -> None:
    session = Session(transport)
    task = await _running(session)
    sim.tick()  # asleep heartbeat
    await session.wait_ready(1.0)
    assert session.serial == sim.serial
    assert session.state.awake is False
    session.stop()
    transport.drop()
    await asyncio.wait_for(task, 1.0)


async def test_request_correlates_reply_and_ignores_own_echo(
    sim: Simulator, transport: FakeTransport
) -> None:
    session = Session(transport, serial=sim.serial)
    task = await _running(session)
    sim.tick()
    await session.wait_ready(1.0)
    fb = await session.request("get_device_msg", timeout=1.0)
    assert fb.ok
    assert fb.payload["version"] == "3.14.11"
    # The broker echo of our own publish must not be treated as robot traffic
    assert sim.log[0][0] == "get_device_msg"
    session.stop()
    transport.drop()
    await asyncio.wait_for(task, 1.0)


async def test_error_state_raises(sim: Simulator, transport: FakeTransport) -> None:
    session = Session(transport, serial=sim.serial)
    task = await _running(session)
    sim.tick()
    await session.wait_ready(1.0)
    with pytest.raises(CommandError) as err:
        await session.request("read_global_params", {}, timeout=1.0)
    assert err.value.state == -2
    ok = await session.request("read_global_params", {"id": 1}, timeout=1.0)
    assert ok.payload["plan_speed"] == 0.6
    session.stop()
    transport.drop()
    await asyncio.wait_for(task, 1.0)


async def test_verified_read_passes_rules(sim: Simulator, transport: FakeTransport) -> None:
    session = Session(transport, serial=sim.serial)
    task = await _running(session)
    sim.tick()
    await session.wait_ready(1.0)
    fb = await session.request("read_all_nogozone", timeout=1.0)
    assert fb.ok
    assert fb.payload == {"data": []}
    session.stop()
    transport.drop()
    await asyncio.wait_for(task, 1.0)


async def test_controller_is_candidate_until_verified(
    sim: Simulator, transport: FakeTransport
) -> None:
    session = Session(transport, serial=sim.serial)
    task = await _running(session)
    sim.tick()
    await session.wait_ready(1.0)
    with pytest.raises(CommandRefusedError, match="candidate"):
        await session.ensure_controller(timeout=1.0)
    assert sim.controller_holder is None
    assert not transport.published
    session.stop()
    transport.drop()
    await asyncio.wait_for(task, 1.0)


async def test_controller_with_opt_in(sim: Simulator, transport: FakeTransport) -> None:
    session = Session(transport, serial=sim.serial, allow_candidates=True)
    task = await _running(session)
    sim.tick()
    await session.wait_ready(1.0)
    await session.ensure_controller(timeout=1.0)
    assert sim.controller_holder == "session"
    session.stop()
    transport.drop()
    await asyncio.wait_for(task, 1.0)


async def test_candidate_refused_and_forbidden_impossible(
    sim: Simulator, transport: FakeTransport
) -> None:
    session = Session(transport, serial=sim.serial)
    task = await _running(session)
    sim.tick()
    await session.wait_ready(1.0)
    with pytest.raises(CommandRefusedError, match="candidate"):
        await session.send("start_plan", {"id": 1})
    with pytest.raises(CommandRefusedError, match="forbidden"):
        await session.send("cmd_vel", {"vel": 1, "rev": 0})
    with pytest.raises(CommandRefusedError, match="forbidden"):
        await session.send("erase_map", confirmed=True)
    assert not transport.published, "refused commands must never reach the wire"
    session.stop()
    transport.drop()
    await asyncio.wait_for(task, 1.0)


async def test_wake_then_stream(sim: Simulator, transport: FakeTransport) -> None:
    session = Session(transport, serial=sim.serial)
    task = await _running(session)
    sim.tick()
    await session.wait_ready(1.0)
    assert session.state.awake is False
    assert await session.wake(timeout=1.0)
    assert session.state.awake is True
    sim.tick()
    await asyncio.sleep(0.05)
    assert session.state.battery == 100
    assert session.state.activity is Activity.CHARGING
    session.stop()
    transport.drop()
    await asyncio.wait_for(task, 1.0)


async def test_encoding_follows_firmware(sim: Simulator, transport: FakeTransport) -> None:
    session = Session(transport, serial=sim.serial)
    task = await _running(session)
    sim.tick()
    await session.wait_ready(1.0)
    await session.request("get_device_msg", timeout=1.0)
    assert codec.looks_zlib(transport.published[0].payload)
    session.stop()
    transport.drop()
    await asyncio.wait_for(task, 1.0)


async def test_timeout_and_reconnect_backoff(sim: Simulator, transport: FakeTransport) -> None:
    session = Session(transport, serial=sim.serial)
    task = await _running(session)
    sim.tick()
    await session.wait_ready(1.0)
    sim.plans = []
    transport.on_publish = None  # robot goes silent
    with pytest.raises(ReplyTimeoutError):
        await session.request("read_all_plan", timeout=0.1)
    # Drop the connection: pending futures fail, the session backs off and reconnects.
    fut = asyncio.ensure_future(session.request("get_device_msg", timeout=5.0))
    await asyncio.sleep(0)
    transport.drop()
    with pytest.raises(ConnectionLostError):
        await fut
    assert session.connected is False
    await asyncio.sleep(0.05)
    assert not task.done(), "run() must keep retrying, never return on disconnect"
    session.stop()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_client_reads(sim: Simulator, transport: FakeTransport) -> None:
    robot = YarboRobot(transport, serial=sim.serial)
    task = asyncio.create_task(robot.session.run())
    await asyncio.sleep(0)
    sim.tick()
    await robot.session.wait_ready(1.0)
    state = await robot.snapshot()
    assert state.firmware == "3.14.11"
    assert await robot.plans() == []
    ref = await robot.gps_reference()
    assert ref is not None
    assert ref.fixed
    site = await robot.site_map()
    assert len(site.charging) == 1
    assert site.zones == ()
    robot.session.stop()
    transport.drop()
    await asyncio.wait_for(task, 1.0)


async def test_plaintext_to_new_firmware_is_dropped_by_simulator(
    sim: Simulator, transport: FakeTransport
) -> None:
    session = Session(transport, serial=sim.serial, encoding="json")
    task = await _running(session)
    sim.tick()
    await session.wait_ready(1.0)
    with pytest.raises(ReplyTimeoutError):
        await session.request("get_device_msg", timeout=0.1)
    assert sim.log[-1] == ("get_device_msg", "DROPPED: not zlib")
    session.stop()
    transport.drop()
    await asyncio.wait_for(task, 1.0)


async def test_client_start_with_spawn_and_timeout(
    sim: Simulator, transport: FakeTransport
) -> None:
    spawned: list[asyncio.Task[None]] = []

    def spawn(coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.create_task(coro)
        spawned.append(task)
        return task

    robot = YarboRobot(transport, serial=sim.serial)
    await robot.start(1.0, spawn=spawn)
    assert spawned
    assert not spawned[0].done()
    assert robot.state.awake is False
    await robot.close()
    assert spawned[0].cancelled()

    dead = YarboRobot(FakeTransport(fail_next_connects=10**6), serial=sim.serial)
    with pytest.raises(ConnectionLostError, match="no robot heartbeat"):
        await dead.start(0.2)


def test_topics_all_for_serial() -> None:
    assert topics.all_for("abc") == "snowbot/abc/#"
