"""Find Yarbo brokers on the LAN and classify what answered.

Two things may answer on port 1883: the rover itself and, on sites with a
base station, a relay broker on the base station. Nothing here is asserted
beyond what the probe observes. The MAC hints come from the community
project and are labelled as hints until Phase 0 confirms them.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import ipaddress
import re
import secrets
import shutil
import socket
import subprocess
import time

import aiomqtt

from . import topics

ROVER_OUI_HINT = "C8:FE:0F"
RELAY_OUI_HINT = "E0:4E:7A"
_ARP_MAC_RE = re.compile(r"((?:[0-9a-fA-F]{1,2}[:-]){5}[0-9a-fA-F]{1,2})")


@dataclass
class BrokerHit:
    """One host that accepted an MQTT connection."""

    host: str
    port: int
    serials: list[str]
    leaves: list[str]
    first_message_after: float | None
    mac: str | None
    hostname: str | None
    guess: str

    def render(self) -> str:
        return (
            f"{self.host}:{self.port}  serials={self.serials or '-'}  leaves={self.leaves or '-'}  "
            f"first_msg={self.first_message_after:.1f}s  mac={self.mac or '?'}  "
            f"host={self.hostname or '?'}  guess={self.guess}"
            if self.first_message_after is not None
            else f"{self.host}:{self.port}  no snowbot traffic  mac={self.mac or '?'}  "
            f"host={self.hostname or '?'}  guess={self.guess}"
        )


def _normalise_mac(mac: str) -> str:
    return ":".join(part.zfill(2).upper() for part in re.split(r"[:-]", mac))


def arp_mac(ip: str) -> str | None:
    """Read the MAC for ``ip`` from the local ARP table (macOS and Linux)."""
    arp = shutil.which("arp")
    if not arp:
        return None
    try:
        out = subprocess.run(  # noqa: S603 - fixed binary, one IP argument
            [arp, "-n", ip], capture_output=True, text=True, timeout=3, check=False
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return None
    match = _ARP_MAC_RE.search(out)
    return _normalise_mac(match.group(1)) if match else None


def classify(mac: str | None, hostname: str | None, serials: list[str]) -> str:
    """Return a human guess. Hints only; Phase 0 decides what is true."""
    if mac:
        if mac.startswith(ROVER_OUI_HINT):
            return "rover (OUI hint)"
        if mac.startswith(RELAY_OUI_HINT):
            return "base station relay (OUI hint)"
        first_octet = int(mac.split(":")[0], 16)
        if first_octet & 0x02:
            return "locally administered MAC; relay per community hint"
    if hostname and "yarbo" in hostname.lower():
        return "hostname contains yarbo"
    if serials:
        return "answers snowbot traffic; type unknown"
    return "open 1883 but no snowbot traffic"


async def _tcp_open(host: str, port: int, timeout: float) -> bool:
    try:
        async with asyncio.timeout(timeout):
            _, writer = await asyncio.open_connection(host, port)
    except (TimeoutError, OSError):
        return False
    writer.close()
    return True


async def _listen(host: str, port: int, wait: float) -> tuple[list[str], list[str], float | None]:
    serials: set[str] = set()
    leaves: set[str] = set()
    started = time.monotonic()
    first: float | None = None
    identifier = f"yarbo-local-discover-{secrets.token_hex(3)}"
    try:
        async with aiomqtt.Client(host, port=port, identifier=identifier, timeout=wait) as client:
            await client.subscribe(topics.all_for(None))
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
                        if len(leaves) >= 4:
                            break
            except TimeoutError:
                pass
    except (aiomqtt.MqttError, OSError):
        return [], [], None
    return sorted(serials), sorted(leaves), first


async def discover(
    hosts: list[str],
    *,
    port: int = 1883,
    connect_timeout: float = 0.6,
    wait: float = 6.0,
    concurrency: int = 64,
) -> list[BrokerHit]:
    """Scan ``hosts`` for MQTT brokers that carry snowbot traffic."""
    sem = asyncio.Semaphore(concurrency)

    async def check(host: str) -> str | None:
        async with sem:
            return host if await _tcp_open(host, port, connect_timeout) else None

    open_hosts = [h for h in await asyncio.gather(*(check(h) for h in hosts)) if h]
    hits: list[BrokerHit] = []
    for host in open_hosts:
        serials, leaves, first = await _listen(host, port, wait)
        mac = arp_mac(host)
        try:
            hostname = socket.gethostbyaddr(host)[0]
        except OSError:
            hostname = None
        hits.append(
            BrokerHit(
                host=host,
                port=port,
                serials=serials,
                leaves=leaves,
                first_message_after=first,
                mac=mac,
                hostname=hostname,
                guess=classify(mac, hostname, serials),
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
