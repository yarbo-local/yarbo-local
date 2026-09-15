import base64
import json
from pathlib import Path
import zlib

from yarbo_local import codec
from yarbo_local.redact import Redactor, _nmea_checksum, redact_file


def test_serial_token_is_stable_within_capture() -> None:
    r = Redactor(salt="fixed")
    a = r.serial_token("2440011234567890")
    b = r.serial_token("2440011234567890")
    assert a == b
    assert a.startswith("SN-")
    assert "2440011234567890" not in a


def test_topic_and_text_redaction() -> None:
    r = Redactor(salt="fixed")
    r.register_serial("2440011234567890")
    assert r.redact_topic("snowbot/2440011234567890/device/DeviceMSG").startswith("snowbot/SN-")
    text = "sn 2440011234567890 mac c8:fe:0f:01:02:03 ip 192.168.40.23"
    out = r.redact_text(text)
    assert "2440011234567890" not in out
    assert "c8:fe:0f" not in out
    assert "192.168.40.23" not in out


def test_gga_shift_preserves_structure_and_checksum() -> None:
    r = Redactor(salt="fixed", lat_offset=1.5, lon_offset=-2.25)
    body = "GNGGA,123519.00,4807.0380,N,01131.0000,E,4,12,0.9,545.4,M,46.9,M,,"
    sentence = f"${body}*{_nmea_checksum(body)}"
    out = r.redact_gga(sentence)
    assert out.startswith("$GNGGA,123519.00,")
    fields = out[1 : out.rfind("*")].split(",")
    assert fields[6] == "4"  # fix quality untouched
    assert fields[2] != "4807.0380"
    assert fields[4] != "01131.0000"
    assert out[out.rfind("*") + 1 :] == _nmea_checksum(out[1 : out.rfind("*")])


def test_value_walker_shifts_coordinates_and_drops_secrets() -> None:
    r = Redactor(salt="fixed", lat_offset=1.0, lon_offset=1.0)
    payload = {
        "ref": {"latitude": 48.1, "longitude": 11.5},
        "wifi": {"ssid": "home", "password": "hunter2", "signal": -50},
        "zero": {"latitude": 0, "longitude": 0},
        "list": [{"lat": 10.0, "lon": 20.0}],
    }
    out = r.redact_value(payload)
    assert out["ref"] == {"latitude": 49.1, "longitude": 12.5}
    assert out["wifi"] == {"ssid": "REDACTED", "password": "REDACTED", "signal": -50}
    assert out["zero"] == {"latitude": 0, "longitude": 0}
    assert out["list"] == [{"lat": 11.0, "lon": 21.0}]


def test_identifying_keys_from_real_snapshot_are_dropped() -> None:
    r = Redactor(salt="fixed")
    out = r.redact_value(
        {
            "lat_lon_hight": "42.3 -71.5 88.1",
            "net_module_status": {"lte_iccid": "8943010352", "lte_rssi": 0},
            "HeadSerialMsg": {"head_sn": "ABC"},
            "halow_status": {"ssid": "DC_123", "strength": -43},
        }
    )
    assert out["lat_lon_hight"] == "REDACTED"
    assert out["net_module_status"] == {"lte_iccid": "REDACTED", "lte_rssi": 0}
    assert out["HeadSerialMsg"] == {"head_sn": "REDACTED"}
    assert out["halow_status"] == {"ssid": "REDACTED", "strength": -43}


def test_redact_file_roundtrip(tmp_path: Path) -> None:
    src = tmp_path / "in.jsonl"
    src.write_text(
        json.dumps(
            {
                "topic": "snowbot/2440011234567890/device/data_feedback",
                "payload": {
                    "data": {"ref": {"latitude": 48.1, "longitude": 11.5}, "sn": "2440011234567890"}
                },
            }
        )
        + "\n"
    )
    dst = tmp_path / "out.jsonl"
    assert redact_file(src, dst, salt="s") == 1
    rec = json.loads(dst.read_text())
    assert "2440011234567890" not in dst.read_text()
    assert rec["topic"].startswith("snowbot/SN-")
    assert rec["payload"]["data"]["ref"]["latitude"] != 48.1


