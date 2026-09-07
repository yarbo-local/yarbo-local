from yarbo_local import topics


def test_parse_device_topic() -> None:
    t = topics.parse("snowbot/2440011234567890/device/DeviceMSG")
    assert t is not None
    assert t.serial == "2440011234567890"
    assert t.side == "device"
    assert t.leaf == "DeviceMSG"
    assert str(t) == "snowbot/2440011234567890/device/DeviceMSG"


def test_parse_app_topic_with_nested_leaf() -> None:
    t = topics.parse("snowbot/abc/app/some/nested/leaf")
    assert t is not None
    assert t.side == "app"
    assert t.leaf == "some/nested/leaf"


def test_parse_rejects_foreign_topics() -> None:
    assert topics.parse("homeassistant/status") is None
    assert topics.parse("snowbot/abc/other/x") is None
    assert topics.parse("snowbot/abc") is None


def test_builders() -> None:
    assert topics.app("sn", "get_map") == "snowbot/sn/app/get_map"
    assert topics.device("sn", "heart_beat") == "snowbot/sn/device/heart_beat"
    assert topics.all_for(None) == "snowbot/+/#"
    assert topics.all_for("sn") == "snowbot/sn/#"
