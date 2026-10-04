"""High-level client: one robot, typed methods, resolution on connect."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Iterable, Sequence
import contextlib
from types import TracebackType
from typing import Any

from . import payloads
from .exceptions import CommandError, ConnectionLostError, PlanStartError, PreflightError
from .feedback import PlanFeedback
from .models import (
    Feedback,
    GpsReference,
    PlanSummary,
    RobotState,
    Schedule,
    SiteMap,
    parse_area_params,
    parse_gps_ref,
    parse_plans,
    parse_schedules,
    parse_site_map,
)
from .payloads import PointLike, RefLike
from .preflight import COMMANDS, Action, check
from .registry import Registry
from .session import Session, StateListener
from .transport import MqttTransport, Transport

# Seen on 3.14.11: a start that works reports route calculation within a second.
START_CONFIRM_S = 8.0


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
        registry: Registry | None = None,
        allow_candidates: bool = False,
    ) -> YarboRobot:
        return cls(
            MqttTransport(host, port, tls=tls),
            serial=serial,
            registry=registry,
            allow_candidates=allow_candidates,
        )

    # -- lifecycle

    async def start(
        self,
        ready_timeout: float = 15.0,
        *,
        spawn: Callable[[Coroutine[Any, Any, None]], asyncio.Task[None]] | None = None,
    ) -> None:
        """Run the session in the background and wait for the first heartbeat.

        ``spawn`` lets a host such as Home Assistant own the task. On timeout the
        session is torn down and :class:`ConnectionLostError` is raised.
        """
        coro = self.session.run()
        self._task = spawn(coro) if spawn else asyncio.create_task(coro, name="yarbo-session")
        try:
            await self.session.wait_ready(ready_timeout)
        except TimeoutError as err:
            await self.close()
            raise ConnectionLostError(
                f"no robot heartbeat within {ready_timeout:.0f}s (wrong host or unreachable)"
            ) from err

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
        await self.session.request("get_device_msg", timeout=10.0)
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

    async def area_params(self, area_id: int) -> dict[str, Any] | None:
        """Work settings for one area; None when the robot has no such area."""
        fb = await self.session.request("read_area_params", {"id": area_id})
        return parse_area_params(fb.payload, area_id)

    async def pathway_params(self, pathway_id: int) -> dict[str, Any] | None:
        fb = await self.session.request("read_pathway_params", {"id": pathway_id})
        return parse_area_params(fb.payload, pathway_id)

    async def recharge_point(self) -> Any:
        return (await self.session.request("read_recharge_point")).payload

    async def global_params(self) -> Any:
        return (await self.session.request("read_global_params")).payload

    async def schedules(self) -> list[Schedule]:
        return parse_schedules((await self.session.request("read_schedules")).payload)

    async def mower_area_params(self, area_id: int) -> dict[str, Any] | None:
        """Mowing settings for one area; None when the robot has no such area."""
        fb = await self.session.request("read_mower_area_params", {"id": area_id})
        return parse_area_params(fb.payload, area_id)

    async def plan_history(self) -> list[dict[str, Any]]:
        """Past runs as the robot keeps them, newest handling left to the caller."""
        payload = (await self.session.request("read_all_plan_history", timeout=15.0)).payload
        items = payload.get("data") if isinstance(payload, dict) else payload
        return [dict(item) for item in items or [] if isinstance(item, dict)]

    async def plan_progress(self) -> PlanFeedback | None:
        """The plan being worked, or the last one; None when the robot has none to report."""
        try:
            fb = await self.session.request("get_plan_feedback", {}, timeout=10.0)
        except CommandError as err:
            if err.state == -1:  # "no plan working"
                return None
            raise
        return PlanFeedback.from_wire(fb.payload)

    async def preview_route(self, plan_id: int, *, percent: int = 0) -> PlanFeedback | None:
        """The route the robot would drive for a plan, without moving it.

        Shaped like the progress of a running plan: one path per area, ``path_type`` 0
        for the fill and 1 for the lap along the edge; ``total_s`` is the estimate.
        """
        if not 0 <= percent <= 99:
            raise ValueError("percent must be 0 to 99")
        fb = await self.session.request(
            "preview_plan_path", {"id": plan_id, "percent": percent}, timeout=30.0
        )
        return PlanFeedback.from_wire(fb.payload)

    async def raw_request(
        self,
        name: str,
        payload: Any = None,
        *,
        timeout: float = 8.0,
        confirmed: bool = False,
        may_pause_plan: bool = False,
    ) -> Feedback:
        """Registry-checked request for anything not wrapped here."""
        return await self.session.request(
            name, payload, timeout=timeout, confirmed=confirmed, may_pause_plan=may_pause_plan
        )

    # -- actions

    async def wake(self) -> bool:
        return await self.session.wake()

    def can(self, action: Action) -> bool:
        """Whether the command behind ``action`` is verified, so a control may be offered."""
        return self.session.registry.sendable(
            COMMANDS[action], allow_candidates=self.session.allow_candidates
        )

    async def act(self, action: Action, *, plan_id: int | None = None, percent: int = 0) -> None:
        """Run a moving action: pre-flight first, then the verified command behind it.

        ``Action.START`` needs ``plan_id``; ``percent`` is where in the plan to begin, and only
        0 has been seen on the wire. Raises :class:`PreflightError` with the reasons when it
        cannot work now, and :class:`CommandRefusedError` while the command is still unverified.
        """
        payload: dict[str, int] | None = None
        if action is Action.START:
            if plan_id is None:
                raise ValueError("starting needs a plan id")
            if not 0 <= percent <= 99:
                raise ValueError("percent must be 0 to 99")
            payload = {"id": plan_id, "percent": percent}
        refusals = check(action, self.state, connected=self.session.connected)
        if refusals:
            raise PreflightError(action, refusals)
        if action is Action.START and plan_id is not None:
            await self._prepare_start(plan_id)
        # Going home takes the controller, which ends the mow; that is what was asked for.
        await self.session.send(COMMANDS[action], payload, may_pause_plan=action is Action.DOCK)

    async def _prepare_start(self, plan_id: int) -> None:
        """Send what the app sends before ``start_plan``, in the same order.

        The app checks that the plan's areas connect to the dock, then fetches the map,
        and never takes the controller. This copies the app; it does not decide whether a
        start works. On 2026-10-04 this sequence, and the app's exact messages with its
        keep-alive, stalled on the East Lawn path just as the older shortcut did on
        2026-10-02. The connectivity reply is not interpreted: it was three empty lists
        whether or not a start worked.
        """
        await self.wake()  # the app's keep-alive is already running when it starts a plan
        areas = next((plan.area_ids for plan in await self.plans() if plan.id == plan_id), ())
        if areas:
            await self.session.request("check_map_connectivity", {"ids": list(areas)})
        await self.session.request("get_map", timeout=30.0)

    async def dock(self) -> None:
        """Send the robot home. Also clears a latched fault, which ``resume`` cannot."""
        await self.act(Action.DOCK)

    async def resume(self) -> None:
        """Resume a paused plan."""
        await self.act(Action.RESUME)

    async def stop(self) -> None:
        """End the plan where the robot is. It stays there; ``dock()`` sends it home."""
        await self.act(Action.STOP)

    async def pause(self) -> None:
        """Pause the running plan where it is."""
        await self.act(Action.PAUSE)

    async def start_plan(
        self, plan_id: int, *, percent: int = 0, confirm_within: float | None = None
    ) -> None:
        """Start a plan the way the app does and wait for the robot's verdict.

        The plan's areas are checked for a link to the dock and the map is fetched first,
        as the app does; the controller is not taken. The robot never answers
        ``start_plan``. One that can start reports route calculation within a second; one
        that cannot leaves a negative planning code and says nothing else, and since that
        code may already be there from an earlier failure, only the running codes count as
        a start. Raises :class:`PlanStartError` otherwise.
        """
        await self.act(Action.START, plan_id=plan_id, percent=percent)
        wait = START_CONFIRM_S if confirm_within is None else confirm_within
        deadline = asyncio.get_running_loop().time() + wait
        while asyncio.get_running_loop().time() < deadline:
            if self.state.plan_running:
                return
            await asyncio.sleep(0.1)
        if self.state.plan_running:
            return
        raise PlanStartError(plan_id, self.state.plan_error)

    # -- plans and schedules (verified on 3.14.11; each call is the explicit confirmation)

    async def save_plan(
        self,
        name: str,
        area_ids: Sequence[int],
        *,
        plan_id: int | None = None,
        self_order: bool = True,
    ) -> int:
        """Create a plan, or with ``plan_id`` change its name and areas. Returns its id.

        The robot numbers a new plan itself and does not say which number it chose, so
        the id is found by reading the plans before and after.
        """
        body = payloads.plan(name, area_ids, plan_id=plan_id, self_order=self_order)
        if plan_id is not None:
            await self.session.request("save_plan", body, confirmed=True)
            return plan_id
        before = {plan.id for plan in await self.plans()}
        await self.session.request("save_plan", body, confirmed=True)
        created = sorted(plan.id for plan in await self.plans() if plan.id not in before)
        if len(created) != 1:
            raise CommandError("save_plan", 0, "the plan was saved but its id cannot be told")
        return created[0]

    async def delete_plan(self, plan_id: int) -> None:
        """Delete a plan. The robot deletes the schedules that use it as well."""
        await self.session.request("del_plan", {"id": plan_id}, confirmed=True)

    async def sort_plans(self, plan_ids: Sequence[int]) -> None:
        """Set the order the plans are listed in."""
        await self.session.request("sort_plan", {"ids": [int(i) for i in plan_ids]}, confirmed=True)

    async def save_schedule(
        self,
        *,
        plan_id: int,
        name: str,
        start_time: str,
        end_time: str,
        week_day: int,
        schedule_id: int | None = None,
        schedule_type: int = 3,
        enabled: bool = True,
        return_method: int = 2,
        resume_progress: bool = False,
        last_progress: int = 0,
    ) -> Schedule:
        """Store a schedule and return it as the robot holds it.

        Times are ``HH:MM:SS``. Without ``schedule_id`` the next free number is used, as
        the app does. See :class:`Schedule` for what is and is not known about the numbers.
        """
        if schedule_id is None:
            schedule_id = max((s.id for s in await self.schedules()), default=0) + 1
        body = payloads.schedule(
            schedule_id=schedule_id,
            plan_id=plan_id,
            name=name,
            start_time=start_time,
            end_time=end_time,
            week_day=week_day,
            schedule_type=schedule_type,
            enabled=enabled,
            return_method=return_method,
            resume_progress=resume_progress,
            last_progress=last_progress,
        )
        fb = await self.session.request("save_schedule", body, confirmed=True)
        stored = Schedule.from_wire(fb.payload)
        if stored is None:
            raise CommandError("save_schedule", fb.state, "no stored schedule in the reply")
        return stored

    async def delete_schedule(self, schedule_id: int) -> None:
        await self.session.request("del_schedule", {"id": schedule_id}, confirmed=True)

    # -- settings
    #
    # These take the controller, so the session refuses them while a plan runs unless
    # ``may_pause_plan`` is passed: taking the controller pauses the plan.

    async def set_sound(
        self, *, enabled: bool, volume: float, may_pause_plan: bool = False
    ) -> None:
        """Voice prompts on or off, and their volume from 0.0 to 1.0."""
        await self.session.request(
            "set_sound_param",
            payloads.sound(enabled=enabled, volume=volume),
            may_pause_plan=may_pause_plan,
        )

    async def set_person_detection(self, enabled: bool, *, may_pause_plan: bool = False) -> None:
        await self.session.request(
            "set_person_detect", {"state": 1 if enabled else 0}, may_pause_plan=may_pause_plan
        )

    async def set_child_lock(self, enabled: bool, *, may_pause_plan: bool = False) -> None:
        await self.session.request(
            "set_child_lock", {"state": bool(enabled)}, may_pause_plan=may_pause_plan
        )

    async def set_follow_mode(self, enabled: bool, *, may_pause_plan: bool = False) -> None:
        """The robot does not answer this; ``state.follow_mode`` follows within seconds."""
        await self.session.send(
            "set_follow_state", {"state": 1 if enabled else 0}, may_pause_plan=may_pause_plan
        )

    async def update_global_params(
        self, *, may_pause_plan: bool = False, **changes: Any
    ) -> dict[str, Any]:
        """Change general settings, for example ``recharge_battery=25``.

        The robot stores one record. It is read, the keys the app sends are kept, the
        changes applied, and the whole record sent back. Returns what was sent.
        """
        current = await self.global_params()
        if not isinstance(current, dict):
            raise CommandError("read_global_params", 0, "no settings record in the reply")
        record = payloads.changed_record(
            current,
            payloads.APP_GLOBAL_PARAM_KEYS,
            changes,
            fixed={"id": payloads.GLOBAL_PARAMS_ID},
        )
        await self.session.request(
            "save_global_params", record, confirmed=True, may_pause_plan=may_pause_plan
        )
        return record

    async def update_mower_area_params(self, area_id: int, **changes: Any) -> dict[str, Any]:
        """Change the mowing settings of one area, for example ``edge_circles=1``.

        A mapping updates the stored mapping: ``first_clean_params={"blade_height": 60}``
        keeps the other values of the first pass. Returns the record that was sent.
        """
        current = await self.mower_area_params(area_id)
        if current is None:
            raise ValueError(f"the robot has no area {area_id}")
        record = payloads.changed_record(
            current,
            payloads.APP_MOWER_AREA_KEYS,
            changes,
            fixed={"id": area_id, "mapping_module_type": payloads.MAPPING_MODULE_TYPE},
        )
        await self.session.request("save_mower_area_params", record, confirmed=True)
        return record

    # -- the map
    #
    # Points are metres from ``reference``, x west and y north, as ``site_map()`` gives
    # them; ``reference`` is the (latitude, longitude) the map's zones carry as ``ref``.
    # Without an id the robot creates the object and numbers it; no-go zones, pathways
    # and sidewalks share one counter. The stored record comes back.

    async def save_nogozone(
        self,
        name: str,
        outline: Iterable[PointLike],
        reference: RefLike,
        *,
        zone_id: int | None = None,
        enabled: bool = True,
        may_pause_plan: bool = False,
    ) -> dict[str, Any]:
        body = payloads.nogozone(name, outline, reference, zone_id=zone_id, enabled=enabled)
        return await self._save_map_object("save_nogozone", body, may_pause_plan)

    async def delete_nogozone(self, zone_id: int, *, may_pause_plan: bool = False) -> None:
        await self.session.request(
            "del_nogozone", {"id": zone_id}, confirmed=True, may_pause_plan=may_pause_plan
        )

    async def save_pathway(
        self,
        name: str,
        line: Iterable[PointLike],
        reference: RefLike,
        *,
        pathway_id: int | None = None,
    ) -> dict[str, Any]:
        """The robot links a pathway to the dock or an area by where its ends lie."""
        body = payloads.pathway(name, line, reference, pathway_id=pathway_id)
        return await self._save_map_object("save_pathway", body, False)

    async def delete_pathway(self, pathway_id: int, *, may_pause_plan: bool = False) -> None:
        await self.session.request(
            "del_pathway", {"id": pathway_id}, confirmed=True, may_pause_plan=may_pause_plan
        )

    async def save_sidewalk(
        self,
        name: str,
        line: Iterable[PointLike],
        reference: RefLike,
        *,
        sidewalk_id: int | None = None,
        may_pause_plan: bool = False,
    ) -> dict[str, Any]:
        body = payloads.sidewalk(name, line, reference, sidewalk_id=sidewalk_id)
        return await self._save_map_object("save_sidewalk", body, may_pause_plan)

    async def delete_sidewalk(self, sidewalk_id: int, *, may_pause_plan: bool = False) -> None:
        await self.session.request(
            "del_sidewalk", {"id": sidewalk_id}, confirmed=True, may_pause_plan=may_pause_plan
        )

    async def update_area(
        self,
        area_id: int,
        *,
        name: str | None = None,
        algorithm_type: int | None = None,
        outline: Iterable[PointLike] | None = None,
    ) -> dict[str, Any]:
        """Edit an area in place and return the stored record.

        ``algorithm_type`` 0 plans rows, 1 a contour spiral, 4 rows with fewer turns.
        """
        payload = (await self.session.request("read_all_clean_area", timeout=15.0)).payload
        items = payload.get("data") if isinstance(payload, dict) else payload
        current = next(
            (a for a in items or [] if isinstance(a, dict) and a.get("id") == area_id), None
        )
        if current is None:
            raise ValueError(f"the robot has no area {area_id}")
        changes: dict[str, Any] = {}
        if name is not None:
            changes["name"] = name
        if algorithm_type is not None:
            changes["algorithm_type"] = int(algorithm_type)
        if outline is not None:
            corners = payloads.points(outline)
            if len(corners) < 3:
                raise ValueError("an area needs at least three points")
            changes["range"] = corners
        record = payloads.changed_record(
            current, payloads.APP_AREA_KEYS, changes, fixed={"id": area_id}
        )
        return await self._save_map_object("save_clean_area", record, False)

    async def _save_map_object(
        self, command: str, body: dict[str, Any], may_pause_plan: bool
    ) -> dict[str, Any]:
        fb = await self.session.request(
            command, body, timeout=15.0, confirmed=True, may_pause_plan=may_pause_plan
        )
        stored = fb.payload
        if not isinstance(stored, dict):
            raise CommandError(command, fb.state, "no stored record in the reply")
        return dict(stored)

    # -- head and charging

    async def set_blade_height(self, millimetres: int, *, may_pause_plan: bool = False) -> None:
        """Move the mower deck to a cutting height. The robot does not answer; it took
        five to seven seconds to move 20 mm."""
        if millimetres <= 0:
            raise ValueError("the height is in millimetres, above zero")
        await self.session.send(
            "mower_target_cmd", {"target": int(millimetres)}, may_pause_plan=may_pause_plan
        )

    async def start_charging(self) -> None:
        """Start charging on the dock. For a robot that docked and did not begin by itself."""
        await self.session.send("wireless_charging_cmd", {"cmd": 1}, confirmed=True)
