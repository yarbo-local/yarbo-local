"""Sniffer: subscribe to the robot's broker and write everything to JSONL.

One record per message::

    {"t": 1757260000.123, "ts": "2026-09-07T14:26:40.123Z",
     "topic": "snowbot/SN/device/DeviceMSG", "bytes": 2310, "enc": "zlib",
     "payload": {...}}

Both sides of the conversation are captured (``snowbot/+/#``) so that
commands published by the phone app are recorded next to the robot's replies.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
from pathlib import Path
import secrets
import sys
import time
from typing import Any, TextIO

import aiomqtt

from . import codec, topics
from .redact import Redactor


@dataclass
class CaptureStats:
    """Running counters printed at the end of a capture."""

    started: float = field(default_factory=time.monotonic)
    messages: int = 0
    bytes_total: int = 0
    per_topic: Counter[str] = field(default_factory=Counter)
    encodings: Counter[str] = field(default_factory=Counter)
    serials: set[str] = field(default_factory=set)
    first_seen: dict[str, float] = field(default_factory=dict)
    last_seen: dict[str, float] = field(default_factory=dict)

    def note(self, topic: str, size: int, enc: str, now: float) -> None:
        self.messages += 1
        self.bytes_total += size
        self.per_topic[topic] += 1
        self.encodings[enc] += 1
        self.first_seen.setdefault(topic, now)
        self.last_seen[topic] = now
        parsed = topics.parse(topic)
        if parsed:
            self.serials.add(parsed.serial)

    def render(self) -> str:
        elapsed = max(time.monotonic() - self.started, 1e-6)
        lines = [
            (
                f"messages={self.messages} bytes={self.bytes_total} elapsed={elapsed:.1f}s "
                f"encodings={dict(self.encodings)} serials={sorted(self.serials) or '-'}"
            ),
            f"{'topic':60} {'count':>7} {'rate/s':>8} {'span s':>8}",
        ]
        for topic, count in self.per_topic.most_common():
            span = self.last_seen[topic] - self.first_seen[topic]
            rate = count / span if span > 0 else 0.0
            lines.append(f"{topic:60} {count:7d} {rate:8.2f} {span:8.1f}")
        return "\n".join(lines)


def build_record(
    topic: str, raw: bytes, redactor: Redactor | None, *, at: float | None = None
) -> dict[str, Any]:
    """One JSONL record for a raw message, redacted when a redactor is given."""
    now = time.time() if at is None else at
    payload, enc = codec.decode(raw)
    if redactor is not None:
        parsed = topics.parse(topic)
        if parsed:
            redactor.register_serial(parsed.serial)
        topic = redactor.redact_topic(topic)
        payload = redactor.redact_value(payload)
    return {
        "t": round(now, 3),
        "ts": datetime.fromtimestamp(now, tz=UTC).isoformat(timespec="milliseconds"),
        "topic": topic,
        "bytes": len(raw),
        "enc": enc,
        "payload": payload,
    }


async def sniff(
    host: str,
    port: int = 1883,
    *,
    out: Path | None = None,
    duration: float | None = None,
    topic_filter: str | None = None,
    redact: bool = False,
    echo: TextIO | None = None,
    stats_every: float = 15.0,
) -> CaptureStats:
    """Capture until ``duration`` seconds elapse or the process is interrupted."""
    stats = CaptureStats()
    redactor = Redactor() if redact else None
    sub = topic_filter or topics.all_for(None)
    log = echo or sys.stderr
    fh = out.open("a", encoding="utf-8") if out else None
    identifier = f"yarbo-local-sniff-{secrets.token_hex(3)}"

    async def _run() -> None:
        async with aiomqtt.Client(host, port=port, identifier=identifier) as client:
            await client.subscribe(sub)
            print(f"connected to {host}:{port}, subscribed {sub}", file=log)
            next_stats = time.monotonic() + stats_every
            async for message in client.messages:
                raw = message.payload if isinstance(message.payload, bytes) else b""
                rec = build_record(message.topic.value, raw, redactor)
                stats.note(message.topic.value, rec["bytes"], rec["enc"], time.monotonic())
                if fh:
                    fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
                    fh.flush()
                if stats_every and time.monotonic() >= next_stats:
                    print(stats.render(), file=log)
                    next_stats = time.monotonic() + stats_every

    try:
        if duration:
            async with asyncio.timeout(duration):
                await _run()
        else:
            await _run()
    except TimeoutError:
        pass
    finally:
        if fh:
            fh.close()
    return stats
