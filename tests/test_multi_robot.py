"""Several robots at one site, simulated.

Nobody on the project owns two robots, so these tests state what must hold and
prove it against the simulator. Three site shapes are covered:

- one broker per robot, which is what a rover on Wi-Fi is;
- one robot behind two addresses, which is a rover plus its base station relay
  (seen on real hardware);
- one broker carrying several robots, which a shared base station might be
  (not yet seen on real hardware; ``tests/live`` exists to find out).

Every assertion says what was expected, what was seen and what it means, so a
failure on someone else's site is something we can act on.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from yarbo_local import FakeBroker, Simulator, codec, discover, topics
from yarbo_local.exceptions import RobotNotFoundError
from yarbo_local.resolve import resolve
from yarbo_local.session import MessageEvent, Session
from yarbo_local.transport import Transport

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "protocol"
    / "fixtures"
    / "3.14.11"
    / "get_device_msg-asleep.jsonl"
)
SECOND = "SN-second-robot"
OLD_FIRMWARE = "3.8.2"  # before 3.9 the robot speaks plain JSON and drops zlib


@pytest.fixture
def robot_a() -> Simulator:
    return Simulator.from_fixture(FIXTURE)


@pytest.fixture
def robot_b(robot_a: Simulator) -> Simulator:
    other = robot_a.sibling(SECOND)
    other.snapshot["BatteryMSG"] = {**other.snapshot["BatteryMSG"], "capacity": 42}
    return other


@pytest.fixture
def shared(robot_a: Simulator, robot_b: Simulator) -> FakeBroker:
    broker = FakeBroker()
    broker.attach(robot_a)
    broker.attach(robot_b)
    return broker


async def _start(broker: FakeBroker, serial: str | None) -> tuple[Session, asyncio.Task[None]]:
    session = Session(broker.client(), serial=serial)
    task = asyncio.create_task(session.run())
    await session.wait_ready(1.0)
    return session, task


async def _stop(*pairs: tuple[Session, asyncio.Task[None]]) -> None:
    for session, task in pairs:
        session.stop()
        task.cancel()
    await asyncio.gather(*(task for _, task in pairs), return_exceptions=True)


# -- topic filters: the broker test double is only as good as its matching


@pytest.mark.parametrize(
    ("topic_filter", "topic", "expected"),
    [
        ("snowbot/A/#", "snowbot/A/device/heart_beat", True),
        ("snowbot/A/#", "snowbot/B/device/heart_beat", False),
        ("snowbot/A/#", "snowbot/A", True),
        ("snowbot/+/#", "snowbot/B/app/get_map", True),
        ("snowbot/+/device/heart_beat", "snowbot/B/device/heart_beat", True),
        ("snowbot/+/device/heart_beat", "snowbot/B/device/DeviceMSG", False),
        ("snowbot/+/device/heart_beat", "snowbot/B/device/heart_beat/extra", False),
        ("snowbot/A/device/+", "snowbot/A/device", False),
        ("other/#", "snowbot/A/device/heart_beat", False),
    ],
)
def test_topic_filter_matching(topic_filter: str, topic: str, expected: bool) -> None:
    assert topics.matches(topic_filter, topic) is expected


# -- one broker carrying two robots


async def test_a_scoped_session_never_hears_the_other_robot(
    shared: FakeBroker, robot_a: Simulator, robot_b: Simulator
) -> None:
    heard: list[MessageEvent] = []
    session, task = await _start(shared, robot_a.serial)
    session.add_message_listener(heard.append)
    other, other_task = await _start(shared, robot_b.serial)
    await other.wake(timeout=1.0)
    for _ in range(3):
        robot_a.tick()
        robot_b.tick()
    await asyncio.sleep(0.05)

    foreign = sorted({ev.serial for ev in heard} - {robot_a.serial})
    assert foreign == [], (
        f"A session for {robot_a.serial} subscribed to {session.transport.filters!r} and still "  # type: ignore[attr-defined]
        f"received traffic for {foreign}. Expected: only its own serial. Meaning: entities of "
        "one robot could show another robot's state."
    )
    assert session.state.awake is False, (
        "Robot B was woken and robot A's session now reports awake. Expected: A still asleep. "
        "Meaning: state crossed between robots on a shared broker."
    )
    assert session.other_serials == set()
    await _stop((session, task), (other, other_task))


async def test_each_robot_answers_its_own_request(
    shared: FakeBroker, robot_a: Simulator, robot_b: Simulator
) -> None:
    a, a_task = await _start(shared, robot_a.serial)
    b, b_task = await _start(shared, robot_b.serial)
    reply_a, reply_b = await asyncio.gather(
        a.request("get_device_msg", timeout=1.0), b.request("get_device_msg", timeout=1.0)
    )
    battery_a = reply_a.payload["BatteryMSG"]["capacity"]
    battery_b = reply_b.payload["BatteryMSG"]["capacity"]
    assert (battery_a, battery_b) == (100, 42), (
        f"Two get_device_msg requests went out at once. Expected batteries (100, 42) for "
        f"({robot_a.serial}, {robot_b.serial}); got ({battery_a}, {battery_b}). Meaning: a reply "
        "was matched to the wrong robot. Replies are matched by command name only, so this "
        "relies on each session being subscribed to its own serial."
    )
    assert a.state.battery == 100
    assert b.state.battery == 42
    await _stop((a, a_task), (b, b_task))


async def test_a_command_reaches_one_robot_only(
    shared: FakeBroker, robot_a: Simulator, robot_b: Simulator
) -> None:
    b, b_task = await _start(shared, robot_b.serial)
    assert await b.wake(timeout=1.0)
    handled_by_a = [name for name, _ in robot_a.log]
    assert handled_by_a == [], (
        f"A wake sent to {robot_b.serial} was also handled by {robot_a.serial}: {handled_by_a}. "
        "Expected: nothing. Meaning: a command for one robot moved another."
    )
    assert robot_b.awake is True
    assert robot_a.awake is False
    await _stop((b, b_task))


async def test_mixed_firmware_on_one_broker(shared: FakeBroker, robot_a: Simulator) -> None:
    """One robot on zlib firmware, one on plain JSON: the encoding is per robot, not per broker."""
    old = robot_a.sibling("SN-old-firmware", firmware=OLD_FIRMWARE)
    shared.attach(old)
    new_session, new_task = await _start(shared, robot_a.serial)
    old_session, old_task = await _start(shared, old.serial)
    await new_session.request("get_device_msg", timeout=0.3)
    # The old robot is asleep, so its firmware is unknown: the first try is a zlib guess,
    # which it drops; the retry in plain JSON is the one it answers.
    await old_session.request("get_device_msg", timeout=0.3)
    assert [entry for entry in old.log if entry[0] == "get_device_msg"] == [
        ("get_device_msg", "DROPPED: zlib"),
        ("get_device_msg", {}),
    ], f"what the old-firmware robot saw: {old.log}"

    await new_session.request("read_all_plan", timeout=0.3)
    await old_session.request("read_all_plan", timeout=0.3)
    last = {
        serial: codec.looks_zlib(
            [m for c in shared.clients for m in c.published if f"/{serial}/" in m.topic][-1].payload
        )
        for serial in (robot_a.serial, old.serial)
    }
    assert last == {robot_a.serial: True, old.serial: False}, (
        f"Encoding of the last command per robot: {last}. Expected zlib for firmware "
        f"{robot_a.firmware} and plain JSON for {OLD_FIRMWARE}, each settled by that robot's own "
        "snapshot. Meaning: the encoding leaked from one robot to the other, and firmware "
        "before 3.9 drops zlib without a reply, so that robot would look dead."
    )
    await _stop((new_session, new_task), (old_session, old_task))


async def test_an_unscoped_session_says_which_robot_it_took(
    shared: FakeBroker, robot_a: Simulator, robot_b: Simulator, caplog: pytest.LogCaptureFixture
) -> None:
    session, task = await _start(shared, None)
    await asyncio.sleep(0.05)
    assert session.serial in {robot_a.serial, robot_b.serial}
    others = {robot_a.serial, robot_b.serial} - {session.serial}
    assert session.other_serials == others, (
        f"An unscoped session took {session.serial} and recorded {session.other_serials} as also "
        f"present. Expected {others}. Meaning: setup cannot warn that the broker carries "
        "several robots."
    )
    assert "also carries robot" in caplog.text
    await _stop((session, task))


# -- discovery and address resolution


def _site(brokers: dict[str, FakeBroker]) -> discover.TransportFactory:
    def connect(host: str, _port: int) -> Transport:
        return brokers[host].client()

    return connect


async def test_discovery_hears_every_robot_on_a_shared_broker(
    shared: FakeBroker, robot_a: Simulator, robot_b: Simulator
) -> None:
    hits = await discover.discover(
        ["10.0.0.5"],
        wait=0.2,
        heartbeat_only=True,
        names=False,
        connect=_site({"10.0.0.5": shared}),
    )
    assert hits[0].serials == sorted([robot_a.serial, robot_b.serial]), (
        f"Heard {hits[0].serials} with heartbeats {hits[0].heartbeats}. Expected both robots. "
        "Meaning: discovery stopped at the first heartbeat, so the second robot on a shared "
        "broker cannot be found or re-found."
    )
    assert hits[0].leaves == ["heart_beat"], "a heartbeat-only scan subscribed to more than that"


async def test_stop_on_returns_early_but_never_for_the_wrong_robot(
    shared: FakeBroker, robot_a: Simulator, robot_b: Simulator
) -> None:
    connect = _site({"10.0.0.5": shared})
    started = asyncio.get_running_loop().time()
    got = await discover.sample("10.0.0.5", wait=5.0, stop_on=robot_b.serial, connect=connect)
    took = asyncio.get_running_loop().time() - started
    assert robot_b.serial in got.serials
    assert took < 1.0, f"stop_on did not return early: {took:.2f}s of a 5 s window"
    missing = await discover.sample("10.0.0.5", wait=0.2, stop_on="SN-absent", connect=connect)
    assert "SN-absent" not in missing.serials
    assert missing.serials == sorted([robot_a.serial, robot_b.serial])


async def test_resolve_finds_each_robot_at_its_own_address(
    robot_a: Simulator, robot_b: Simulator
) -> None:
    """Two rovers on Wi-Fi: two brokers, one robot each."""
    one, two = FakeBroker(), FakeBroker()
    one.attach(robot_a)
    two.attach(robot_b)
    connect = _site({"192.168.50.1": one, "192.168.50.2": two})
    for robot, expected in ((robot_a, "192.168.50.1"), (robot_b, "192.168.50.2")):
        host = await resolve(
            serial=robot.serial, subnet="192.168.50.0/30", scan_wait=0.2, connect=connect
        )
        assert host == expected, f"{robot.serial} resolved to {host}, expected {expected}"


async def test_resolve_refuses_an_address_that_now_belongs_to_another_robot(
    robot_a: Simulator, robot_b: Simulator
) -> None:
    """A lease swap: robot A's old address now answers as robot B.

    An open port is not proof. Before this was checked, the entry for A connected to
    B's broker, heard nothing on its own serial, timed out, and did the same on every retry.
    """
    one, two = FakeBroker(), FakeBroker()
    one.attach(robot_b)  # swapped
    two.attach(robot_a)
    connect = _site({"192.168.50.1": one, "192.168.50.2": two})
    host = await resolve(
        serial=robot_a.serial,
        last_host="192.168.50.1",
        subnet="192.168.50.0/30",
        scan_wait=0.2,
        connect=connect,
    )
    assert host == "192.168.50.2", (
        f"{robot_a.serial} last lived at 192.168.50.1, which now carries {robot_b.serial}. "
        f"resolve() returned {host}; expected 192.168.50.2, where {robot_a.serial} is heard."
    )
    with pytest.raises(RobotNotFoundError):
        await resolve(
            serial="SN-gone",
            last_host="192.168.50.1",
            subnet="192.168.50.0/30",
            scan_wait=0.2,
            connect=connect,
        )


async def test_one_robot_behind_two_addresses_is_one_robot(robot_a: Simulator) -> None:
    """Rover and base station relay: same serial at two hosts. Seen on real hardware."""
    relay = FakeBroker()
    relay.attach(robot_a)
    connect = _site({"192.168.1.22": relay, "192.168.50.184": relay})
    hits = await discover.discover(
        ["192.168.1.22", "192.168.50.184"],
        wait=0.2,
        heartbeat_only=True,
        names=False,
        connect=connect,
    )
    assert [h.serials for h in hits] == [[robot_a.serial], [robot_a.serial]]
    assert len({s for h in hits for s in h.serials}) == 1, "one robot must not count as two"


async def test_a_dead_host_is_reported_not_raised(robot_a: Simulator) -> None:
    from yarbo_local.transport import FakeTransport  # noqa: PLC0415

    def connect(_host: str, _port: int) -> Transport:
        return FakeTransport(fail_next_connects=1)

    got = await discover.sample("10.0.0.9", wait=0.2, connect=connect)
    assert got.serials == []
    assert got.error is not None
    assert "simulated connect failure" in got.error


async def test_looking_for_one_robot_does_not_wait_on_the_others(
    robot_a: Simulator, robot_b: Simulator
) -> None:
    """Re-finding robot A on a two-robot site must not sit out the window on B's broker."""
    one, two = FakeBroker(), FakeBroker()
    one.attach(robot_a)
    two.attach(robot_b)
    connect = _site({"192.168.50.1": one, "192.168.50.2": two})
    loop = asyncio.get_running_loop()
    started = loop.time()
    host = await resolve(
        serial=robot_a.serial, subnet="192.168.50.0/30", scan_wait=5.0, connect=connect
    )
    took = loop.time() - started
    assert host == "192.168.50.1"
    assert took < 1.0, (
        f"resolve() took {took:.1f}s of a 5 s window. Expected it to return as soon as "
        f"{robot_a.serial} was heard, cancelling the listener on the other robot's broker."
    )
    assert all(not c.connected for c in (*one.clients, *two.clients)), "a listener was left open"
