"""Best-effort redaction for captures that will be shared as fixtures.

What it does:

- Serial numbers become a stable per-capture token (``SN-xxxxxxxx``) in topics
  and in any string value, so correlation within one capture survives and
  correlation across captures does not.
- Geographic coordinates are shifted by a per-capture offset so that geometry
  is preserved but absolute position is not. This covers ``latitude`` /
  ``longitude`` / ``lat`` / ``lon`` / ``lan`` keys (the firmware misspells one)
  and NMEA ``$GNGGA`` / ``$GPGGA`` sentences, whose checksum is recomputed.
- MAC addresses, IPv4 addresses, Wi-Fi SSIDs and passwords, and the base
  station name are replaced.

What it does not do: guarantee anonymity. Review a redacted file before
publishing it. Field names are kept intact on purpose, they are the point.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import secrets
from typing import Any

_MAC_RE = re.compile(r"\b(?:[0-9A-Fa-f]{1,2}[:-]){5}[0-9A-Fa-f]{1,2}\b")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_GGA_RE = re.compile(r"\$G[NP]GGA,[^*\r\n\"]*(?:\*[0-9A-Fa-f]{2})?")

_LAT_KEYS = frozenset({"latitude", "lat", "lan"})
_LON_KEYS = frozenset({"longitude", "lon", "lng"})
_DROP_KEYS = frozenset(
    {
        "ssid",
        "password",
        "psk",
        "base_name",
        "basename",  # modebase_info.BaseName
        "name_of_wifi",
        "lat_lon_hight",  # read_gps_ref: "lat lon height" as one string
        "lte_iccid",
        "iccid",
        "imei",
        "head_sn",
        "body_sn",
    }
)


def _json_string(text: str) -> Any | None:
    """Parse ``text`` when it is a JSON object or array, else None."""
    stripped = text.strip()
    if stripped[:1] not in "{[":
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def _nmea_checksum(body: str) -> str:
    value = 0
    for ch in body:
        value ^= ord(ch)
    return f"{value:02X}"


def _dm_to_decimal(text: str, degree_digits: int) -> float | None:
    try:
        degrees = float(text[:degree_digits])
        minutes = float(text[degree_digits:])
    except ValueError:
        return None
    return degrees + minutes / 60.0


def _decimal_to_dm(value: float, degree_digits: int) -> str:
    degrees = int(abs(value))
    minutes = (abs(value) - degrees) * 60.0
    return f"{degrees:0{degree_digits}d}{minutes:07.4f}"


@dataclass
class Redactor:
    """Stateful redactor; one instance per capture keeps tokens stable."""

    salt: str = field(default_factory=lambda: secrets.token_hex(8))
    lat_offset: float = field(default_factory=lambda: secrets.randbelow(120_000) / 1000 - 60.0)
    lon_offset: float = field(default_factory=lambda: secrets.randbelow(300_000) / 1000 - 150.0)
    _serials: dict[str, str] = field(default_factory=dict)

    def serial_token(self, serial: str) -> str:
        if serial not in self._serials:
            digest = hashlib.sha256(f"{self.salt}:{serial}".encode()).hexdigest()[:8]
            self._serials[serial] = f"SN-{digest}"
        return self._serials[serial]

    def register_serial(self, serial: str) -> None:
        self.serial_token(serial)

    # -- coordinates -------------------------------------------------------

    def shift_lat(self, lat: float) -> float:
        shifted = lat + self.lat_offset
        return max(-89.0, min(89.0, shifted))

    def shift_lon(self, lon: float) -> float:
        shifted = lon + self.lon_offset
        while shifted > 180.0:
            shifted -= 360.0
        while shifted < -180.0:
            shifted += 360.0
        return shifted

    def redact_gga(self, sentence: str) -> str:
        """Shift the position inside a GGA sentence and fix its checksum."""
        star = sentence.rfind("*")
        body = sentence[1:star] if star > 0 else sentence[1:]
        fields = body.split(",")
        if len(fields) < 6 or not fields[0].endswith("GGA"):
            return sentence
        lat = _dm_to_decimal(fields[2], 2) if fields[2] else None
        lon = _dm_to_decimal(fields[4], 3) if fields[4] else None
        if lat is not None:
            signed = -lat if fields[3] == "S" else lat
            new_lat = self.shift_lat(signed)
            fields[2] = _decimal_to_dm(new_lat, 2)
            fields[3] = "S" if new_lat < 0 else "N"
        if lon is not None:
            signed = -lon if fields[5] == "W" else lon
            new_lon = self.shift_lon(signed)
            fields[4] = _decimal_to_dm(new_lon, 3)
            fields[5] = "W" if new_lon < 0 else "E"
        new_body = ",".join(fields)
        if star <= 0:
            return f"${new_body}"  # sentence arrived without a checksum; keep it that way
        return f"${new_body}*{_nmea_checksum(new_body)}"

    # -- generic walkers ---------------------------------------------------

    def redact_text(self, text: str) -> str:
        for serial, token in self._serials.items():
            text = text.replace(serial, token)
        text = _GGA_RE.sub(lambda m: self.redact_gga(m.group(0)), text)
        text = _MAC_RE.sub("00:00:00:00:00:00", text)
        return _IPV4_RE.sub("0.0.0.0", text)  # noqa: S104 - replacement token, not a bind

    def redact_topic(self, topic: str) -> str:
        parts = topic.split("/")
        if len(parts) >= 2 and parts[0] == "snowbot" and parts[1] != "+":
            parts[1] = self.serial_token(parts[1])
        return "/".join(parts)

    def redact_value(self, value: Any, key: str = "") -> Any:
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for k, v in value.items():
                lowered = str(k).lower()
                if lowered in _DROP_KEYS:
                    out[k] = "REDACTED"
                elif (
                    lowered in _LAT_KEYS and isinstance(v, int | float) and not isinstance(v, bool)
                ):
                    out[k] = self.shift_lat(float(v)) if v != 0 else v
                elif (
                    lowered in _LON_KEYS and isinstance(v, int | float) and not isinstance(v, bool)
                ):
                    out[k] = self.shift_lon(float(v)) if v != 0 else v
                else:
                    out[k] = self.redact_value(v, lowered)
            return out
        if isinstance(value, list):
            return [self.redact_value(v, key) for v in value]
        if isinstance(value, str):
            nested = _json_string(value)
            if nested is not None:
                # A JSON document carried as a string (modebase_info.ModeBase does this).
                return json.dumps(self.redact_value(nested), separators=(",", ":"))
            return self.redact_text(value)
        return value


def redactor_for_salt(salt: str | None) -> Redactor:
    """A Redactor whose coordinate offsets are derived from ``salt``.

    With a shared salt, several captures from one robot get the same serial
    token and the same geometric shift, so they stay consistent as a set.
    """
    if salt is None:
        return Redactor()
    digest = hashlib.sha256(f"offsets:{salt}".encode()).digest()
    lat = int.from_bytes(digest[:4], "big") % 120_000 / 1000 - 60.0
    lon = int.from_bytes(digest[4:8], "big") % 300_000 / 1000 - 150.0
    return Redactor(salt=salt, lat_offset=lat, lon_offset=lon)


def redact_file(src: Path, dst: Path, *, salt: str | None = None) -> int:
    """Rewrite a capture JSONL with every record redacted. Returns the record count."""
    redactor = redactor_for_salt(salt)
    # First pass: learn serials from topics so replacements inside payload strings work.
    with src.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                topic = json.loads(line).get("topic", "")
                parts = str(topic).split("/")
                if len(parts) >= 2 and parts[0] == "snowbot" and parts[1] != "+":
                    redactor.register_serial(parts[1])
    count = 0
    dst.parent.mkdir(parents=True, exist_ok=True)
    with src.open(encoding="utf-8") as fin, dst.open("w", encoding="utf-8") as fout:
        for line in fin:
            if not line.strip():
                continue
            rec = json.loads(line)
            rec["topic"] = redactor.redact_topic(str(rec.get("topic", "")))
            rec["payload"] = redactor.redact_value(rec.get("payload"))
            fout.write(json.dumps(rec, separators=(",", ":")) + "\n")
            count += 1
    return count
