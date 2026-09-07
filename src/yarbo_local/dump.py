"""Summarise a JSONL capture: topics, rates, and the DeviceMSG key inventory.

The key inventory is the raw material for ``protocol/fields.yaml``: every
flattened path seen, how many frames carried it, and an example value.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

from . import codec, topics


@dataclass
class KeyInfo:
    frames: int = 0
    examples: list[Any] = field(default_factory=list)
    types: Counter[str] = field(default_factory=Counter)

    def note(self, value: Any) -> None:
        self.frames += 1
        self.types[type(value).__name__] += 1
        if len(self.examples) < 3 and value not in self.examples:
            self.examples.append(value)


@dataclass
class Summary:
    records: int = 0
    topics: Counter[str] = field(default_factory=Counter)
    encodings: Counter[str] = field(default_factory=Counter)
    first_t: float | None = None
    last_t: float | None = None
    device_msg_frames: int = 0
    keys: dict[str, KeyInfo] = field(default_factory=lambda: defaultdict(KeyInfo))
    feedback_topics: Counter[str] = field(default_factory=Counter)
    app_commands: Counter[str] = field(default_factory=Counter)
    app_samples: dict[str, Any] = field(default_factory=dict)

    def render(self, *, keys: bool, app: bool) -> str:
        span = (self.last_t - self.first_t) if self.first_t and self.last_t else 0.0
        lines = [
            (
                f"records={self.records} span={span:.1f}s encodings={dict(self.encodings)} "
                f"DeviceMSG_frames={self.device_msg_frames}"
            ),
            "",
            f"{'topic':60} {'count':>7} {'rate/s':>8}",
        ]
        for topic, count in self.topics.most_common():
            rate = count / span if span > 0 else 0.0
            lines.append(f"{topic:60} {count:7d} {rate:8.2f}")
        if self.feedback_topics:
            lines += ["", "data_feedback replies by command:"]
            for cmd, count in self.feedback_topics.most_common():
                lines.append(f"  {cmd:40} {count}")
        if app and self.app_commands:
            lines += ["", "app-side commands observed (what the phone sent):"]
            for cmd, count in self.app_commands.most_common():
                sample = json.dumps(self.app_samples.get(cmd), separators=(",", ":"))
                lines.append(f"  {cmd:40} {count:5d}  e.g. {sample[:120]}")
        if keys and self.keys:
            lines += ["", f"DeviceMSG key inventory ({len(self.keys)} paths):"]
            lines.append(f"  {'path':56} {'frames':>7} {'types':18} example")
            for path in sorted(self.keys):
                info = self.keys[path]
                types = ",".join(sorted(info.types))
                example = (
                    json.dumps(info.examples[0], separators=(",", ":")) if info.examples else ""
                )
                lines.append(f"  {path:56} {info.frames:7d} {types:18} {example[:60]}")
        return "\n".join(lines)


def summarise(path: Path) -> Summary:
    """Read a JSONL capture (from ``sniff`` or ``probe``) and summarise it."""
    summary = Summary()
    with path.open(encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            rec = json.loads(line)
            summary.records += 1
            t = rec.get("t")
            if isinstance(t, int | float):
                summary.first_t = t if summary.first_t is None else min(summary.first_t, t)
                summary.last_t = t if summary.last_t is None else max(summary.last_t, t)
            topic = str(rec.get("topic", ""))
            summary.topics[topic] += 1
            summary.encodings[str(rec.get("enc", "?"))] += 1
            parsed = topics.parse(topic)
            payload = rec.get("payload")
            if not parsed:
                continue
            if parsed.side == "app":
                summary.app_commands[parsed.leaf] += 1
                summary.app_samples.setdefault(parsed.leaf, payload)
                continue
            if parsed.leaf == "DeviceMSG" and isinstance(payload, dict):
                summary.device_msg_frames += 1
                for key, value in codec.flatten(payload).items():
                    summary.keys[key].note(value)
            elif parsed.leaf == "data_feedback" and isinstance(payload, dict):
                summary.feedback_topics[str(payload.get("topic"))] += 1
    return summary
