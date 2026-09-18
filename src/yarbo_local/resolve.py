"""Address resolution: last known address, then DNS name, then subnet scan.

Home Assistant's passive discovery cannot see a robot on another VLAN, and a
DHCP lease can move. The order here is the one agreed for the integration.
"""

from __future__ import annotations

import asyncio
import logging
import socket

from . import discover
from .exceptions import RobotNotFoundError

_LOGGER = logging.getLogger(__name__)


async def _resolves(name: str) -> str | None:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(name, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError):
        return None
    return str(infos[0][4][0]) if infos else None


async def _carries(
    host: str,
    port: int,
    *,
    serial: str | None,
    connect_timeout: float,
    wait: float,
    connect: discover.TransportFactory | None,
) -> bool:
    """True when ``host`` answers and, if a serial is wanted, that robot is heard there.

    An open port is not enough. When a lease moves, the old address may belong to
    another robot or to some other broker, and a session scoped to our serial would
    sit on it in silence for ever.
    """
    if connect is None and not await discover.tcp_open(host, port, connect_timeout):
        return False
    if serial is None:
        return True
    got = await discover.sample(host, port, wait=wait, stop_on=serial, connect=connect)
    if serial not in got.serials:
        _LOGGER.info(
            "%s:%s answers but does not carry %s (heard: %s); looking elsewhere",
            host,
            port,
            serial,
            got.serials or got.error or "nothing",
        )
        return False
    return True


async def resolve(
    *,
    serial: str | None,
    last_host: str | None = None,
    dns_name: str | None = None,
    subnet: str | None = None,
    port: int = 1883,
    connect_timeout: float = 1.5,
    scan_wait: float = discover.HEARTBEAT_WINDOW,
    connect: discover.TransportFactory | None = None,
) -> str:
    """Return a host that answers on the broker port, trying the cheap options first."""
    candidates: list[str] = []
    if last_host:
        candidates.append(last_host)
    if dns_name:
        resolved = await _resolves(dns_name)
        if resolved and resolved not in candidates:
            candidates.append(resolved)
    for host in candidates:
        if await _carries(
            host,
            port,
            serial=serial,
            connect_timeout=connect_timeout,
            wait=scan_wait,
            connect=connect,
        ):
            return host
    if subnet:
        hits = await discover.discover(
            discover.expand(subnet),
            port=port,
            wait=scan_wait,
            heartbeat_only=True,
            names=False,
            stop_on=serial,
            connect=connect,
        )
        for hit in hits:
            if hit.serials and (serial is None or serial in hit.serials):
                return hit.host
    raise RobotNotFoundError(
        f"no broker for serial {serial or '?'} at {candidates or 'no candidates'}"
        + (f" or on {subnet}" if subnet else "")
    )
