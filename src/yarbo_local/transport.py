"""Transports: the real aiomqtt one and an in-memory fake for tests.

A transport moves bytes. It does not decode, correlate or reconnect. The one
rule every transport must honour: ``messages()`` never returns normally while
the caller expects a connection. It raises :class:`ConnectionLostError` so the
session can back off and reconnect. A stream that returns cleanly on a dropped
connection is exactly the bug that livelocked the community integration.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
import contextlib
from dataclasses import dataclass, field
import secrets
import ssl
from typing import Protocol

import aiomqtt

from .exceptions import ConnectionLostError


@dataclass(frozen=True, slots=True)
class Message:
    topic: str
    payload: bytes
    retain: bool = False


class Transport(Protocol):
    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def subscribe(self, topic_filter: str) -> None: ...
    async def publish(self, topic: str, payload: bytes, *, retain: bool = False) -> None: ...
    def messages(self) -> AsyncIterator[Message]: ...
    @property
    def connected(self) -> bool: ...


class MqttTransport:
    """aiomqtt-backed transport for one broker."""

    def __init__(
        self,
        host: str,
        port: int = 1883,
        *,
        tls: bool = False,
        identifier: str | None = None,
        keepalive: int = 30,
        will: tuple[str, bytes] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.tls = tls
        self.identifier = identifier or f"yarbo-local-{secrets.token_hex(3)}"
        self.keepalive = keepalive
        self._will = will
        self._client: aiomqtt.Client | None = None
        self._filters: list[str] = []
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        ctx: ssl.SSLContext | None = None
        if self.tls:
            # The robot presents EMQX's stock self-signed sample certificate.
            # This gives encryption, not authentication; see the findings.
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        will = aiomqtt.Will(self._will[0], self._will[1], retain=True) if self._will else None
        client = aiomqtt.Client(
            self.host,
            port=self.port,
            identifier=self.identifier,
            keepalive=self.keepalive,
            tls_context=ctx,
            will=will,
        )
        try:
            await client.__aenter__()
            for topic_filter in self._filters:
                await client.subscribe(topic_filter)
        except (aiomqtt.MqttError, OSError) as err:
            raise ConnectionLostError(f"connect to {self.host}:{self.port} failed: {err}") from err
        self._client = client
        self._connected = True

    async def close(self) -> None:
        client, self._client = self._client, None
        self._connected = False
        if client is not None:
            with contextlib.suppress(aiomqtt.MqttError, OSError):
                await client.__aexit__(None, None, None)

    async def subscribe(self, topic_filter: str) -> None:
        if topic_filter not in self._filters:
            self._filters.append(topic_filter)
        if self._client is not None:
            try:
                await self._client.subscribe(topic_filter)
            except (aiomqtt.MqttError, OSError) as err:
                self._connected = False
                raise ConnectionLostError(str(err)) from err

    async def publish(self, topic: str, payload: bytes, *, retain: bool = False) -> None:
        if self._client is None:
            raise ConnectionLostError("not connected")
        try:
            await self._client.publish(topic, payload, retain=retain)
        except (aiomqtt.MqttError, OSError) as err:
            self._connected = False
            raise ConnectionLostError(str(err)) from err

    async def messages(self) -> AsyncIterator[Message]:
        if self._client is None:
            raise ConnectionLostError("not connected")
        try:
            async for msg in self._client.messages:
                raw = msg.payload if isinstance(msg.payload, bytes) else b""
                yield Message(msg.topic.value, raw, bool(msg.retain))
        except (aiomqtt.MqttError, OSError) as err:
            self._connected = False
            raise ConnectionLostError(str(err)) from err
        self._connected = False
        raise ConnectionLostError("message stream ended")


PublishHook = Callable[[str, bytes], Awaitable[None] | None]


@dataclass
class FakeTransport:
    """In-memory transport. Tests deliver inbound messages and inspect publishes."""

    on_publish: PublishHook | None = None
    inbox: asyncio.Queue[Message | Exception] = field(default_factory=asyncio.Queue)
    published: list[Message] = field(default_factory=list)
    filters: list[str] = field(default_factory=list)
    _connected: bool = False
    connect_attempts: int = 0
    fail_next_connects: int = 0

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self.connect_attempts += 1
        if self.fail_next_connects > 0:
            self.fail_next_connects -= 1
            raise ConnectionLostError("simulated connect failure")
        self._connected = True

    async def close(self) -> None:
        self._connected = False

    async def subscribe(self, topic_filter: str) -> None:
        if topic_filter not in self.filters:
            self.filters.append(topic_filter)

    async def publish(self, topic: str, payload: bytes, *, retain: bool = False) -> None:
        if not self._connected:
            raise ConnectionLostError("not connected")
        self.published.append(Message(topic, payload, retain))
        if self.on_publish is not None:
            result = self.on_publish(topic, payload)
            if result is not None:
                await result

    async def messages(self) -> AsyncIterator[Message]:
        if not self._connected:
            raise ConnectionLostError("not connected")
        while True:
            item = await self.inbox.get()
            if isinstance(item, Exception):
                self._connected = False
                raise item
            yield item

    # -- test helpers

    def deliver(self, topic: str, payload: bytes, *, retain: bool = False) -> None:
        self.inbox.put_nowait(Message(topic, payload, retain))

    def drop(self) -> None:
        """Simulate the broker going away."""
        self.inbox.put_nowait(ConnectionLostError("simulated disconnect"))
