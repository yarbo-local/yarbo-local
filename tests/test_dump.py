import json
from pathlib import Path

from yarbo_local import dump


def test_summarise_capture(tmp_path: Path) -> None:
    path = tmp_path / "cap.jsonl"
    records = [
        {
            "t": 1.0,
            "topic": "snowbot/sn/device/DeviceMSG",
            "enc": "zlib",
            "payload": {"BatteryMSG": {"capacity": 90}, "StateMSG": {"working_state": 1}},
        },
        {
            "t": 2.0,
            "topic": "snowbot/sn/device/DeviceMSG",
            "enc": "zlib",
            "payload": {"BatteryMSG": {"capacity": 89}, "StateMSG": {"working_state": 1}},
        },
        {
            "t": 2.5,
            "topic": "snowbot/sn/device/heart_beat",
            "enc": "json",
            "payload": {"working_state": 1},
        },
        {
            "t": 3.0,
            "topic": "snowbot/sn/device/data_feedback",
            "enc": "zlib",
            "payload": {"topic": "read_all_plan", "state": 0, "msg": "ok", "data": []},
        },
        {"t": 3.5, "topic": "snowbot/sn/app/start_plan", "enc": "zlib", "payload": {"id": 1}},
    ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    summary = dump.summarise(path)
    assert summary.records == 5
    assert summary.device_msg_frames == 2
    assert summary.keys["BatteryMSG.capacity"].frames == 2
    assert summary.keys["BatteryMSG.capacity"].examples == [90, 89]
    assert summary.feedback_topics["read_all_plan"] == 1
    assert summary.app_commands["start_plan"] == 1
    text = summary.render(keys=True, app=True)
    assert "BatteryMSG.capacity" in text
    assert "start_plan" in text
