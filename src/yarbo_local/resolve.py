"""Address resolution: last known address, then DNS name, then subnet scan.

Home Assistant's passive discovery cannot see a robot on another VLAN, and a
DHCP lease can move. The order here is the one agreed for the integration.
"""

from __future__ import annotations

import asyncio
import socket

from . import discover
from .exceptions import RobotNotFoundError


async def _resolves(name: str) -> str | None:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(name, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError):
        return None
    return str(infos[0][4][0]) if infos else None


async def resolve(
    *,
    serial: str | None,
    last_host: str | None = None,
    dns_name: str | None = None,
    subnet: str | None = None,
    port: int = 1883,
    connect_timeout: float = 1.5,
    scan_wait: float = 6.0,
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
        if await discover._tcp_open(host, port, connect_timeout):
            return host
    if subnet:
        hits = await discover.discover(discover.expand(subnet), port=port, wait=scan_wait)
        for hit in hits:
            if hit.serials and (serial is None or serial in hit.serials):
                return hit.host
    raise RobotNotFoundError(
        f"no broker for serial {serial or '?'} at {candidates or 'no candidates'}"
        + (f" or on {subnet}" if subnet else "")
    )
