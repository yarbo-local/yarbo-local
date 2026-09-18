"""The site check has to be right before anyone with two robots runs it.

Whoever runs it on a real multi-robot site should be testing their site, not our
test code. So it is exercised here three ways:

- end to end against simulated sites (shared broker, rover plus relay, two rovers);
- every failure path, by handing ``judge`` a record of a site that misbehaves;
- the scrubber, because the report is meant to be pasted into a public issue.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import contextlib
import logging
from pathlib import Path

import pytest

from yarbo_local import FakeBroker, FakeTransport, Simulator, sitecheck
from yarbo_local.sitecheck import Heard, Labels, Survey, Visit
from yarbo_local.transport import Transport

FIXTURES = Path(__file__).resolve().parents[1] / "protocol" / "fixtures" / "3.14.11"
FIXTURE = FIXTURES / "get_device_msg-asleep.jsonl"
MAP = FIXTURES / "get_map-area-pathway.jsonl"
SECOND = "SN-second-robot"


def _robots() -> tuple[Simulator, Simulator]:
    first = Simulator.from_fixture(FIXTURE, map_fixture=MAP)
    second = first.sibling(SECOND)
    second.snapshot["CombinedOdom"] = {"x": 25.0, "y": -40.0, "phi": 0.5}
    return first, second


def _site(brokers: dict[str, FakeBroker]) -> sitecheck.TransportFactory:
    def connect(host: str, _port: int) -> Transport:
        if host not in brokers:
            return FakeTransport(fail_next_connects=10**6)  # a dead address
        return brokers[host].client()

    return connect


@contextlib.asynccontextmanager
async def _heartbeats(*robots: Simulator) -> AsyncIterator[None]:
    """Robots heartbeat on their own clock; the fake broker has none."""

    async def beat() -> None:
        while True:
            for robot in robots:
                robot.tick()
            await asyncio.sleep(0.05)

    task = asyncio.create_task(beat())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def _status(findings: list[sitecheck.Finding]) -> dict[str, str]:
    return {f.check: f.status for f in findings}


# -- end to end on simulated sites


async def test_a_shared_broker_is_recognised_and_every_check_runs() -> None:
    a, b = _robots()
    broker = FakeBroker()
    broker.attach(a)
    broker.attach(b)
    async with _heartbeats(a, b):
        survey = await sitecheck.observe(
            "10.0.0.5", window=0.3, hold=0.1, connect=_site({"10.0.0.5": broker})
        )
    findings = sitecheck.judge(survey)
    assert survey.shared_brokers == {"10.0.0.5": sorted([a.serial, b.serial])}
    assert _status(findings) == {
        "found": "pass",
        "shared-broker": "info",
        "heartbeat-window": "pass",
        "isolation": "pass",
        "reads": "pass",
        "coexistence": "pass",
        "identity": "pass",
        "encoding": "pass",
        "unscoped": "pass",
        "resolve": "skip",  # a single host, not a subnet
        "maps": "info",
    }, "\n\n".join(f.render() for f in findings)
    assert [v.zones for v in survey.visits] == [2, 2]
    assert all(v.drops == 0 for v in survey.visits), "our own close must not count as a drop"


async def test_two_rovers_and_recovery_from_a_stale_address() -> None:
    a, b = _robots()
    one, two = FakeBroker(), FakeBroker()
    one.attach(a)
    two.attach(b)
    connect = _site({"192.168.50.1": one, "192.168.50.2": two})
    async with _heartbeats(a, b):
        survey = await sitecheck.observe("192.168.50.0/30", window=0.3, hold=0.1, connect=connect)
    status = _status(sitecheck.judge(survey))
    assert survey.resolved == {a.serial: "192.168.50.1", b.serial: "192.168.50.2"}
    assert status["resolve"] == "pass"
    assert status["shared-broker"] == "info"
    assert status["unscoped"] == "skip"


async def test_one_robot_behind_rover_and_relay() -> None:
    """Our own site: the same serial at two addresses, two sessions at once."""
    a, _ = _robots()
    relay = FakeBroker()
    relay.attach(a)
    connect = _site({"192.168.1.22": relay, "192.168.50.184": relay})
    async with _heartbeats(a):
        survey = await sitecheck.observe(
            "192.168.1.22,192.168.50.184", window=0.3, hold=0.1, connect=connect
        )
    status = _status(sitecheck.judge(survey))
    assert survey.serials == [a.serial]
    assert len(survey.visits) == 2
    assert status["coexistence"] == "pass"
    assert status["identity"] == "pass"
    assert status["shared-broker"] == "skip"


async def test_nothing_reachable_says_what_to_fix() -> None:
    survey = await sitecheck.observe("10.9.9.9", window=0.1, connect=_site({}))
    found = sitecheck.check_found(survey)
    assert found.status == "fail"
    assert "VLAN or firewall" in found.meaning
    assert survey.listen_errors  # the reason is kept, not swallowed


# -- every failure path, from a record of a site that misbehaves


def _survey(**heard: dict[str, Heard]) -> Survey:
    survey = Survey(spec="test", window=12.0)
    survey.heard = dict(heard)
    for host, robots in heard.items():
        survey.labels.host(host)
        for serial in robots:
            survey.labels.robot(serial)
    return survey


def _beats(*at: float) -> Heard:
    return Heard(heartbeat_at=list(at), leaves={"heart_beat"}, encodings={"heart_beat": "json"})


def test_a_slow_heartbeat_fails_with_the_constant_to_change() -> None:
    survey = _survey(h1={"A": _beats(0.5, 9.5)})
    finding = sitecheck.check_heartbeat_window(survey)
    assert finding.status == "fail"
    assert "longest gap 9.0s" in finding.seen
    assert "HEARTBEAT_WINDOW" in finding.meaning


def test_a_robot_heard_without_a_heartbeat_fails() -> None:
    survey = _survey(h1={"A": Heard(leaves={"DeviceMSG"})})
    assert sitecheck.check_heartbeat_window(survey).status == "fail"


def test_a_leaky_broker_is_reported_with_counts() -> None:
    survey = _survey(h1={"A": _beats(1, 3), "B": _beats(1, 3)})
    survey.visits = [Visit("h1", "A", foreign={"B": 17}), Visit("h1", "B")]
    finding = sitecheck.check_isolation(survey)
    assert finding.status == "fail"
    assert "robot-1 via host-1 received 17 messages for robot-2" in finding.seen


def test_failed_reads_name_the_step_and_the_address() -> None:
    survey = _survey(h1={"A": _beats(1, 3)})
    survey.visits = [Visit("h1", "A", errors=["during 'get_map': ReplyTimeoutError: no reply"])]
    finding = sitecheck.check_reads(survey)
    assert finding.status == "fail"
    assert "during 'get_map'" in finding.seen
    assert "robot-1 via host-1" in finding.seen


def test_sessions_that_kick_each_other_fail() -> None:
    survey = _survey(h1={"A": _beats(1, 3), "B": _beats(1, 3)})
    survey.visits = [Visit("h1", "A", drops=2), Visit("h1", "B")]
    finding = sitecheck.check_coexistence(survey)
    assert finding.status == "fail"
    assert "dropped 2x" in finding.seen


def test_two_serials_at_one_position_fail_identity() -> None:
    survey = _survey(h1={"A": _beats(1, 3), "B": _beats(1, 3)})
    survey.visits = [
        Visit("h1", "A", pose=(1.0, 2.0, 0.0)),
        Visit("h1", "B", pose=(1.0, 2.01, 0.0)),
    ]
    finding = sitecheck.check_identity(survey)
    assert finding.status == "fail"
    assert "same position" in finding.seen
    assert "1.0" not in finding.seen, "a position must never be written out"


def test_one_serial_in_two_places_fails_identity() -> None:
    survey = _survey(h1={"A": _beats(1, 3)}, h2={"A": _beats(1, 3)})
    survey.visits = [
        Visit("h1", "A", pose=(0.0, 0.0, 0.0)),
        Visit("h2", "A", pose=(30.0, 0.0, 0.0)),
    ]
    assert sitecheck.check_identity(survey).status == "fail"


def test_an_encoding_that_contradicts_the_firmware_rule_fails() -> None:
    heard = Heard(
        heartbeat_at=[1, 3], leaves={"heart_beat", "DeviceMSG"}, encodings={"DeviceMSG": "json"}
    )
    survey = _survey(h1={"A": heard})
    survey.visits = [Visit("h1", "A", firmware="3.14.11")]
    finding = sitecheck.check_encoding(survey)
    assert finding.status == "fail"
    assert "firmware 3.14.11 sends DeviceMSG as json" in finding.seen


def test_an_unscoped_session_that_misses_a_robot_fails() -> None:
    survey = _survey(h1={"A": _beats(1, 3), "B": _beats(1, 3), "C": _beats(1, 3)})
    survey.unscoped = {"h1": ("A", {"B"})}
    finding = sitecheck.check_unscoped(survey)
    assert finding.status == "fail"
    assert "robot-3" in finding.seen


def test_resolving_to_the_wrong_robots_address_fails() -> None:
    survey = _survey(h1={"A": _beats(1, 3)}, h2={"B": _beats(1, 3)})
    survey.resolved = {"A": "h2", "B": None}
    finding = sitecheck.check_resolve(survey)
    assert finding.status == "fail"
    assert "robot-1 -> host-2" in finding.seen
    assert "robot-2 -> not found" in finding.seen


def test_map_references_are_compared_but_never_written() -> None:
    survey = _survey(h1={"A": _beats(1, 3)}, h2={"B": _beats(1, 3)})
    survey.visits = [
        Visit("h1", "A", reference=(42.123456, -71.654321)),
        Visit("h2", "B", reference=(42.123456, -71.654321)),
    ]
    finding = sitecheck.check_maps(survey)
    assert finding.status == "info"
    assert "0.0 m apart" in finding.seen
    assert "42.1" not in finding.render()
    assert "71.6" not in finding.render()


# -- nothing private in what gets shared


def test_the_scrubber_covers_serials_hosts_topics_and_home() -> None:
    labels = Labels()
    labels.robot("260701036G38U142")
    labels.host("192.168.50.184")
    text = labels.scrub(
        "260701036G38U142 at 192.168.50.184, stray 10.1.2.3, topic snowbot/ZZ99UNKNOWN/device/x, "
        f"file {Path.home()}/Projects/yarbo-local/x.py"
    )
    assert text == (
        "robot-1 at host-1, stray host-?, topic snowbot/robot-?/device/x, "
        "file ~/Projects/yarbo-local/x.py"
    )


async def test_report_and_log_hold_no_serial_address_or_position(tmp_path: Path) -> None:
    a, b = _robots()
    a.serial = "260701036G38U142"  # shaped like a real one, so the test means something
    broker = FakeBroker()
    broker.attach(a)
    broker.attach(b)

    def work() -> tuple[Survey, list[sitecheck.Finding]]:
        return sitecheck.run(
            "10.0.0.5",
            window=0.3,
            hold=0.1,
            out_dir=tmp_path,
            connect=_site({"10.0.0.5": broker}),
        )

    async with _heartbeats(a, b):
        _, findings = await asyncio.to_thread(work)
    report = (tmp_path / "sitecheck-report.md").read_text()
    log = (tmp_path / "sitecheck.log").read_text()
    assert "robot-1" in report
    assert "robot-2" in report
    assert "## Findings" in report
    assert len(findings) == len(sitecheck.CHECKS)
    for private in (a.serial, SECOND, "10.0.0.5", "25.0", "-40.0"):
        assert private not in report, f"{private!r} leaked into the report"
        assert private not in log, f"{private!r} leaked into the log"
    assert "heard device/heart_beat for robot-" in log, "the log must show each message heard"


def test_a_traceback_in_the_log_is_scrubbed(tmp_path: Path) -> None:
    labels = Labels()
    labels.host("192.168.50.184")
    logger = logging.getLogger("yarbo_local.test_scrub")
    handler = logging.FileHandler(tmp_path / "x.log")
    handler.addFilter(sitecheck._ScrubFilter(labels))
    logger.addHandler(handler)

    def fail() -> None:
        raise ConnectionError("connect to 192.168.50.184:1883 failed")

    try:
        try:
            fail()
        except ConnectionError:
            logger.warning("visit failed", exc_info=True)
    finally:
        logger.removeHandler(handler)
        handler.close()
    text = (tmp_path / "x.log").read_text()
    assert "host-1:1883 failed" in text
    assert "192.168" not in text
    assert str(Path.home()) not in text


@pytest.mark.parametrize("check", sitecheck.CHECKS)
def test_every_check_copes_with_an_empty_record(check: object) -> None:
    finding = check(Survey(spec="nothing", window=12.0))  # type: ignore[operator]
    assert finding.status in {"skip", "fail"}
    assert finding.render()
