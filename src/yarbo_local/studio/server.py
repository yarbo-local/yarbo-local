"""aiohttp server behind ``yarbo-local studio``.

It observes one robot through the same session the integration uses, keeps a
ring of raw messages for fixture export, streams decoded messages and
``DeviceMSG`` diffs over a websocket, compares what the robot sends with the
field map, and writes promoted semantics into a checkout's ``protocol/``.
The registry's rules apply to the command console exactly as they do to the
integration: verified commands only, forbidden names never.
"""

from __future__ import annotations

import asyncio
from collections import deque
import contextlib
from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
import sys
import time
from typing import Any
import webbrowser

from aiohttp import WSMsgType, web

from .. import codec, fieldmap
from ..capture import build_record
from ..client import YarboRobot
from ..exceptions import YarboError
from ..redact import redactor_for_salt
from ..registry import data_path
from ..session import MessageEvent

_LOGGER = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
RING_SIZE = 20000
CLIENT_QUEUE = 1000


@dataclass
class TopicStat:
    count: int = 0
    last_at: float = 0.0
    encoding: str = ""
    size: int = 0
    recent: deque[float] = field(default_factory=lambda: deque(maxlen=50))

    @property
    def rate(self) -> float:
        if len(self.recent) < 2:
            return 0.0
        span = self.recent[-1] - self.recent[0]
        return (len(self.recent) - 1) / span if span > 0 else 0.0


def default_protocol_dir() -> Path | None:
    """The ``protocol/`` directory of a source checkout, if we are running from one."""
    candidate = Path(__file__).resolve().parents[3] / "protocol"
    return candidate if (candidate / "commands.yaml").exists() else None


