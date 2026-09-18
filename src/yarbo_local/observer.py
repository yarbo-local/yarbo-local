"""The app observer: what the phone app asked the robot, what it answered, and what changed.

Every command in the registry was learned the same way: someone used the Yarbo app while a
listener watched the robot's broker. This turns that watching into a list a person can read.
For each command the app (or this library) publishes it records the payload, where the
registry stands on it (verified, candidate, forbidden or never heard of), the robot's reply
if one came, and which of the fields that tell the robot's story changed in the seconds after.
A command the registry does not know, or a payload with keys it does not list, is news, and
is marked so.

Pure: feed it ``MessageEvent``s. Nothing here sends, reads a clock or touches a file.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from . import codec
from .blackbox import STORY_KEYS
from .registry import Registry
from .session import MessageEvent

REPLY_WITHIN = 5.0  # seconds a data_feedback may follow its command
EFFECTS_WITHIN = 8.0  # seconds of state changes credited to a command
KEEPALIVE = frozenset({"set_working_state"})
STREAMS = frozenset({"cmd_vel", "cmd_roller"})  # joystick streams: counted, never listed
MAX_EXCHANGES = 500


@dataclass(slots=True)
class Exchange:
    """One command and what followed it."""

    at: float
    name: str
    payload: Any
    sender: str  # "app" or "us"
    standing: str  # verified | candidate | forbidden | unknown
    new_keys: tuple[str, ...] = ()  # payload keys the registry's entry does not list
    reply: dict[str, Any] | None = None
    effects: list[dict[str, Any]] = field(default_factory=list)
    repeats: int = 1  # identical commands folded into this one (keep-alives)

    @property
    def news(self) -> bool:
        """Worth a person's attention: not yet verified, or not as the registry describes it."""
        return self.standing != "verified" or bool(self.new_keys)

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": round(self.at, 3),
            "name": self.name,
            "payload": self.payload,
            "sender": self.sender,
            "standing": self.standing,
            "new_keys": list(self.new_keys),
            "reply": self.reply,
            "effects": self.effects,
            "repeats": self.repeats,
            "news": self.news,
        }


def _cut(value: Any, limit: int = 400) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return f"<{len(value)} chars>"
    if isinstance(value, list) and len(value) > 12:
        return f"<{len(value)} items>"
    if isinstance(value, dict):
        return {k: _cut(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_cut(v, limit) for v in value]
    return value


class AppObserver:
    """Feed it every message; read ``exchanges``."""

    def __init__(self, registry: Registry) -> None:
        self.registry = registry
        self.exchanges: deque[Exchange] = deque(maxlen=MAX_EXCHANGES)
        self.streams: dict[str, int] = {}
        self._story: dict[str, Any] = {}

    def standing(self, name: str) -> str:
        if self.registry.is_forbidden(name):
            return "forbidden"
        if name not in self.registry:
            return "unknown"
        return "verified" if self.registry.get(name).verified else "candidate"

    def _new_keys(self, name: str, payload: Any) -> tuple[str, ...]:
        if name not in self.registry or not isinstance(payload, dict):
            return ()
        known = self.registry.get(name).payload
        if not isinstance(known, dict):
            return tuple(sorted(payload)) if payload and known in (None, {}) else ()
        return tuple(sorted(set(payload) - set(known)))

    def on_message(self, event: MessageEvent) -> Exchange | None:
        """Returns the exchange this message created or changed, for a live view."""
        if event.side == "app":
            return self._on_command(event)
        if event.leaf == "data_feedback" and isinstance(event.value, dict):
            return self._on_reply(event)
        if event.leaf == "DeviceMSG" and isinstance(event.value, dict):
            return self._on_state(event)
        return None

    def _on_command(self, event: MessageEvent) -> Exchange | None:
        if event.leaf in STREAMS:
            self.streams[event.leaf] = self.streams.get(event.leaf, 0) + 1
            return None
        sender = "us" if event.echo else "app"
        last = self.exchanges[-1] if self.exchanges else None
        if (
            event.leaf in KEEPALIVE
            and last is not None
            and (last.name, last.payload, last.sender) == (event.leaf, event.value, sender)
        ):
            last.repeats += 1
            return last
        exchange = Exchange(
            at=event.at,
            name=event.leaf,
            payload=_cut(event.value),
            sender=sender,
            standing=self.standing(event.leaf),
            new_keys=self._new_keys(event.leaf, event.value),
        )
        self.exchanges.append(exchange)
        return exchange

    def _on_reply(self, event: MessageEvent) -> Exchange | None:
        name = event.value.get("topic")
        for exchange in reversed(self.exchanges):
            if event.at - exchange.at > REPLY_WITHIN:
                break
            if exchange.name == name and exchange.reply is None:
                exchange.reply = {
                    "state": event.value.get("state"),
                    "msg": event.value.get("msg"),
                    "data": _cut(event.value.get("data")),
                    "latency_ms": round((event.at - exchange.at) * 1000),
                }
                return exchange
        return None

    def _on_state(self, event: MessageEvent) -> Exchange | None:
        flat = codec.flatten(event.value)
        story = {key: flat.get(key) for key in STORY_KEYS if key in flat}
        changed = {
            key: [self._story[key], value]
            for key, value in story.items()
            if key in self._story and self._story[key] != value
        }
        self._story.update(story)
        if not changed:
            return None
        # Credit the change to the latest command that is not a keep-alive and is recent enough.
        for exchange in reversed(self.exchanges):
            if event.at - exchange.at > EFFECTS_WITHIN:
                break
            if exchange.name in KEEPALIVE:
                continue
            exchange.effects.append({"after_ms": round((event.at - exchange.at) * 1000), **changed})
            return exchange
        return None

    def to_list(self, *, news_only: bool = False) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self.exchanges if e.news or not news_only]
