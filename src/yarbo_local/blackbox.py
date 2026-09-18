"""A flight recorder: the last few minutes of traffic, kept in memory, redacted on the way out.

A report that says "it stopped" cannot be acted on; one that carries what the robot and the
app said in the minutes before can. This keeps that, bounded in count and age, and costs
nothing until someone asks for it. Nothing is written to disk and nothing leaves the process.

What is kept: every command the app or we sent, every reply, the feedback topics thinned to
one in ``THIN_EVERY`` seconds, and a state frame only when one of the fields that tell the
story changed (with the frame before it, so the change can be read). Map blobs and paths are
cut to their size: the site's shape is not needed to debug a protocol exchange.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from typing import Any

from . import codec
from .redact import Redactor
from .session import MessageEvent, Session

MAX_RECORDS = 600
MAX_AGE_S = 900.0
THIN_EVERY = 10.0
BIG = 2000  # characters; anything longer is a map, a path or a blob

STORY_KEYS = (
    "StateMSG.on_going_planning",
    "StateMSG.planning_paused",
    "StateMSG.on_going_recharging",
    "StateMSG.error_code",
    "StateMSG.plan_msg",
    "StateMSG.car_controller",
    "StateMSG.machine_controller",
    "StateMSG.charging_status",
    "BatteryMSG.status",
    "HeadMsg.head_type",
    "RTKMSG.status",
)
QUIET_APP = frozenset({"cmd_vel", "cmd_roller"})  # joystick streams; forbidden and noisy


def _cut(value: Any) -> Any:
    """Replace anything large with its size, keeping the shape of the message."""
    if isinstance(value, dict):
        return {k: _cut(v) for k, v in value.items()}
    if isinstance(value, list):
        return f"<{len(value)} items>" if len(value) > 20 else [_cut(v) for v in value]
    if isinstance(value, str) and len(value) > BIG:
        return f"<{len(value)} chars>"
    return value


class FlightRecorder:
    """Attach to a session; ``dump()`` returns the recent past, redacted."""

    def __init__(self, *, max_records: int = MAX_RECORDS, max_age: float = MAX_AGE_S) -> None:
        self._records: deque[dict[str, Any]] = deque(maxlen=max_records)
        self._max_age = max_age
        self._story: tuple[Any, ...] | None = None
        self._held: dict[str, Any] | None = None  # the latest state frame not yet recorded
        self._last_thin: dict[str, float] = {}
        self._serials: set[str] = set()

    def attach(self, session: Session) -> Callable[[], None]:
        return session.add_message_listener(self.on_message)

    def note(self, at: float, what: str, **detail: Any) -> None:
        """Something the consumer knows and the wire does not: a refusal, a reconnect."""
        self._records.append({"t": round(at, 3), "note": what, **detail})

    def on_message(self, event: MessageEvent) -> None:
        self._serials.add(event.serial)
        if event.side == "app" and event.leaf in QUIET_APP:
            return
        record = {
            "t": round(event.at, 3),
            "from": "us" if event.echo else event.side,
            "leaf": event.leaf,
            "value": _cut(event.value),
        }
        if event.side == "device" and event.leaf == "DeviceMSG" and isinstance(event.value, dict):
            flat = codec.flatten(event.value)
            story = tuple(flat.get(key) for key in STORY_KEYS)
            record["value"] = dict(zip(STORY_KEYS, story, strict=True))
            if story == self._story:
                self._held = record
                return
            if self._held is not None:
                self._records.append(self._held)
            self._story, self._held = story, None
        elif event.side == "device" and event.leaf != "data_feedback":
            last = self._last_thin.get(event.leaf)
            if last is not None and event.at - last < THIN_EVERY:
                return
            self._last_thin[event.leaf] = event.at
        self._records.append(record)
        while self._records and event.at - self._records[0]["t"] > self._max_age:
            self._records.popleft()

    def dump(self) -> list[dict[str, Any]]:
        """The recent past, oldest first, with serials, positions and identifiers redacted."""
        redactor = Redactor()
        for serial in self._serials:
            redactor.register_serial(serial)
        records = list(self._records)
        if self._held is not None:
            records.append(self._held)
        return [redactor.redact_value(record) for record in sorted(records, key=lambda r: r["t"])]
