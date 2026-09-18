"""Site check: what does a site with one or several robots really look like?

Nobody on the project owns two robots. This module is how someone who does can
tell us, in one command, what we need to know, without sharing anything private:

    yarbo-local sitecheck 192.168.50.0/24

It works in two stages, on purpose. First it **observes** and records everything,
then it **judges** the record. A failed check therefore never hides the evidence
around it: the report always contains the whole picture.

It only listens and sends verified reads (``get_device_msg``, ``read_all_plan``,
``get_map``). It never wakes a robot, never takes the controller role from the
phone app, and cannot send a command that is not verified.

Everything written to the report and the log is labelled: serials become
``robot-1``, hosts become ``host-1``. Positions are never written at all.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
import logging
import math
from pathlib import Path
import platform
import re
import time

from . import __version__, codec, discover, topics
from .client import YarboRobot
from .exceptions import RobotNotFoundError, YarboError
from .models import METERS_PER_DEGREE
from .resolve import resolve
from .session import MessageEvent, Session
from .transport import MqttTransport, Transport

_LOGGER = logging.getLogger(__name__)

DEAD_HOST = "192.0.2.1"  # TEST-NET-1: never answers, stands in for a stale address
SLOWEST_HEARTBEAT = 5.0  # seconds between heartbeats of a sleeping robot, as measured
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_TOPIC_SERIAL = re.compile(r"snowbot/(?!robot-)[^/\s'\"]+")


# -- labels: nothing private leaves this module


class Labels:
    """Stable, meaningless names for serials and hosts, and a scrubber that applies them."""

    def __init__(self) -> None:
        self._serials: dict[str, str] = {}
        self._hosts: dict[str, str] = {}

    def robot(self, serial: str) -> str:
        return self._serials.setdefault(serial, f"robot-{len(self._serials) + 1}")

    def host(self, host: str) -> str:
        return self._hosts.setdefault(host, f"host-{len(self._hosts) + 1}")

    def scrub(self, text: str) -> str:
        for raw, label in sorted(self._serials.items(), key=lambda kv: -len(kv[0])):
            text = text.replace(raw, label)
        for raw, label in sorted(self._hosts.items(), key=lambda kv: -len(kv[0])):
            text = text.replace(raw, label)
        text = _TOPIC_SERIAL.sub("snowbot/robot-?", text)
        text = text.replace(str(Path.home()), "~")
        return _IPV4.sub("host-?", text)


class _ScrubFilter(logging.Filter):
    def __init__(self, labels: Labels) -> None:
        super().__init__()
        self._labels = labels

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._labels.scrub(record.getMessage())
        record.args = None
        if record.exc_info:
            # A traceback quotes exception text and file paths; scrub it like any message.
            text = logging.Formatter().formatException(record.exc_info)
            record.exc_text = self._labels.scrub(text)
            record.exc_info = None
        return True


# -- the record


@dataclass
class Heard:
    """One robot as heard on one host while only listening."""

    heartbeat_at: list[float] = field(default_factory=list)
    leaves: set[str] = field(default_factory=set)
    encodings: dict[str, str] = field(default_factory=dict)
    app_commands: set[str] = field(default_factory=set)

    @property
    def longest_gap(self) -> float | None:
        gaps = [b - a for a, b in zip(self.heartbeat_at, self.heartbeat_at[1:], strict=False)]
        return max(gaps) if gaps else None


@dataclass
class Visit:
    """One scoped session to one robot through one host, reads only."""

    host: str
    serial: str
    ready_after: float | None = None
    snapshot_ms: float | None = None
    firmware: str | None = None
    head: str | None = None
    awake: bool | None = None
    battery: int | None = None
    pose: tuple[float, float, float] | None = None  # never written out; compared only
    plans: int | None = None
    zones: int | None = None
    reference: tuple[float, float] | None = None  # never written out; compared only
    foreign: dict[str, int] = field(default_factory=dict)
    messages: int = 0
    drops: int = 0
    sent_zlib: bool | None = None
    errors: list[str] = field(default_factory=list)


@dataclass
class Survey:
    spec: str
    window: float
    hosts_scanned: int = 0
    heard: dict[str, dict[str, Heard]] = field(default_factory=dict)  # host -> serial -> Heard
    listen_errors: dict[str, str] = field(default_factory=dict)
    visits: list[Visit] = field(default_factory=list)
    unscoped: dict[str, tuple[str | None, set[str]]] = field(default_factory=dict)
    resolved: dict[str, str | None] = field(default_factory=dict)  # serial -> host or None
    labels: Labels = field(default_factory=Labels)

    @property
    def serials(self) -> list[str]:
        return sorted({s for robots in self.heard.values() for s in robots})

    def hosts_of(self, serial: str) -> list[str]:
        return sorted(h for h, robots in self.heard.items() if serial in robots)

    @property
    def shared_brokers(self) -> dict[str, list[str]]:
        return {h: sorted(r) for h, r in self.heard.items() if len(r) > 1}


TransportFactory = Callable[[str, int], Transport]


def _mqtt(host: str, port: int) -> Transport:
    return MqttTransport(host, port)


# -- stage one: observe


async def _listen(survey: Survey, host: str, port: int, connect: TransportFactory) -> None:
    """Hear everything one broker carries for the whole window."""
    heard: dict[str, Heard] = {}
    transport = connect(host, port)
    started = time.monotonic()
    try:
        await transport.connect()
        await transport.subscribe(topics.all_for(None))
        async with asyncio.timeout(survey.window):
            async for message in transport.messages():
                parsed = topics.parse(message.topic)
                if parsed is None:
                    continue
                robot = heard.setdefault(parsed.serial, Heard())
                survey.labels.host(host)
                survey.labels.robot(parsed.serial)
                _LOGGER.debug(
                    "+%6.2fs %s heard %s/%s for %s: %d bytes, %s",
                    time.monotonic() - started,
                    host,
                    parsed.side,
                    parsed.leaf,
                    parsed.serial,
                    len(message.payload),
                    "zlib" if codec.looks_zlib(message.payload) else "plain",
                )
                if parsed.side == "app":
                    robot.app_commands.add(parsed.leaf)
                    continue
                robot.leaves.add(parsed.leaf)
                robot.encodings[parsed.leaf] = codec.decode(message.payload)[1]
                if parsed.leaf == "heart_beat":
                    robot.heartbeat_at.append(time.monotonic() - started)
    except TimeoutError:
        pass
    except (YarboError, OSError) as err:
        survey.listen_errors[host] = str(err)
        _LOGGER.warning("listening to %s failed: %s", host, err)
    finally:
        await transport.close()
    # A serial heard only through someone's app publishes is not a robot answering here.
    robots = {s: h for s, h in heard.items() if h.leaves}
    for serial, robot in robots.items():
        survey.labels.robot(serial)
        _LOGGER.info(
            "%s carries %s: %d heartbeats, longest gap %s, leaves %s, encodings %s, "
            "app commands %s",
            host,
            serial,
            len(robot.heartbeat_at),
            f"{robot.longest_gap:.1f}s" if robot.longest_gap else "n/a",
            sorted(robot.leaves),
            robot.encodings,
            sorted(robot.app_commands),
        )
    if robots:
        survey.labels.host(host)
        survey.heard[host] = robots


async def _visit(visit: Visit, port: int, hold: float, connect: TransportFactory) -> None:
    """Connect scoped to one serial, run the verified reads, and count anything foreign."""
    transport = connect(visit.host, port)
    robot = YarboRobot(transport, serial=visit.serial)

    def on_message(_event: MessageEvent) -> None:
        visit.messages += 1

    closing = False

    def on_connection(connected: bool) -> None:
        _LOGGER.debug(
            "%s via %s: connection %s", visit.serial, visit.host, "up" if connected else "DOWN"
        )
        if not connected and visit.ready_after is not None and not closing:
            visit.drops += 1

    robot.session.add_message_listener(on_message)
    robot.session.add_connection_listener(on_connection)
    started = time.monotonic()
    step = "connect and wait for the first heartbeat"
    try:
        await robot.start(SLOWEST_HEARTBEAT * 3)
        visit.ready_after = time.monotonic() - started
        _LOGGER.debug("%s via %s: ready after %.2fs", visit.serial, visit.host, visit.ready_after)
        step = "get_device_msg"
        asked = time.monotonic()
        state = await robot.snapshot()
        visit.snapshot_ms = (time.monotonic() - asked) * 1000
        _LOGGER.debug(
            "%s via %s: get_device_msg answered in %.0f ms, %d top-level keys, firmware %s",
            visit.serial,
            visit.host,
            visit.snapshot_ms,
            len(state.raw),
            state.firmware,
        )
        visit.firmware = state.firmware
        visit.head = state.head_name
        visit.awake = state.awake
        visit.battery = state.battery
        visit.pose = state.position
        visit.sent_zlib = robot.session.sends_zlib
        step = "read_all_plan"
        visit.plans = len(await robot.plans())
        step = "get_map"
        site = await robot.site_map()
        visit.zones = len(site.zones)
        visit.reference = site.reference
        step = "stay connected alongside the other sessions"
        await asyncio.sleep(hold)
    except YarboError as err:
        visit.errors.append(f"during '{step}': {type(err).__name__}: {err}")
        _LOGGER.warning(
            "visit to %s via %s failed during '%s' after %.1fs, %d messages received so far",
            visit.serial,
            visit.host,
            step,
            time.monotonic() - started,
            visit.messages,
            exc_info=True,
        )
    finally:
        visit.foreign = dict(robot.session.foreign_messages)
        closing = True
        await robot.close()
    _LOGGER.info(
        "visit %s via %s: ready %.1fs, snapshot %s ms, firmware %s, head %s, awake %s, "
        "plans %s, zones %s, messages %d, foreign %s, drops %d, errors %s",
        visit.serial,
        visit.host,
        visit.ready_after or -1,
        f"{visit.snapshot_ms:.0f}" if visit.snapshot_ms is not None else "n/a",
        visit.firmware,
        visit.head,
        visit.awake,
        visit.plans,
        visit.zones,
        visit.messages,
        visit.foreign,
        visit.drops,
        visit.errors,
    )


async def _unscoped(survey: Survey, host: str, port: int, connect: TransportFactory) -> None:
    """What a session with no serial does on a broker that carries several robots."""
    session = Session(connect(host, port))
    task = asyncio.create_task(session.run())
    try:
        await session.wait_ready(SLOWEST_HEARTBEAT * 3)
        await asyncio.sleep(min(survey.window, SLOWEST_HEARTBEAT + 1))
    except TimeoutError:
        _LOGGER.warning("unscoped session on %s never became ready", host)
    finally:
        session.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    survey.unscoped[host] = (session.serial, set(session.other_serials))
    _LOGGER.info(
        "unscoped on %s took %s, also heard %s", host, session.serial, session.other_serials
    )


async def observe(
    spec: str,
    *,
    port: int = 1883,
    window: float = 12.0,
    hold: float = 6.0,
    connect: TransportFactory | None = None,
    labels: Labels | None = None,
) -> Survey:
    """Stage one. ``spec`` is a host, a comma list, or a CIDR. Reads only."""
    survey = Survey(spec=spec, window=window, labels=labels or Labels())
    hosts = discover.expand(spec)
    survey.hosts_scanned = len(hosts)
    factory = connect or _mqtt
    if connect is None:
        sem = asyncio.Semaphore(32)

        async def is_open(host: str) -> str | None:
            async with sem:
                return host if await discover.tcp_open(host, port, 0.6) else None

        hosts = [h for h in await asyncio.gather(*(is_open(h) for h in hosts)) if h]
    _LOGGER.info("%d of %d hosts answer on port %d", len(hosts), survey.hosts_scanned, port)

    await asyncio.gather(*(_listen(survey, h, port, factory) for h in hosts))

    # Every robot through every host that carries it, all at once: this is also the test
    # of whether sessions coexist on a shared broker and across a rover and its relay.
    survey.visits = [
        Visit(host=h, serial=s)
        for h, robots in sorted(survey.heard.items())
        for s in sorted(robots)
    ]
    await asyncio.gather(*(_visit(v, port, hold, factory) for v in survey.visits))
    await asyncio.gather(*(_unscoped(survey, h, port, factory) for h in survey.shared_brokers))

    if "/" in spec:
        for serial in survey.serials:
            try:
                survey.resolved[serial] = await resolve(
                    serial=serial,
                    last_host=DEAD_HOST,
                    subnet=spec,
                    port=port,
                    scan_wait=min(window, discover.HEARTBEAT_WINDOW),
                    connect=connect,
                )
            except RobotNotFoundError:
                survey.resolved[serial] = None
            _LOGGER.info(
                "resolve(%s) with a dead last address -> %s", serial, survey.resolved[serial]
            )
    return survey


# -- stage two: judge


@dataclass
class Finding:
    check: str
    status: str  # pass | fail | skip | info
    title: str
    expected: str = ""
    seen: str = ""
    meaning: str = ""
    send_us: str = ""

    @property
    def ok(self) -> bool:
        return self.status != "fail"

    def render(self) -> str:
        lines = [f"[{self.status.upper()}] {self.check}: {self.title}"]
        for label, value in (
            ("expected", self.expected),
            ("seen", self.seen),
            ("meaning", self.meaning),
            ("send us", self.send_us),
        ):
            if value:
                lines.append(f"  {label}: {value}")
        return "\n".join(lines)


REPORT_HINT = "the report file, sitecheck-report.md, and sitecheck.log; both are already scrubbed"


def _name(survey: Survey, visit: Visit) -> str:
    return f"{survey.labels.robot(visit.serial)} via {survey.labels.host(visit.host)}"


def check_found(s: Survey) -> Finding:
    if s.serials:
        return Finding(
            "found",
            "pass",
            f"{len(s.serials)} robot(s) on {len(s.heard)} broker address(es)",
            seen="; ".join(
                f"{s.labels.host(h)} carries {', '.join(s.labels.robot(r) for r in sorted(robots))}"
                for h, robots in sorted(s.heard.items())
            ),
        )
    return Finding(
        "found",
        "fail",
        "no robot answered",
        expected="at least one broker on port 1883 carrying snowbot topics",
        seen=f"{s.hosts_scanned} hosts scanned; errors: {s.listen_errors or 'none'}",
        meaning="This machine cannot reach the robots. Usually a VLAN or firewall rule: the "
        "machine "
        "running this needs TCP 1883 to the robots' network, and the robot must be powered.",
        send_us="nothing yet; fix reachability first (try the robot's IP instead of a subnet)",
    )


def check_shared_broker(s: Survey) -> Finding:
    """The open question this tool exists to answer."""
    if len(s.serials) < 2:
        return Finding(
            "shared-broker",
            "skip",
            "needs two robots to answer; this site has one",
            seen="one robot, reachable at "
            + ", ".join(s.labels.host(h) for h in s.hosts_of(s.serials[0]))
            if s.serials
            else "no robots",
        )
    shared = s.shared_brokers
    return Finding(
        "shared-broker",
        "info",
        "a broker here carries several robots" if shared else "every broker carries one robot",
        seen="; ".join(
            f"{s.labels.host(h)}: {', '.join(s.labels.robot(r) for r in robots)}"
            for h, robots in sorted(shared.items())
        )
        or "no address carried more than one serial",
        meaning="This was unknown until now. If a base station relays more than one rover, setup "
        "has to let you pick a robot; if not, one address always means one robot.",
        send_us=REPORT_HINT + ". This answer is valuable either way",
    )


def check_heartbeat_window(s: Survey) -> Finding:
    late = []
    for host, robots in s.heard.items():
        for serial, heard in robots.items():
            gap = heard.longest_gap
            if not heard.heartbeat_at or (gap is not None and gap > discover.HEARTBEAT_WINDOW):
                late.append(
                    f"{s.labels.robot(serial)} on {s.labels.host(host)}: "
                    f"{len(heard.heartbeat_at)} heartbeats in {s.window:.0f}s, longest gap "
                    f"{f'{gap:.1f}s' if gap is not None else 'n/a'}"
                )
    if not s.serials:
        return Finding("heartbeat-window", "skip", "no robots heard")
    if late:
        return Finding(
            "heartbeat-window",
            "fail",
            "a robot heartbeats more slowly than discovery waits",
            expected=f"every robot heard at least every {discover.HEARTBEAT_WINDOW:.0f}s "
            "(we measured 2 s awake, 5 s asleep)",
            seen="; ".join(late),
            meaning="Discovery and address recovery listen for one window and would miss this "
            "robot. The fix is raising discover.HEARTBEAT_WINDOW to cover the gap seen here.",
            send_us=REPORT_HINT + ", plus the firmware version and whether the robot was asleep",
        )
    return Finding(
        "heartbeat-window",
        "pass",
        f"every robot heartbeats within the {discover.HEARTBEAT_WINDOW:.0f}s discovery window",
    )


def check_isolation(s: Survey) -> Finding:
    leaks = [
        f"{_name(s, v)} received "
        + ", ".join(f"{n} messages for {s.labels.robot(r)}" for r, n in sorted(v.foreign.items()))
        for v in s.visits
        if v.foreign
    ]
    if not s.visits:
        return Finding("isolation", "skip", "no sessions were opened")
    if leaks:
        return Finding(
            "isolation",
            "fail",
            "a session scoped to one robot received another robot's messages",
            expected="a subscription to snowbot/<serial>/# delivers that serial only",
            seen="; ".join(leaks),
            meaning="The broker does not honour the topic filter. The library discarded these "
            "messages, so no entity showed another robot's state, but it costs bandwidth and "
            "it is not how the brokers we have seen behave, so we want to know.",
            send_us=REPORT_HINT,
        )
    return Finding(
        "isolation", "pass", f"{len(s.visits)} scoped session(s) heard only their own robot"
    )


def check_reads(s: Survey) -> Finding:
    failed = [f"{_name(s, v)}: {'; '.join(v.errors)}" for v in s.visits if v.errors]
    if not s.visits:
        return Finding("reads", "skip", "no sessions were opened")
    if failed:
        return Finding(
            "reads",
            "fail",
            "a robot did not answer the verified reads",
            expected="get_device_msg, read_all_plan and get_map answered through every address",
            seen=s.labels.scrub("; ".join(failed)),
            meaning="Either this address relays telemetry but not requests, or sessions interfere "
            "with each other when several are open. Both change how setup must pick an address.",
            send_us=REPORT_HINT + ", and whether the Yarbo app was open at the time",
        )
    return Finding("reads", "pass", "every robot answered every read through every address")


def check_coexistence(s: Survey) -> Finding:
    dropped = [f"{_name(s, v)} dropped {v.drops}x" for v in s.visits if v.drops]
    if len(s.visits) < 2:
        return Finding(
            "coexistence", "skip", "needs two sessions open at once; this site allowed one"
        )
    if dropped:
        return Finding(
            "coexistence",
            "fail",
            "sessions disconnected each other",
            expected=f"{len(s.visits)} sessions open together with no drops",
            seen="; ".join(dropped),
            meaning="The broker limits sessions, or kicks a client when another connects. Home "
            "Assistant with two robots, or HA plus the phone app, would flap.",
            send_us=REPORT_HINT,
        )
    return Finding("coexistence", "pass", f"{len(s.visits)} sessions stayed connected together")


def check_identity(s: Survey) -> Finding:
    """Different serials must be different machines; one serial must be one machine."""
    problems: list[str] = []
    by_serial: dict[str, list[Visit]] = {}
    for v in s.visits:
        if v.pose is not None:
            by_serial.setdefault(v.serial, []).append(v)
    serials = sorted(by_serial)
    for i, a in enumerate(serials):
        for b in serials[i + 1 :]:
            pa, pb = by_serial[a][0].pose, by_serial[b][0].pose
            if pa is not None and pb is not None and math.dist(pa[:2], pb[:2]) < 0.05:
                problems.append(
                    f"{s.labels.robot(a)} and {s.labels.robot(b)} report the same position to "
                    "within "
                    "5 cm"
                )
    for serial, visits in by_serial.items():
        poses = [v.pose for v in visits if v.pose is not None]
        if len(poses) > 1 and max(math.dist(poses[0][:2], p[:2]) for p in poses[1:]) > 2.0:
            problems.append(
                f"{s.labels.robot(serial)} reports positions more than 2 m apart through "
                "different addresses"
            )
    if len(s.visits) < 2:
        return Finding("identity", "skip", "needs two sessions to compare")
    if problems:
        return Finding(
            "identity",
            "fail",
            "a reply does not seem to come from the robot it was addressed to",
            expected="two robots in two places; one robot in one place whichever address is used",
            seen="; ".join(problems),
            meaning="A relay may be answering for the wrong robot, or replies are crossing. "
            "Two robots parked touching each other would also trip this; say so if that is so.",
            send_us=REPORT_HINT,
        )
    return Finding("identity", "pass", "each serial is one machine, and different serials differ")


def check_encoding(s: Survey) -> Finding:
    wrong = []
    for v in s.visits:
        rule = codec.firmware_wants_zlib(v.firmware)
        heard = s.heard.get(v.host, {}).get(v.serial)
        device_msg = heard.encodings.get("DeviceMSG") if heard else None
        if rule is not None and device_msg is not None and (device_msg == "zlib") != rule:
            wrong.append(
                f"{_name(s, v)}: firmware {v.firmware} sends DeviceMSG as {device_msg}, the rule "
                f"says {'zlib' if rule else 'plain'}"
            )
    if not s.visits:
        return Finding("encoding", "skip", "no sessions were opened")
    if wrong:
        return Finding(
            "encoding",
            "fail",
            "a robot's encoding contradicts the firmware rule",
            expected="zlib from firmware 3.9, plain JSON before",
            seen="; ".join(wrong),
            meaning="codec.firmware_wants_zlib has the wrong threshold for this firmware, and "
            "commands in the wrong encoding are dropped without a reply.",
            send_us=REPORT_HINT,
        )
    versions = sorted({v.firmware or "unknown" for v in s.visits})
    return Finding(
        "encoding", "pass", f"encodings match the firmware rule (firmware seen: {versions})"
    )


def check_unscoped(s: Survey) -> Finding:
    if not s.shared_brokers:
        return Finding("unscoped", "skip", "needs a broker carrying several robots; none here")
    silent = []
    for host, robots in s.shared_brokers.items():
        took, others = s.unscoped.get(host, (None, set()))
        if took is None or set(robots) - {took} - others:
            silent.append(
                f"{s.labels.host(host)}: took {s.labels.robot(took) if took else 'nothing'}, "
                f"noticed {sorted(s.labels.robot(o) for o in others)}, carries "
                f"{[s.labels.robot(r) for r in robots]}"
            )
    if silent:
        return Finding(
            "unscoped",
            "fail",
            "a session without a serial did not notice the other robots",
            expected="it follows the first robot heard and records every other serial",
            seen="; ".join(silent),
            meaning="Setup could attach to a robot by luck and never warn that there was a choice.",
            send_us=REPORT_HINT,
        )
    return Finding(
        "unscoped", "pass", "a session without a serial reports the robots it did not take"
    )


def check_resolve(s: Survey) -> Finding:
    if not s.resolved:
        return Finding(
            "resolve",
            "skip",
            "pass the robots' subnet in CIDR form to test recovery from a stale address",
        )
    lost = []
    for serial, host in s.resolved.items():
        if host is None or serial not in s.heard.get(host, {}):
            lost.append(
                f"{s.labels.robot(serial)} -> {s.labels.host(host) if host else 'not found'}, "
                "heard "
                f"at {[s.labels.host(h) for h in s.hosts_of(serial)]}"
            )
    if lost:
        return Finding(
            "resolve",
            "fail",
            "a robot could not be found again from a stale address",
            expected="each serial resolves to an address where that serial is heard",
            seen="; ".join(lost),
            meaning="After a DHCP change Home Assistant would lose this robot, or attach one "
            "robot's entities to another robot's address.",
            send_us=REPORT_HINT,
        )
    return Finding(
        "resolve", "pass", f"{len(s.resolved)} robot(s) found again from a stale address"
    )


def check_maps(s: Survey) -> Finding:
    refs = {v.serial: v.reference for v in s.visits if v.reference is not None}
    if len(refs) < 2:
        return Finding("maps", "skip", "needs two robots with a map each")
    serials = sorted(refs)
    apart = []
    for i, a in enumerate(serials):
        for b in serials[i + 1 :]:
            ra, rb = refs[a], refs[b]
            metres = math.hypot(
                (ra[0] - rb[0]) * METERS_PER_DEGREE,
                (ra[1] - rb[1]) * METERS_PER_DEGREE * math.cos(math.radians(ra[0])),
            )
            apart.append(
                f"{s.labels.robot(a)} and {s.labels.robot(b)}: references {metres:.1f} m apart"
            )
    return Finding(
        "maps",
        "info",
        "each robot keeps its own map",
        seen="; ".join(apart),
        meaning="References that are 0.0 m apart would mean robots share one map frame, which "
        "would let one card draw them together. Different references mean one card per robot.",
        send_us=REPORT_HINT,
    )


CHECKS: tuple[Callable[[Survey], Finding], ...] = (
    check_found,
    check_shared_broker,
    check_heartbeat_window,
    check_isolation,
    check_reads,
    check_coexistence,
    check_identity,
    check_encoding,
    check_unscoped,
    check_resolve,
    check_maps,
)


def judge(survey: Survey) -> list[Finding]:
    return [check(survey) for check in CHECKS]


# -- the report


def report(survey: Survey, findings: list[Finding]) -> str:
    lab = survey.labels
    counts = {k: sum(f.status == k for f in findings) for k in ("pass", "fail", "skip", "info")}
    out = [
        "# Yarbo Local site check",
        "",
        (
            f"yarbo-local {__version__}, Python {platform.python_version()}, {platform.system()}. "
            f"Listening window {survey.window:.0f} s, {survey.hosts_scanned} host(s) scanned."
        ),
        "",
        (
            "Serials and addresses are replaced by labels. No position is written anywhere in "
            "this file. Only verified reads were sent; no robot was woken and the controller "
            "role was not taken."
        ),
        "",
        (
            f"**{counts['fail']} failed, {counts['pass']} passed, {counts['info']} answered, "
            f"{counts['skip']} not applicable to this site.**"
        ),
        "",
        "## Site",
        "",
        "| Address | Robot | Heartbeats | Longest gap | Topics | DeviceMSG | App active |",
        "|---|---|---|---|---|---|---|",
    ]
    for host, robots in sorted(survey.heard.items()):
        for serial, heard in sorted(robots.items()):
            gap = heard.longest_gap
            out.append(
                f"| {lab.host(host)} | {lab.robot(serial)} | {len(heard.heartbeat_at)} | "
                f"{f'{gap:.1f} s' if gap is not None else 'n/a'} | "
                f"{', '.join(sorted(heard.leaves))} | "
                f"{heard.encodings.get('DeviceMSG', 'not seen (asleep)')} | "
                f"{', '.join(sorted(heard.app_commands)) or 'no'} |"
            )
    out += [
        "",
        "## Sessions",
        "",
        (
            "| Robot via address | Ready | Snapshot | Firmware | Head | Awake | Sent as | Plans | "
            "Zones | Messages | Foreign | Drops | Errors |"
        ),
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for v in survey.visits:
        out.append(
            f"| {_name(survey, v)} | "
            f"{f'{v.ready_after:.1f} s' if v.ready_after is not None else 'never'} | "
            f"{f'{v.snapshot_ms:.0f} ms' if v.snapshot_ms is not None else 'n/a'} | {v.firmware} | "
            f"{v.head} | {v.awake} | "
            f"{'n/a' if v.sent_zlib is None else 'zlib' if v.sent_zlib else 'plain'} | {v.plans} | "
            f"{v.zones} | {v.messages} | {sum(v.foreign.values())} | {v.drops} | "
            f"{lab.scrub('; '.join(v.errors)) or 'none'} |"
        )
    out += ["", "## Findings", ""]
    for finding in findings:
        out += ["```", lab.scrub(finding.render()), "```", ""]
    return "\n".join(out)


def run(
    spec: str,
    *,
    port: int = 1883,
    window: float = 12.0,
    hold: float = 6.0,
    out_dir: Path | None = None,
    connect: TransportFactory | None = None,
) -> tuple[Survey, list[Finding]]:
    """Observe, judge, and write ``sitecheck-report.md`` and ``sitecheck.log`` into ``out_dir``."""
    out_dir = out_dir or Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)
    labels = Labels()
    handler = logging.FileHandler(out_dir / "sitecheck.log", mode="w", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    handler.addFilter(_ScrubFilter(labels))
    root = logging.getLogger("yarbo_local")
    previous = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        survey = asyncio.run(
            observe(spec, port=port, window=window, hold=hold, connect=connect, labels=labels)
        )
        findings = judge(survey)
        for finding in findings:
            _LOGGER.info("%s", finding.render())
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)
        handler.close()
    (out_dir / "sitecheck-report.md").write_text(report(survey, findings), encoding="utf-8")
    return survey, findings
