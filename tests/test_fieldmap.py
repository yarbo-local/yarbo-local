"""Field map: seed plus fixtures makes fields.yaml; promotion round-trips."""

from pathlib import Path
import shutil

import pytest

from yarbo_local import fieldmap

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def protocol_dir(tmp_path: Path) -> Path:
    fixtures = tmp_path / "fixtures" / "3.14.11"
    fixtures.mkdir(parents=True)
    shutil.copy(REPO / "protocol/fixtures/3.14.11/get_device_msg-asleep.jsonl", fixtures)
    shutil.copy(REPO / "protocol/fields.seed.yaml", tmp_path)
    fieldmap.regenerate(tmp_path)
    return tmp_path


def test_generated_map_matches_checked_in_shape(protocol_dir: Path) -> None:
    fields = fieldmap.load_fields(protocol_dir / fieldmap.FIELDS_FILE)
    assert "BatteryMSG.capacity" in fields
    assert fields["BatteryMSG.capacity"]["entity"] == "sensor"
    assert fields["BatteryMSG.current"]["status"] == "verified"
    assert fields["BatteryMSG.capacity"]["status"] == "candidate"  # source: vendor SDK


def test_promote_writes_seed_and_regenerates(protocol_dir: Path) -> None:
    entry = fieldmap.promote(
        protocol_dir,
        "abnormal_msg.ctrl_error_code",
        {"entity": "sensor", "category": "diagnostic", "notes": "seen 0", "source": "capture"},
    )
    assert entry["status"] == "verified"
    assert entry["entity"] == "sensor"
    seed = fieldmap.load_seed(protocol_dir / fieldmap.SEED_FILE)
    assert seed["abnormal_msg.ctrl_error_code"]["notes"] == "seen 0"
    fields = fieldmap.load_fields(protocol_dir / fieldmap.FIELDS_FILE)
    assert fields["abnormal_msg.ctrl_error_code"]["status"] == "verified"


def test_promote_validates_entity(protocol_dir: Path) -> None:
    with pytest.raises(ValueError, match="entity must be one of"):
        fieldmap.promote(protocol_dir, "BatteryMSG.capacity", {"entity": "thermostat"})


def test_checked_in_map_is_current() -> None:
    """protocol/fields.yaml must equal what the seed and fixtures generate."""
    seed = fieldmap.load_seed(REPO / "protocol" / fieldmap.SEED_FILE)
    data = fieldmap.build(REPO / "protocol" / "fixtures", seed)
    assert fieldmap.render(data) == (REPO / "protocol" / fieldmap.FIELDS_FILE).read_text(
        encoding="utf-8"
    )
