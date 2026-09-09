"""``yarbo-local status``: connect through the library and print what it knows."""

from __future__ import annotations

import asyncio
import sys

from .client import YarboRobot
from .models import RobotState


def _fmt(value: object, unit: str = "") -> str:
    if value is None:
        return "?"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.2f}{unit}"
    return f"{value}{unit}"


def render(state: RobotState) -> str:
    """Multi-line summary. Local coordinates only; never the GNSS position."""
    pos = state.position
    fix = state.fix
    rows = [
        ("serial", state.serial),
        ("firmware", state.firmware),
        ("body firmware", state.body_firmware),
        ("head", f"{state.head_name} ({state.head_type})"),
        ("awake", state.awake),
        ("activity", state.activity.value),
        ("battery", _fmt(state.battery, "%")),
        ("battery health", _fmt(state.battery_health, "%")),
        ("charging", state.charging),
        ("battery current", _fmt(state.battery_current, " A")),
        ("error code", state.error_code),
        ("rtk status", f"{_fmt(state.rtk_status)} usable={_fmt(state.rtk_usable)}"),
        ("satellites", state.satellites),
        ("gga quality", fix.quality if fix else None),
        ("heading", _fmt(state.heading_degrees, " deg")),
        ("position", f"x={pos[0]:.2f} y={pos[1]:.2f} phi={pos[2]:.2f}" if pos else None),
        ("network path", state.network_path),
        ("halow rssi", _fmt(state.halow_rssi, " dBm")),
        ("ambient temp", _fmt(state.ambient_temperature, " C")),
        ("volume", state.volume),
        ("person detection", state.person_detection),
        ("child lock", state.child_lock),
    ]
    width = max(len(k) for k, _ in rows)
    return "\n".join(f"{k:<{width}}  {_fmt(v)}" for k, v in rows)


def render_line(state: RobotState) -> str:
    return (
        f"awake={_fmt(state.awake)} activity={state.activity.value} "
        f"battery={_fmt(state.battery)} rtk={_fmt(state.rtk_status)} error={state.error_code}"
    )


async def show(
    host: str,
    *,
    port: int = 1883,
    tls: bool = False,
    serial: str | None = None,
    wake: bool = False,
    watch: float = 0.0,
) -> int:
    robot = YarboRobot.for_host(host, port=port, tls=tls, serial=serial)
    async with robot:
        print(f"connected to {host}:{port}, robot {robot.serial}", file=sys.stderr)
        if wake:
            woke = await robot.wake()
            print("wake:", "awake" if woke else "no heartbeat change", file=sys.stderr)
        state = await robot.snapshot()
        print(render(state))
        if watch > 0:
            last = [render_line(state)]

            def on_change(new: RobotState) -> None:
                line = render_line(new)
                if line != last[0]:
                    last[0] = line
                    print(line)

            robot.on_state(on_change)
            print(f"watching for {watch:.0f}s", file=sys.stderr)
            await asyncio.sleep(watch)
    return 0
