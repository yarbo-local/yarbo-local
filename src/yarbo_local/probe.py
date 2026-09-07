"""Probe: send one allowlisted command and show everything that comes back.

This is the Phase 0 instrument for answering protocol questions. It:

1. connects and subscribes to both sides of the robot's namespace,
2. watches for ``settle`` seconds to learn the outbound encoding from inbound
   traffic and to take a ``DeviceMSG`` baseline,
3. publishes the command,
4. collects for ``timeout`` seconds and prints, in order: the correlated
   ``data_feedback`` reply (matched on its ``topic`` field), any other feedback
   topics, and the ``DeviceMSG`` keys whose values changed since the baseline.

Only commands in :data:`PHASE0_COMMANDS` can be sent. Every one of them is a
read, except ``set_working_state`` which is the wake-up question itself and
``get_controller`` which takes the controller role from the phone app.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
from pathlib import Path
import secrets
import time
from typing import Any

import aiomqtt

from . import codec, topics

PHASE0_COMMANDS: dict[str, dict[str, Any]] = {
    "get_device_msg": {},
    "get_state_msg": {},
    "get_controller": {},
    "set_working_state": {"state": 1, "source": "smart_home"},
    "read_all_plan": {},
    "read_gps_ref": {},
    "get_map": {"width": 256, "height": 256},
    "get_plan_feedback": {},
    "read_all_nogozone": {},
    "read_all_clean_area": {},
    "read_all_pathway": {},
    "read_all_sidewalk": {},
    "read_global_params": {},
    "read_schedules": {},
    "get_all_map_backup": {},
    "read_recharge_point": {},
    "get_connect_wifi_name": {},
}


@dataclass
class ProbeResult:
    """Everything observed during one probe."""

    command: str
    payload: dict[str, Any]
    compressed: bool
    published_at: float = 0.0
    reply: dict[str, Any] | None = None
    reply_latency: float | None = None
    other_feedback: list[tuple[str, Any]] = field(default_factory=list)
    device_msg_changes: dict[str, tuple[Any, Any]] = field(default_factory=dict)
    app_side_seen: list[tuple[str, Any]] = field(default_factory=list)
    echoed: bool = False
    records: list[dict[str, Any]] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            (
                f"command={self.command} payload={json.dumps(self.payload)} "
                f"encoding={'zlib' if self.compressed else 'json'}"
            )
        ]
        if self.reply is None:
            lines.append("reply: NONE on data_feedback (no ack, or the command name is wrong)")
        else:
            state = self.reply.get("state")
            msg = self.reply.get("msg")
            lines.append(
                f"reply: state={state} msg={msg!r} latency={self.reply_latency:.3f}s"
                if self.reply_latency is not None
                else f"reply: state={state} msg={msg!r}"
            )
            data = self.reply.get("data")
            blob = codec.decode_blob(data)
            if blob is not None:
                data, label = blob
                text = json.dumps(data, separators=(",", ":"))
                lines.append(
                    f"data ({label}, {len(text)} chars decoded): "
                    f"{text[:1500]}{'...' if len(text) > 1500 else ''}"
                )
            else:
                text = json.dumps(data, separators=(",", ":"))
                lines.append(
                    f"data ({len(text)} chars): {text[:1500]}{'...' if len(text) > 1500 else ''}"
                )
        for leaf, value in self.other_feedback:
            text = json.dumps(value, separators=(",", ":"))
            lines.append(f"also on {leaf}: {text[:500]}{'...' if len(text) > 500 else ''}")
        lines.append(f"broker echoed our publish: {'yes' if self.echoed else 'no'}")
        for topic, value in self.app_side_seen:
            lines.append(f"other app-side traffic during probe: {topic} {json.dumps(value)[:200]}")
        if self.device_msg_changes:
            lines.append("DeviceMSG changes after the command:")
            for path, (before, after) in sorted(self.device_msg_changes.items()):
                lines.append(f"  {path}: {before!r} -> {after!r}")
        return "\n".join(lines)


async def probe(
    host: str,
    command: str,
    *,
    serial: str | None = None,
    port: int = 1883,
    payload: dict[str, Any] | None = None,
    encoding: str = "auto",
    settle: float = 6.0,
    timeout: float = 8.0,
    out: Path | None = None,
) -> ProbeResult:
    """Send one allowlisted command and collect the response window."""
    if command not in PHASE0_COMMANDS:
        allowed = ", ".join(sorted(PHASE0_COMMANDS))
        raise ValueError(f"{command!r} is not in the Phase 0 allowlist: {allowed}")
    body = PHASE0_COMMANDS[command] if payload is None else payload

    seen_encodings: dict[str, codec.Encoding] = {}
    baseline: dict[str, Any] = {}
    latest: dict[str, Any] = {}
    firmware: str | None = None
    records: list[dict[str, Any]] = []
    result = ProbeResult(command=command, payload=body, compressed=False)
    identifier = f"yarbo-local-probe-{secrets.token_hex(3)}"

    async with aiomqtt.Client(host, port=port, identifier=identifier) as client:
        await client.subscribe(topics.all_for(serial))
        learned_serial = serial

        async def consume(until: float, phase: str) -> None:
            nonlocal learned_serial, firmware
            while True:
                remaining = until - time.monotonic()
                if remaining <= 0:
                    return
                try:
                    async with asyncio.timeout(remaining):
                        message = await anext(client.messages)
                except (TimeoutError, StopAsyncIteration):
                    return
                topic = message.topic.value
                raw = message.payload if isinstance(message.payload, bytes) else b""
                value, enc = codec.decode(raw)
                now = time.time()
                records.append(
                    {
                        "t": round(now, 3),
                        "phase": phase,
                        "topic": topic,
                        "enc": enc,
                        "payload": value,
                    }
                )
                parsed = topics.parse(topic)
                if not parsed:
                    continue
                if learned_serial is None:
                    learned_serial = parsed.serial
                if parsed.serial != learned_serial:
                    continue
                if parsed.side == "app":
                    if phase == "collect":
                        if parsed.leaf == command and value == body:
                            result.echoed = True
                        else:
                            result.app_side_seen.append((parsed.leaf, value))
                    continue
                seen_encodings[parsed.leaf] = enc
                if parsed.leaf == "DeviceMSG" and isinstance(value, dict):
                    flat = codec.flatten(value)
                    firmware = str(value.get("version") or firmware or "") or None
                    target = baseline if phase == "settle" else latest
                    target.update(flat)
                elif (
                    phase == "collect"
                    and parsed.leaf == "data_feedback"
                    and isinstance(value, dict)
                ):
                    if value.get("topic") == command and result.reply is None:
                        result.reply = value
                        result.reply_latency = now - result.published_at
                    else:
                        result.other_feedback.append(
                            (f"data_feedback[{value.get('topic')}]", value)
                        )
                elif phase == "collect" and parsed.leaf not in ("heart_beat",):
                    result.other_feedback.append((parsed.leaf, value))

        await consume(time.monotonic() + settle, "settle")
        if learned_serial is None:
            raise RuntimeError(
                "no robot traffic seen; pass --serial or wait for a heartbeat before probing"
            )

        if encoding == "zlib":
            compress = True
        elif encoding == "json":
            compress = False
        else:
            decided = codec.firmware_wants_zlib(firmware)
            if decided is None:
                decided = codec.observed_encoding(seen_encodings)
            compress = True if decided is None else decided
        result.compressed = compress

        result.published_at = time.time()
        await client.publish(
            topics.app(learned_serial, command), codec.encode(body, compress=compress)
        )
        await consume(time.monotonic() + timeout, "collect")

    for path, after in latest.items():
        before = baseline.get(path)
        if before != after:
            result.device_msg_changes[path] = (before, after)
    result.records = records
    if out:
        with out.open("a", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
    return result
