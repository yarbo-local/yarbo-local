"""High-level client: one robot, typed methods, resolution on connect."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
from types import TracebackType
from typing import Any

from .models import (
    Feedback,
    GpsReference,
    PlanSummary,
    RobotState,
    SiteMap,
    parse_gps_ref,
    parse_plans,
    parse_site_map,
)
from .registry import Registry
from .session import Session, StateListener
from .transport import MqttTransport, Transport


class YarboRobot:
    """Connect, read, and (within the registry's rules) command one robot."""

    def __init__(
        self,
        transport: Transport,
        *,
        serial: str | None = None,
        registry: Registry | None = None,
        allow_candidates: bool = False,
    ) -> None:
        self.session = Session(
            transport, serial=serial, registry=registry, allow_candidates=allow_candidates
        )
        self._task: asyncio.Task[None] | None = None

    @classmethod
    def for_host(
        cls,
        host: str,
        *,
        port: int = 1883,
        tls: bool = False,
        serial: str | None = None,
        allow_candidates: bool = False,
    ) -> YarboRobot:
        return cls(
            MqttTransport(host, port, tls=tls), serial=serial, allow_candidates=allow_candidates
        )

    # -- lifecycle

    async def start(self, ready_timeout: float = 15.0) -> None:
        self._task = asyncio.create_task(self.session.run(), name="yarbo-session")
        await self.session.wait_ready(ready_timeout)

    async def close(self) -> None:
        self.session.stop()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def __aenter__(self) -> YarboRobot:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    # -- state

    @property
    def state(self) -> RobotState:
        return self.session.state

    @property
    def serial(self) -> str | None:
        return self.session.serial

    def on_state(self, cb: StateListener) -> Callable[[], None]:
        return self.session.add_state_listener(cb)

    # -- reads (all verified on firmware 3.14.11)

    async def snapshot(self) -> RobotState:
        """Full ``get_device_msg`` snapshot, merged into the state and returned."""
        fb = await self.session.request("get_device_msg", timeout=10.0)
        if isinstance(fb.payload, dict):
            return await self.session.merge_snapshot(fb.payload)
        return self.session.state

    async def plans(self) -> list[PlanSummary]:
        fb = await self.session.request("read_all_plan")
        return parse_plans(fb.payload)

    async def gps_reference(self) -> GpsReference | None:
        fb = await self.session.request("read_gps_ref")
        return parse_gps_ref(fb.payload)

    async def site_map(self) -> SiteMap:
        fb = await self.session.request("get_map", timeout=30.0)
        payload = fb.payload
        return parse_site_map(payload if isinstance(payload, dict) else {})

    async def recharge_point(self) -> Any:
        return (await self.session.request("read_recharge_point")).payload

    async def global_params(self) -> Any:
        return (await self.session.request("read_global_params")).payload

    async def schedules(self) -> Any:
        return (await self.session.request("read_schedules")).payload

    async def raw_request(
        self, name: str, payload: Any = None, *, timeout: float = 8.0, confirmed: bool = False
    ) -> Feedback:
        """Registry-checked request for anything not wrapped above."""
        return await self.session.request(name, payload, timeout=timeout, confirmed=confirmed)

    # -- actions

    async def wake(self) -> bool:
        return await self.session.wake()
