"""The recorder keeps going when the robot's broker goes away and comes back."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from typing import Any

import aiomqtt
import pytest

from yarbo_local import capture


class FakeMessage:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = aiomqtt.Topic(topic)
        self.payload = payload


class FakeClient:
    """First connection delivers one message then drops; the second delivers one more and ends."""

    connections = 0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> FakeClient:
        FakeClient.connections += 1
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def subscribe(self, topic: str) -> None:
        return None

    @property
    def messages(self) -> Any:
        n = FakeClient.connections

        async def gen() -> Any:
            yield FakeMessage("snowbot/SN1/device/heart_beat", json.dumps({"n": n}).encode())
            if n == 1:
                raise aiomqtt.MqttError("Disconnected during message iteration")

        return gen()


async def test_a_dropped_broker_is_reconnected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    FakeClient.connections = 0
    monkeypatch.setattr(capture.aiomqtt, "Client", FakeClient)
    monkeypatch.setattr(capture, "RECONNECT_MIN_S", 0.01)
    log = io.StringIO()
    out = tmp_path / "cap.jsonl"
    await asyncio.wait_for(capture.sniff("broker", out=out, echo=log, stats_every=0), 5)
    records = [json.loads(line) for line in out.read_text().splitlines()]
    assert [r["payload"] for r in records] == [{"n": 1}, {"n": 2}]
    assert FakeClient.connections == 2
    assert "reconnecting" in log.getvalue()