def test_gga_without_checksum_and_camelcase_and_nested_json_string() -> None:
    r = Redactor(salt="fixed", lat_offset=1.0, lon_offset=1.0)
    out = r.redact_value(
        {
            "modebase_info": {
                "BaseName": "DC_123",
                "ModeBase": json.dumps({"latitude": 48.1, "longitude": 11.5, "h": 1}),
            },
            "rtk_base_data": {
                "base": {"gngga": "$GNGGA,193452.00,4807.0380,N,01131.0000,E,7,30,0.5,8\\r\\n"}
            },
        }
    )
    assert out["modebase_info"]["BaseName"] == "REDACTED"
    inner = json.loads(out["modebase_info"]["ModeBase"])
    assert inner == {"latitude": 49.1, "longitude": 12.5, "h": 1}
    gga = out["rtk_base_data"]["base"]["gngga"]
    assert "4807.0380" not in gga
    assert "01131.0000" not in gga
    assert "*" not in gga


def test_text_coordinates_are_shifted_and_placeholders_kept() -> None:
    r = Redactor(salt="t", lat_offset=1.25, lon_offset=-2.5)
    out = r.redact_value(
        {
            "modebase_info": {
                "ModeBase": json.dumps(
                    {"latitude": "42.1234567", "longitude": "-71.7654321", "altitude": "88.1"}
                )
            },
            "placeholder": {"latitude": "999.999", "longitude": "999.999"},
            "zero": {"lat": "0", "lon": "0.0"},
            "junk": {"latitude": "north-ish"},
        }
    )
    inner = json.loads(out["modebase_info"]["ModeBase"])
    assert inner["latitude"] == "43.3734567"
    assert inner["longitude"] == "-74.2654321"
    assert inner["altitude"] == "88.1"
    assert out["placeholder"] == {"latitude": "999.999", "longitude": "999.999"}
    assert out["zero"] == {"lat": "0", "lon": "0.0"}
    assert out["junk"]["latitude"] == "REDACTED"


def test_base64_zlib_map_blob_is_redacted_inside() -> None:
    site = {
        "areas": [
            {
                "id": 1,
                "name": "Area 1",
                "ref": {"latitude": 42.123456, "longitude": -71.654321},
                "range": [{"x": 1.0, "y": 2.0, "phi": 0.0}],
            }
        ]
    }
    blob = base64.b64encode(zlib.compress(json.dumps(site).encode())).decode()
    r = Redactor(salt="t", lat_offset=1.0, lon_offset=2.0)
    out = r.redact_value({"topic": "get_map", "data": blob})
    decoded = codec.decode_blob(out["data"])
    assert decoded is not None
    inner, kind = decoded
    assert kind == "b64zlib"
    area = inner["areas"][0]
    assert area["ref"]["latitude"] == 43.123456
    assert round(area["ref"]["longitude"], 6) == -69.654321
    assert area["range"] == [{"x": 1.0, "y": 2.0, "phi": 0.0}]
    assert "42.1234" not in json.dumps(out)


def test_latitude_shift_keeps_differences_near_the_wrap() -> None:
    r = Redactor(salt="t", lat_offset=59.9, lon_offset=0.0)
    a, b = r.shift_lat(28.00), r.shift_lat(28.01)  # 87.9 and 87.91 would have clamped to 89
    assert round(b - a, 6) == 0.01
    assert -80.0 <= a <= 80.0


def test_truncated_gga_is_scrubbed() -> None:
    r = Redactor(salt="t", lat_offset=1.0, lon_offset=2.0)
    out = r.redact_value({"base": {"gngga": "$GNGGA,132203.00,4212.34\\n"}})
    assert out["base"]["gngga"] == "$GNGGA,132203.00,TRUNCATED"
    assert "4212" not in json.dumps(out)
