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
from dataclasses import dataclass
import ipaddress
import secrets
import socket
import time

import aiomqtt

from . import topics


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


async def _listen(
    host: str, port: int, wait: float, *, heartbeat_only: bool
) -> tuple[list[str], list[str], float | None]:
    """Learn the serials a broker carries.

    ``heartbeat_only`` subscribes to the heartbeat alone and returns at the first
    one: the robot sends it awake or asleep, and it is all address resolution needs.
    Otherwise every topic is sampled, so the CLI can show what is flowing.
    """
    serials: set[str] = set()
    leaves: set[str] = set()
    started = time.monotonic()
    first: float | None = None
    identifier = f"yarbo-local-discover-{secrets.token_hex(3)}"
    topic = topics.heartbeats() if heartbeat_only else topics.all_for(None)
    try:
        async with aiomqtt.Client(host, port=port, identifier=identifier, timeout=wait) as client:
            await client.subscribe(topic)
            try:
                async with asyncio.timeout(wait):
                    async for message in client.messages:
                        parsed = topics.parse(message.topic.value)
                        if not parsed:
                            continue
                        if first is None:
                            first = time.monotonic() - started
                        serials.add(parsed.serial)
                        leaves.add(parsed.leaf)
                        if heartbeat_only or len(leaves) >= 4:
                            break
            except TimeoutError:
                pass
    except (aiomqtt.MqttError, OSError):
        return [], [], None
    return sorted(serials), sorted(leaves), first


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
    wait: float = 6.0,
    concurrency: int = 32,
    heartbeat_only: bool = False,
    names: bool = True,
) -> list[BrokerHit]:
    """Scan ``hosts`` for MQTT brokers that carry snowbot traffic.

    ``names`` adds a reverse DNS lookup per hit, run off the event loop. Address
    resolution inside Home Assistant turns it off, and uses ``heartbeat_only``.
    """
    sem = asyncio.Semaphore(concurrency)

    async def check(host: str) -> str | None:
        async with sem:
            return host if await tcp_open(host, port, connect_timeout) else None

    open_hosts = [h for h in await asyncio.gather(*(check(h) for h in hosts)) if h]
    hits: list[BrokerHit] = []
    for host in open_hosts:
        serials, leaves, first = await _listen(host, port, wait, heartbeat_only=heartbeat_only)
        hostname = await asyncio.to_thread(_reverse_name, host) if names else None
        hits.append(
            BrokerHit(
                host=host,
                port=port,
                serials=serials,
                leaves=leaves,
                first_message_after=first,
                hostname=hostname,
                guess=classify(hostname, serials),
            )
        )
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
