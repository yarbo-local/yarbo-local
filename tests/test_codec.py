import base64
import json
import zlib

from yarbo_local import codec


def test_zlib_roundtrip() -> None:
    value = {"BatteryMSG": {"capacity": 91}, "version": "3.9.2"}
    raw = codec.encode(value, compress=True)
    assert codec.looks_zlib(raw)
    decoded, enc = codec.decode(raw)
    assert decoded == value
    assert enc == "zlib"


def test_plain_json_roundtrip() -> None:
    value = {"working_state": 0}
    raw = codec.encode(value, compress=False)
    assert not codec.looks_zlib(raw)
    decoded, enc = codec.decode(raw)
    assert decoded == value
    assert enc == "json"


def test_raw_fallback_keeps_bytes() -> None:
    raw = b"\x78\x9c\x00\x01garbage"
    decoded, enc = codec.decode(raw)
    assert enc == "raw"
    assert decoded == {"_raw_hex": raw.hex()}


def test_compact_encoding() -> None:
    raw = codec.encode({"a": 1, "b": [1, 2]}, compress=False)
    assert raw == b'{"a":1,"b":[1,2]}'
    assert json.loads(zlib.decompress(codec.encode({"a": 1}, compress=True))) == {"a": 1}


def test_observed_encoding_ignores_heartbeat() -> None:
    assert codec.observed_encoding({"heart_beat": "json"}) is None
    assert codec.observed_encoding({"heart_beat": "json", "DeviceMSG": "zlib"}) is True
    assert codec.observed_encoding({"DeviceMSG": "json"}) is False
    assert codec.observed_encoding({"DeviceMSG": "raw"}) is None


def test_firmware_rule() -> None:
    assert codec.firmware_wants_zlib("3.9.0") is True
    assert codec.firmware_wants_zlib("3.10.1") is True
    assert codec.firmware_wants_zlib("3.8.9") is False
    assert codec.firmware_wants_zlib("2.3.9") is False
    assert codec.firmware_wants_zlib(None) is None
    assert codec.firmware_wants_zlib("") is None
    assert codec.firmware_wants_zlib("v4.0") is True


def test_flatten() -> None:
    nested = {"A": {"b": 1, "c": {"d": [1, 2]}}, "x": None, "system_info": {"cpu.Temperature": 40}}
    assert codec.flatten(nested) == {
        "A.b": 1,
        "A.c.d": [1, 2],
        "x": None,
        "system_info.cpu.Temperature": 40,
    }


def test_decode_blob_b64zlib_and_jsonstr() -> None:
    inner = {"areas": [], "nogozones": [{"id": 1}]}
    blob = base64.b64encode(zlib.compress(json.dumps(inner).encode())).decode()
    assert codec.decode_blob(blob) == (inner, "b64zlib")
    assert codec.decode_blob(json.dumps(inner)) == (inner, "jsonstr")
    assert codec.decode_blob("") is None
    assert codec.decode_blob("not a blob") is None
    assert codec.decode_blob({"already": "decoded"}) is None
