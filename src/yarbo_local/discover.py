"""Find Yarbo brokers on the LAN.

Two things may answer on port 1883: the rover itself and, on sites with a
base station, a relay broker on the base station. A broker is identified by
the serial in the topics it carries and by nothing else: no ARP table, no
subprocess, no MAC heuristics. Phase 0 showed the rover and the base station
answering behind one locally administered MAC with identical data, so the MAC
said nothing useful anyway.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
import ipaddress
import secrets
import socket
import time

from . import topics
from .exceptions import ConnectionLostError
from .transport import MqttTransport, Transport

# The robot heartbeats every 2 s awake and every 5 s asleep. A window longer than the
# slow interval hears every robot a broker carries, not only the first one to speak.
HEARTBEAT_WINDOW = 6.0

TransportFactory = Callable[[str, int], Transport]


@dataclass
class BrokerHit:
    """One host that accepted an MQTT connection."""

    host: str
    port: int
    serials: list[str]
    leaves: list[str]
    first_message_after: float | None
    hostname: str | None
    guess: str
    heartbeats: dict[str, int] = field(default_factory=dict)

    def render(self) -> str:
        return (
            f"{self.host}:{self.port}  serials={self.serials or '-'}  leaves={self.leaves or '-'}  "
            f"first_msg={self.first_message_after:.1f}s  "
            f"host={self.hostname or '?'}  guess={self.guess}"
            if self.first_message_after is not None
            else f"{self.host}:{self.port}  no snowbot traffic  "
            f"host={self.hostname or '?'}  guess={self.guess}"
        )


def classify(hostname: str | None, serials: list[str]) -> str:
    """Say what was observed, and only that."""
    if serials:
        named = " (hostname contains yarbo)" if hostname and "yarbo" in hostname.lower() else ""
        return f"carries snowbot traffic{named}"
    if hostname and "yarbo" in hostname.lower():
        return "hostname contains yarbo, but no snowbot traffic"
    return "open 1883 but no snowbot traffic"


async def tcp_open(host: str, port: int, timeout: float) -> bool:
    """Non-blocking connect on a raw socket.

    ``asyncio.open_connection`` resolves even IP literals through the default
    thread pool, so scanning a /24 with dozens of concurrent probes queues the
    lookups behind each other and the per-host timeout expires before the
    connect is attempted. A raw ``sock_connect`` on a numeric address avoids
    that path entirely.
    """
    loop = asyncio.get_running_loop()
    try:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
    except OSError:
        return False
    sock.setblocking(False)
    try:
        async with asyncio.timeout(timeout):
            await loop.sock_connect(sock, (host, port))
    except (TimeoutError, OSError):
        return False
    finally:
        sock.close()
    return True


@dataclass
class BrokerSample:
    """What one broker carried during one listening window."""

    serials: list[str]
    leaves: list[str]
    first_message_after: float | None
    heartbeats: dict[str, int]
    error: str | None = None


def _mqtt(host: str, port: int) -> Transport:
    return MqttTransport(host, port, identifier=f"yarbo-local-discover-{secrets.token_hex(3)}")


async def sample(
    host: str,
    port: int = 1883,
    *,
    wait: float = HEARTBEAT_WINDOW,
    heartbeat_only: bool = True,
    stop_on: str | None = None,
    connect: TransportFactory | None = None,
) -> BrokerSample:
    """Listen to one broker and report every serial it carries.

    A broker may carry more than one robot, so the whole window is heard out rather
    than stopping at the first heartbeat. ``stop_on`` names the one serial the caller
    is looking for and returns as soon as it is heard. ``heartbeat_only`` subscribes to
    heartbeats alone, which every robot sends awake or asleep; otherwise every topic is
    sampled so the CLI can show what is flowing.
    """
    serials: set[str] = set()
    leaves: set[str] = set()
    heartbeats: dict[str, int] = {}
    started = time.monotonic()
    first: float | None = None
    transport = (connect or _mqtt)(host, port)
    error: str | None = None
    try:
        await transport.connect()
        await transport.subscribe(topics.heartbeats() if heartbeat_only else topics.all_for(None))
        async with asyncio.timeout(wait):
            async for message in transport.messages():
                parsed = topics.parse(message.topic)
                if not parsed:
                    continue
                if first is None:
                    first = time.monotonic() - started
                serials.add(parsed.serial)
                leaves.add(parsed.leaf)
                if parsed.side == "device" and parsed.leaf == "heart_beat":
                    heartbeats[parsed.serial] = heartbeats.get(parsed.serial, 0) + 1
                if stop_on is not None and parsed.serial == stop_on:
                    break
    except TimeoutError:
        pass
    except (ConnectionLostError, OSError) as err:
        error = str(err)
    finally:
        await transport.close()
    return BrokerSample(sorted(serials), sorted(leaves), first, heartbeats, error)


def _reverse_name(host: str) -> str | None:
    try:
        return socket.gethostbyaddr(host)[0]
    except OSError:
        return None


async def discover(
    hosts: list[str],
    *,
    port: int = 1883,
    connect_timeout: float = 0.6,
    wait: float = HEARTBEAT_WINDOW,
    concurrency: int = 32,
    heartbeat_only: bool = False,
    names: bool = True,
    stop_on: str | None = None,
    connect: TransportFactory | None = None,
) -> list[BrokerHit]:
    """Scan ``hosts`` for MQTT brokers that carry snowbot traffic.

    Every open host is listened to for the whole window, all at once, so a site with
    several robots costs one window rather than one per robot. ``names`` adds a reverse
    DNS lookup per hit, run off the event loop. ``connect`` replaces the MQTT transport,
    and with it the TCP check, for tests.
    """
    sem = asyncio.Semaphore(concurrency)

    async def check(host: str) -> str | None:
        if connect is not None:
            return host
        async with sem:
            return host if await tcp_open(host, port, connect_timeout) else None

    async def listen(host: str) -> BrokerHit:
        async with sem:
            got = await sample(
                host,
                port,
                wait=wait,
                heartbeat_only=heartbeat_only,
                stop_on=stop_on,
                connect=connect,
            )
        hostname = await asyncio.to_thread(_reverse_name, host) if names else None
        return BrokerHit(
            host=host,
            port=port,
            serials=got.serials,
            leaves=got.leaves,
            first_message_after=got.first_message_after,
            hostname=hostname,
            guess=classify(hostname, got.serials),
            heartbeats=got.heartbeats,
        )

    open_hosts = [h for h in await asyncio.gather(*(check(h) for h in hosts)) if h]
    if stop_on is None:
        return list(await asyncio.gather(*(listen(h) for h in open_hosts)))

    # Looking for one robot: the moment one host carries it, stop listening to the rest
    # instead of sitting out the window on every broker that will never match.
    tasks = [asyncio.create_task(listen(h)) for h in open_hosts]
    hits: list[BrokerHit] = []
    try:
        for done in asyncio.as_completed(tasks):
            hit = await done
            hits.append(hit)
            if stop_on in hit.serials:
                break
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return hits


def expand(spec: str) -> list[str]:
    """Expand ``a.b.c.d``, ``a.b.c.d,e.f.g.h`` or ``a.b.c.0/24`` into host strings."""
    hosts: list[str] = []
    for raw_part in spec.split(","):
        part = raw_part.strip()
        if not part:
            continue
        if "/" in part:
            hosts.extend(str(ip) for ip in ipaddress.ip_network(part, strict=False).hosts())
        else:
            hosts.append(part)
    return hosts
