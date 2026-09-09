"""Session: one robot, one transport, the protocol rules in one place.

Responsibilities, and nothing else:

- keep the transport connected, with backoff that never spins;
- decode inbound frames into :class:`RobotState` and notify listeners;
- send commands through the registry's rules, choosing the encoding from
  what the robot has shown us;
- correlate ``data_feedback`` replies to requests by the echoed ``topic``;
- take the controller role and wake the robot only when a command needs it;
- ignore our own publishes echoed back by the broker.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
import logging
import random
import time
from typing import Any

from . import codec, topics
from .exceptions import (
    CommandError,
    CommandRefusedError,
    ConnectionLostError,
    ControllerError,
    ReplyTimeoutError,
)
from .models import Feedback, Heartbeat, RobotState
from .registry import Command, Registry
from .transport import Message, Transport

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MessageEvent:
    """Every message on the robot's namespace, decoded, for observers."""

    at: float
    topic: str
    serial: str
    side: str
    leaf: str
    value: Any
    encoding: str
    size: int
    echo: bool
    raw: bytes


StateListener = Callable[[RobotState], Awaitable[None] | None]
MessageListener = Callable[[MessageEvent], Awaitable[None] | None]
TopicListener = Callable[[str, Any], Awaitable[None] | None]
ConnectionListener = Callable[[bool], Awaitable[None] | None]

BACKOFF_MIN = 1.0
BACKOFF_MAX = 60.0
WAKE_TIMEOUT = 15.0
CONTROLLER_TIMEOUT = 5.0
CONTROLLER_ACK = "successfully connected"
ECHO_WINDOW = 2.0


