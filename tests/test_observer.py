"""The app observer, fed the real exchanges captured on 2026-09-18."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from yarbo_local import codec
from yarbo_local.observer import AppObserver, Exchange
from yarbo_local.registry import Registry
from yarbo_local.session import MessageEvent

FIXTURES = Path(__file__).resolve().parents[1] / "protocol" / "fixtures" / "3.14.11"


def events(file: Path) -> list[MessageEvent]:
    out = []
    for line in file.read_text().splitlines():
        rec = json.loads(line)
        _, serial, side, leaf = rec["topic"].split("/", 3)
        out.append(
            MessageEvent(
                at=rec["t"],
                topic=rec["topic"],
                serial=serial,
                side=side,
                leaf=leaf,
                value=rec["payload"],
                encoding=rec["enc"],
                size=rec["bytes"],
                echo=False,
                raw=codec.encode(rec["payload"], compress=False),
            )
        )
    return out


def observe(name: str) -> AppObserver:
    observer = AppObserver(Registry.default())
    for event in events(FIXTURES / name):
        observer.on_message(event)
    return observer


def named(observer: AppObserver, name: str) -> Exchange:
    return next(e for e in observer.exchanges if e.name == name)


def test_a_pause_from_the_app_is_read_with_its_effect() -> None:
    observer = observe("plan-pause-resume-app.jsonl")
    pause = named(observer, "pause")
    assert (pause.sender, pause.standing, pause.payload) == ("app", "verified", {})
    assert pause.reply is None, "the robot does not answer pause"
    changed = {key: pair for effect in pause.effects for key, pair in effect.items()}
    assert changed["StateMSG.on_going_planning"] == [1, 0]
    assert changed["StateMSG.planning_paused"] == [0, 1]
    assert pause.effects[0]["after_ms"] < 1500
    resume = named(observer, "resume")
    assert any(e.get("StateMSG.on_going_planning") == [0, 1] for e in resume.effects)
    assert not pause.news
    assert not resume.news


def test_a_start_shows_what_we_know_and_what_is_news() -> None:
    observer = observe("plan-start-from-dock.jsonl")
    start = named(observer, "start_plan")
    assert start.payload == {"id": 1, "percent": 0}
    assert start.standing == "verified"
    assert start.new_keys == ()
    assert any("StateMSG.on_going_planning" in effect for effect in start.effects)

    check = named(observer, "check_map_connectivity")
    assert check.standing == "candidate", "registered from the 2026-09-26 app capture"
    assert check.reply is not None
    assert check.reply["state"] == 0
    assert check.reply["data"] == {"disconnected": [], "invalid": [], "normal": []}

    news = {e["name"] for e in observer.to_list(news_only=True)}
    assert "read_mower_area_params" in news
    assert "start_plan" not in news


def test_a_failed_start_has_no_reply_and_a_negative_code_as_its_effect() -> None:
    observer = observe("plan-start-fails-route-wp005.jsonl")
    start = named(observer, "start_plan")
    assert start.reply is None
    codes = [
        e["StateMSG.on_going_planning"][1]
        for e in start.effects
        if "StateMSG.on_going_planning" in e
    ]
    assert codes[-1] == -12


def _event(at: float, side: str, leaf: str, value: Any, *, echo: bool = False) -> MessageEvent:
    return MessageEvent(
        at=at,
        topic=f"snowbot/SN/{side}/{leaf}",
        serial="SN",
        side=side,
        leaf=leaf,
        value=value,
        encoding="json",
        size=1,
        echo=echo,
        raw=b"",
    )


def test_keepalives_fold_streams_are_counted_and_forbidden_is_named() -> None:
    observer = AppObserver(Registry.default())
    for i in range(6):
        observer.on_message(_event(10.0 * i, "app", "set_working_state", {"state": 1}))
    for i in range(40):
        observer.on_message(_event(100.0 + i / 50, "app", "cmd_vel", {"vel": 0.0, "rev": 0.0}))
    observer.on_message(_event(200.0, "app", "erase_map", {}))
    observer.on_message(_event(201.0, "app", "get_map", {}, echo=True))
    assert [(e.name, e.repeats) for e in observer.exchanges] == [
        ("set_working_state", 6),
        ("erase_map", 1),
        ("get_map", 1),
    ]
    assert observer.streams == {"cmd_vel": 40}
    assert named(observer, "erase_map").standing == "forbidden"
    assert named(observer, "get_map").sender == "us"


def test_a_payload_key_the_registry_does_not_list_is_news() -> None:
    observer = AppObserver(Registry.default())
    observer.on_message(_event(1.0, "app", "start_plan", {"id": 1, "percent": 0, "area": 4}))
    start = named(observer, "start_plan")
    assert start.new_keys == ("area",)
    assert start.news


def test_a_late_reply_and_a_late_change_are_not_credited() -> None:
    observer = AppObserver(Registry.default())
    observer.on_message(_event(1.0, "app", "get_map", {}))
    observer.on_message(_event(2.0, "device", "DeviceMSG", {"StateMSG": {"on_going_planning": 0}}))
    observer.on_message(_event(30.0, "device", "data_feedback", {"topic": "get_map", "state": 0}))
    observer.on_message(_event(31.0, "device", "DeviceMSG", {"StateMSG": {"on_going_planning": 1}}))
    get_map = named(observer, "get_map")
    assert get_map.reply is None
    assert get_map.effects == []