class Studio:
    """State behind the HTTP and websocket API."""

    def __init__(self, robot: YarboRobot, *, protocol_dir: Path | None = None) -> None:
        self.robot = robot
        self.protocol_dir = protocol_dir
        self.started = time.time()
        self.ring: deque[tuple[float, str, bytes]] = deque(maxlen=RING_SIZE)
        self.topics: dict[str, TopicStat] = {}
        self.seen_paths: dict[str, dict[str, Any]] = {}
        self.clients: dict[web.WebSocketResponse, asyncio.Queue[str]] = {}
        self._last_frame: dict[str, Any] = {}
        self._unsubscribe = [
            robot.session.add_message_listener(self._on_message),
            robot.session.add_connection_listener(self._on_connection),
        ]

    async def close(self) -> None:
        for unsub in self._unsubscribe:
            unsub()
        for ws in list(self.clients):
            await ws.close()

    # -- observation

    def _on_message(self, ev: MessageEvent) -> None:
        self.ring.append((ev.at, ev.topic, ev.raw))
        stat = self.topics.setdefault(ev.topic, TopicStat())
        stat.count += 1
        stat.last_at = ev.at
        stat.encoding = ev.encoding
        stat.size = ev.size
        stat.recent.append(ev.at)
        self._broadcast(
            {
                "type": "message",
                "t": ev.at,
                "topic": ev.topic,
                "side": ev.side,
                "leaf": ev.leaf,
                "enc": ev.encoding,
                "size": ev.size,
                "echo": ev.echo,
                "payload": ev.value,
            }
        )
        if ev.side != "device" or not isinstance(ev.value, dict):
            return
        if ev.leaf == "DeviceMSG":
            flat = codec.flatten(ev.value)
            self._note_paths(flat, ev.at)
            diff = self._diff(flat)
            if diff:
                self._broadcast({"type": "diff", "t": ev.at, **diff})
        elif (
            ev.leaf == "data_feedback"
            and ev.value.get("topic") == "get_device_msg"
            and isinstance(ev.value.get("data"), dict)
        ):
            self._note_paths(codec.flatten(ev.value["data"]), ev.at)

    def _on_connection(self, connected: bool) -> None:
        self._broadcast({"type": "connection", "t": time.time(), "connected": connected})

    def _note_paths(self, flat: dict[str, Any], at: float) -> None:
        for path, value in flat.items():
            entry = self.seen_paths.get(path)
            if entry is None:
                self.seen_paths[path] = {
                    "path": path,
                    "example": value,
                    "type": type(value).__name__,
                    "count": 1,
                    "first_at": at,
                }
            else:
                entry["count"] += 1

    def _diff(self, flat: dict[str, Any]) -> dict[str, Any] | None:
        previous = self._last_frame
        self._last_frame = flat
        changed = {
            k: [previous[k], v] for k, v in flat.items() if k in previous and previous[k] != v
        }
        added = [k for k in flat if k not in previous]
        removed = [k for k in previous if k not in flat]
        if not (changed or added or removed):
            return None
        return {"changed": changed, "added": added, "removed": removed}

    def _broadcast(self, obj: dict[str, Any]) -> None:
        text = json.dumps(obj, default=str)
        for queue in list(self.clients.values()):
            # A slow viewer misses messages rather than stalling the session.
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(text)

    # -- views

    def summary(self) -> dict[str, Any]:
        state = self.robot.state
        now = time.time()
        return {
            "serial": self.robot.serial,
            "firmware": state.firmware,
            "connected": self.robot.session.connected,
            "awake": state.awake,
            "activity": state.activity.value,
            "battery": state.battery,
            "head": state.head_name,
            "uptime": round(now - self.started, 1),
            "ring": len(self.ring),
            "protocol_dir": str(self.protocol_dir) if self.protocol_dir else None,
            "editable": self.protocol_dir is not None,
            "topics": [
                {
                    "topic": topic,
                    "count": stat.count,
                    "rate": round(stat.rate, 2),
                    "age": round(now - stat.last_at, 1),
                    "enc": stat.encoding,
                    "size": stat.size,
                }
                for topic, stat in sorted(self.topics.items())
            ],
        }

    def _fields(self) -> dict[str, dict[str, Any]]:
        path = (
            self.protocol_dir / fieldmap.FIELDS_FILE
            if self.protocol_dir
            else data_path(fieldmap.FIELDS_FILE)
        )
        return fieldmap.load_fields(path)

    def knowledge(self) -> dict[str, Any]:
        registry = self.robot.session.registry
        return {
            "fields": self._fields(),
            "commands": [
                {
                    "name": cmd.name,
                    "status": cmd.status,
                    "ack": cmd.ack,
                    "risk": cmd.risk,
                    "controller": cmd.controller,
                    "awake": cmd.awake,
                    "heads": list(cmd.heads),
                    "evidence": cmd.evidence,
                    "payload": cmd.payload,
                    "notes": cmd.notes,
                }
                for cmd in registry
            ],
            "forbidden": registry.forbidden,
        }

    def diff(self) -> dict[str, Any]:
        fields = self._fields()
        unknown = [entry for path, entry in sorted(self.seen_paths.items()) if path not in fields]
        known_unseen = [path for path in fields if path not in self.seen_paths]
        return {
            "unknown": unknown,
            "known_unseen": known_unseen,
            "seen": len(self.seen_paths),
            "known": len(fields),
        }

    # -- writes

    def _require_checkout(self) -> Path:
        if self.protocol_dir is None:
            raise web.HTTPBadRequest(
                text=json.dumps({"error": "no protocol directory; start with --protocol-dir"}),
                content_type="application/json",
            )
        return self.protocol_dir

    def promote(self, body: dict[str, Any]) -> dict[str, Any]:
        protocol_dir = self._require_checkout()
        path = str(body.get("path") or "")
        semantics = {k: v for k, v in body.items() if k != "path"}
        return fieldmap.promote(protocol_dir, path, semantics)

    def list_fixtures(self) -> list[dict[str, Any]]:
        protocol_dir = self._require_checkout()
        out = []
        for file in sorted((protocol_dir / "fixtures").glob("*/*.jsonl")):
            out.append(
                {
                    "firmware": file.parent.name,
                    "name": file.stem,
                    "path": str(file),
                    "bytes": file.stat().st_size,
                    "records": sum(1 for line in file.open(encoding="utf-8") if line.strip()),
                }
            )
        return out

    def save_fixture(self, name: str, seconds: float) -> dict[str, Any]:
        protocol_dir = self._require_checkout()
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in name.strip()).strip("-")
        if not safe:
            raise ValueError("fixture name is required")
        firmware = self.robot.state.firmware or "unknown"
        target = protocol_dir / "fixtures" / firmware / f"{safe}.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - seconds
        redactor = redactor_for_salt(None)
        real_serial = self.robot.serial or ""
        lines: list[str] = []
        for at, topic, raw in self.ring:
            if at < cutoff:
                continue
            rec = build_record(topic, raw, redactor, at=at)
            lines.append(json.dumps(rec, separators=(",", ":")))
        text = "\n".join(lines) + ("\n" if lines else "")
        target.write_text(text, encoding="utf-8")
        return {
            "path": str(target),
            "records": len(lines),
            "seconds": seconds,
            "leaks": {"serial": text.count(real_serial) if real_serial else 0},
        }

    async def run_command(self, name: str, payload: Any) -> dict[str, Any]:
        session = self.robot.session
        cmd = session.registry.check(name, allow_candidates=False, confirmed=False)
        started = time.monotonic()
        if cmd.expects_reply:
            fb = await session.request(name, payload, timeout=15.0)
            return {
                "name": name,
                "ok": fb.ok,
                "state": fb.state,
                "msg": fb.msg,
                "data": fb.payload,
                "latency_ms": round((time.monotonic() - started) * 1000, 1),
            }
        await session.send(name, payload)
        return {"name": name, "ok": True, "state": None, "msg": "sent (no reply expected)"}

    # -- http

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_static("/static", STATIC)
        app.router.add_get("/ws", self._ws)
        app.router.add_get("/api/summary", self._json(lambda _r: self.summary()))
        app.router.add_get("/api/knowledge", self._json(lambda _r: self.knowledge()))
        app.router.add_get("/api/diff", self._json(lambda _r: self.diff()))
        app.router.add_get("/api/fixtures", self._json(lambda _r: self.list_fixtures()))
        app.router.add_post("/api/fixtures", self._fixtures_post)
        app.router.add_post("/api/fields", self._fields_post)
        app.router.add_post("/api/command", self._command_post)
        app.router.add_post("/api/wake", self._wake_post)
        return app

    @staticmethod
    def _json(fn: Any) -> Any:
        async def handler(request: web.Request) -> web.Response:
            return web.json_response(fn(request), dumps=lambda o: json.dumps(o, default=str))

        return handler

    @staticmethod
    def _error(status: int, message: str) -> web.Response:
        return web.json_response({"error": message}, status=status)

    async def _index(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC / "index.html")

    async def _fixtures_post(self, request: web.Request) -> web.Response:
        body = await request.json()
        try:
            result = self.save_fixture(str(body.get("name", "")), float(body.get("seconds", 60)))
        except (ValueError, OSError) as err:
            return self._error(400, str(err))
        return web.json_response(result)

    async def _fields_post(self, request: web.Request) -> web.Response:
        body = await request.json()
        try:
            entry = self.promote(body)
        except (ValueError, OSError) as err:
            return self._error(400, str(err))
        return web.json_response(entry, dumps=lambda o: json.dumps(o, default=str))

    async def _command_post(self, request: web.Request) -> web.Response:
        body = await request.json()
        try:
            result = await self.run_command(str(body.get("name", "")), body.get("payload"))
        except YarboError as err:
            return self._error(400, str(err))
        return web.json_response(result, dumps=lambda o: json.dumps(o, default=str))

    async def _wake_post(self, request: web.Request) -> web.Response:
        try:
            awake = await self.robot.wake()
        except YarboError as err:
            return self._error(400, str(err))
        return web.json_response({"awake": awake})

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=CLIENT_QUEUE)
        self.clients[ws] = queue
        await ws.send_str(json.dumps({"type": "hello", **self.summary()}, default=str))

        async def writer() -> None:
            while True:
                await ws.send_str(await queue.get())

        task = asyncio.create_task(writer())
        try:
            async for msg in ws:
                if msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        finally:
            task.cancel()
            self.clients.pop(ws, None)
        return ws


async def serve(
    robot_host: str,
    *,
    robot_port: int = 1883,
    serial: str | None = None,
    ui_host: str = "127.0.0.1",
    ui_port: int = 8765,
    protocol_dir: Path | None = None,
    open_browser: bool = True,
) -> None:
    """Connect to the robot and serve the Studio until interrupted."""
    robot = YarboRobot.for_host(robot_host, port=robot_port, serial=serial)
    studio = Studio(robot, protocol_dir=protocol_dir or default_protocol_dir())
    await robot.start()
    runner = web.AppRunner(studio.build_app())
    await runner.setup()
    site = web.TCPSite(runner, ui_host, ui_port)
    await site.start()
    url = f"http://{ui_host}:{ui_port}"
    print(
        f"Studio at {url} for robot {robot.serial} via {robot_host}:{robot_port}; "
        f"protocol dir: {studio.protocol_dir or 'none (read-only)'}",
        file=sys.stderr,
    )
    if open_browser:
        webbrowser.open(url)
    try:
        await asyncio.Event().wait()
    finally:
        await studio.close()
        await runner.cleanup()
        await robot.close()
