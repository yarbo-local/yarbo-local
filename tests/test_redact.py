from yarbo_local.redact import Redactor, _nmea_checksum


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