class Session:
    """Protocol session for one robot over one transport."""

    def __init__(
        self,
        transport: Transport,
        *,
        serial: str | None = None,
        registry: Registry | None = None,
        allow_candidates: bool = False,
        encoding: str = "auto",
    ) -> None:
        self.transport = transport
        self.serial = serial
        self.registry = registry or Registry.default()
        self.allow_candidates = allow_candidates
        self._encoding = encoding
        self.state = RobotState(serial=serial)
        self.connected = False
        self._state_listeners: list[StateListener] = []
        self._topic_listeners: list[TopicListener] = []
        self._message_listeners: list[MessageListener] = []
        self._connection_listeners: list[ConnectionListener] = []
        self._pending: dict[str, list[asyncio.Future[Feedback]]] = {}
        self._own_publishes: list[tuple[float, str, bytes]] = []
        self._seen_encodings: dict[str, codec.Encoding] = {}
        self._controller_held = False
        self._stop = asyncio.Event()
        self._ready = asyncio.Event()

    # -- listeners

    def add_state_listener(self, cb: StateListener) -> Callable[[], None]:
        self._state_listeners.append(cb)
        return lambda: self._state_listeners.remove(cb)

    def add_topic_listener(self, cb: TopicListener) -> Callable[[], None]:
        """Receive every decoded device or app message as ``(leaf_or_app_cmd, value)``."""
        self._topic_listeners.append(cb)
        return lambda: self._topic_listeners.remove(cb)

    def add_message_listener(self, cb: MessageListener) -> Callable[[], None]:
        """Observe every decoded message, including echoes of our own publishes."""
        self._message_listeners.append(cb)
        return lambda: self._message_listeners.remove(cb)

    def add_connection_listener(self, cb: ConnectionListener) -> Callable[[], None]:
        self._connection_listeners.append(cb)
        return lambda: self._connection_listeners.remove(cb)

    async def _emit(self, listeners: list[Any], *args: Any) -> None:
        for cb in list(listeners):
            try:
                result = cb(*args)
                if result is not None:
                    await result
            except Exception:
                _LOGGER.exception("listener %r failed", cb)

    # -- lifecycle

    async def run(self) -> None:
        """Keep the session alive until :meth:`stop`. Never returns early on disconnect."""
        delay = BACKOFF_MIN
        while not self._stop.is_set():
            try:
                await self.transport.connect()
                await self.transport.subscribe(topics.all_for(self.serial))
                await self._set_connected(True)
                delay = BACKOFF_MIN
                async for message in self.transport.messages():
                    await self._handle(message)
                    if self._stop.is_set():
                        break
            except ConnectionLostError as err:
                _LOGGER.info("connection lost (%s); retrying in %.1fs", err, delay)
            except asyncio.CancelledError:
                raise
            finally:
                await self._set_connected(False)
                await self.transport.close()
            if self._stop.is_set():
                return
            # Jittered exponential backoff. This sleep is the suspension point that
            # guarantees the loop cannot monopolise the event loop.
            await asyncio.sleep(delay * random.uniform(0.7, 1.3))  # noqa: S311
            delay = min(delay * 2, BACKOFF_MAX)

    def stop(self) -> None:
        self._stop.set()

    async def wait_ready(self, timeout: float = 15.0) -> None:
        """Wait until the first heartbeat has identified the robot."""
        async with asyncio.timeout(timeout):
            await self._ready.wait()

    async def _set_connected(self, value: bool) -> None:
        if self.connected == value:
            return
        self.connected = value
        if not value:
            self._controller_held = False
            for futures in self._pending.values():
                for fut in futures:
                    if not fut.done():
                        fut.set_exception(ConnectionLostError("disconnected while waiting"))
            self._pending.clear()
        await self._emit(self._connection_listeners, value)

    # -- inbound

    async def _handle(self, message: Message) -> None:
        parsed = topics.parse(message.topic)
        if parsed is None:
            return
        if self.serial is None:
            self.serial = parsed.serial
            self.state = RobotState(serial=parsed.serial)
        elif parsed.serial != self.serial:
            return
        value, enc = codec.decode(message.payload)
        echo = parsed.side == "app" and self._is_own_echo(message)
        if self._message_listeners:
            event = MessageEvent(
                at=time.time(),
                topic=message.topic,
                serial=parsed.serial,
                side=parsed.side,
                leaf=parsed.leaf,
                value=value,
                encoding=enc,
                size=len(message.payload),
                echo=echo,
                raw=message.payload,
            )
            await self._emit(self._message_listeners, event)
        if parsed.side == "app":
            if not echo:
                await self._emit(self._topic_listeners, f"app/{parsed.leaf}", value)
            return
        self._seen_encodings[parsed.leaf] = enc
        await self._handle_device(parsed.leaf, value)
        await self._emit(self._topic_listeners, parsed.leaf, value)

    async def _handle_device(self, leaf: str, value: Any) -> None:
        if not isinstance(value, dict):
            return
        now = time.time()
        changed = False
        if leaf == "heart_beat":
            hb = Heartbeat(int(value.get("working_state", 0) or 0))
            self.state, changed = self.state.with_heartbeat(hb, now)
            self._ready.set()
        elif leaf == "DeviceMSG":
            self.state, changed = self.state.with_frame(value, now)
            self._ready.set()
        elif leaf == "data_feedback":
            fb = Feedback.from_wire(value)
            for fut in self._pending.pop(fb.topic, []):
                if not fut.done():
                    fut.set_result(fb)
        if changed:
            await self._emit(self._state_listeners, self.state)

    async def merge_snapshot(self, frame: Mapping[str, Any]) -> RobotState:
        """Merge a full ``get_device_msg`` snapshot as if it were a pushed frame."""
        self.state, changed = self.state.with_frame(frame, time.time())
        if changed:
            await self._emit(self._state_listeners, self.state)
        return self.state

    def _is_own_echo(self, message: Message) -> bool:
        now = time.monotonic()
        self._own_publishes = [
            (t, topic, payload)
            for (t, topic, payload) in self._own_publishes
            if now - t < ECHO_WINDOW
        ]
        for _t, topic, payload in self._own_publishes:
            if topic == message.topic and payload == message.payload:
                return True
        return False

    # -- outbound

    def _compress(self) -> bool:
        if self._encoding == "zlib":
            return True
        if self._encoding == "json":
            return False
        decided = codec.firmware_wants_zlib(self.state.firmware)
        if decided is None:
            decided = codec.observed_encoding(self._seen_encodings)
        return True if decided is None else decided

    async def _publish(self, name: str, payload: Any) -> None:
        if self.serial is None:
            raise ConnectionLostError("robot serial unknown; wait_ready() first")
        raw = codec.encode(payload, compress=self._compress())
        topic = topics.app(self.serial, name)
        self._own_publishes.append((time.monotonic(), topic, raw))
        await self.transport.publish(topic, raw)

    def _check(self, name: str, confirmed: bool) -> Command:
        return self.registry.check(
            name, allow_candidates=self.allow_candidates, confirmed=confirmed
        )

    async def _prepare(self, cmd: Command) -> None:
        head = self.state.head_type
        if cmd.heads and head is not None and head not in cmd.heads:
            raise CommandRefusedError(
                f"{cmd.name!r} needs head {list(cmd.heads)}, attached is {head}"
            )
        if cmd.awake and self.state.awake is False:
            await self.wake()
        if cmd.controller and not self._controller_held:
            await self.ensure_controller()

    async def send(self, name: str, payload: Any = None, *, confirmed: bool = False) -> None:
        """Fire-and-forget publish through the registry rules."""
        cmd = self._check(name, confirmed)
        await self._prepare(cmd)
        await self._publish(name, cmd.payload if payload is None else payload)

    async def request(
        self,
        name: str,
        payload: Any = None,
        *,
        timeout: float = 8.0,
        confirmed: bool = False,
    ) -> Feedback:
        """Publish and wait for the correlated ``data_feedback`` reply."""
        cmd = self._check(name, confirmed)
        if not cmd.expects_reply:
            raise CommandRefusedError(f"{name!r} does not reply on data_feedback; use send()")
        await self._prepare(cmd)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Feedback] = loop.create_future()
        self._pending.setdefault(name, []).append(fut)
        try:
            await self._publish(name, cmd.payload if payload is None else payload)
            async with asyncio.timeout(timeout):
                fb = await fut
        except TimeoutError as err:
            raise ReplyTimeoutError(f"{name}: no reply within {timeout}s") from err
        finally:
            waiters = self._pending.get(name)
            if waiters and fut in waiters:
                waiters.remove(fut)
                if not waiters:
                    self._pending.pop(name, None)
        if not fb.ok:
            raise CommandError(name, fb.state, fb.msg)
        if name == "get_device_msg" and isinstance(fb.payload, dict):
            # The snapshot is authoritative; fold it into the state for every caller.
            await self.merge_snapshot(fb.payload)
        return fb

    # -- robot-level helpers

    async def wake(self, timeout: float = WAKE_TIMEOUT) -> bool:
        """Send the wake command and wait for the heartbeat to report awake."""
        if self.state.awake:
            return True
        cmd = self._check("set_working_state", confirmed=False)
        await self._publish(cmd.name, cmd.payload)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.state  # fresh read each turn; the run loop replaces it
            if state.awake:
                return True
            await asyncio.sleep(0.25)
        return False

    async def ensure_controller(self, timeout: float = CONTROLLER_TIMEOUT) -> None:
        """Take the controller role. Refuses under the same rules as any command."""
        cmd = self._check("get_controller", confirmed=False)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Feedback] = loop.create_future()
        self._pending.setdefault(cmd.name, []).append(fut)
        try:
            await self._publish(cmd.name, cmd.payload)
            async with asyncio.timeout(timeout):
                fb = await fut
        except TimeoutError as err:
            raise ControllerError(
                "no controller acknowledgement; close the Yarbo app and retry"
            ) from err
        finally:
            waiters = self._pending.get(cmd.name)
            if waiters and fut in waiters:
                waiters.remove(fut)
        if not fb.ok or CONTROLLER_ACK not in fb.msg.lower():
            self._controller_held = False
            raise ControllerError(f"controller refused: state={fb.state} msg={fb.msg!r}")
        self._controller_held = True
