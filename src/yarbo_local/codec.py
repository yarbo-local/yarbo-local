"""Payload codec for the Yarbo MQTT dialect.

Facts this module encodes (verified in the vendor SDK and two community projects):

- Payloads are JSON. On firmware 3.9.0 and later the JSON is zlib-compressed
  (RFC 1950, first byte 0x78). Before 3.9.0 it is plain UTF-8 JSON.
- ``heart_beat`` is always plain JSON regardless of firmware.
- Sending compressed payloads to firmware that expects plain JSON is dropped
  silently: no reply, no error. Getting the outbound encoding right matters.

The decoder never guesses on the way in: it tries zlib when the magic bytes
say zlib, then plain JSON, then falls back to a hex wrapper so a capture never
loses bytes. The encoder is explicit; callers decide ``compress`` based on
what they have observed from the robot (see :func:`observed_encoding`).
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any, Literal
import zlib

Encoding = Literal["zlib", "json", "raw"]

_ZLIB_FIRST = 0x78
_ZLIB_LEVEL_BYTES = frozenset({0x01, 0x5E, 0x9C, 0xDA})


def looks_zlib(raw: bytes) -> bool:
    """Return True when ``raw`` starts with a plausible zlib header."""
    return len(raw) >= 2 and raw[0] == _ZLIB_FIRST and raw[1] in _ZLIB_LEVEL_BYTES


def decode(raw: bytes) -> tuple[Any, Encoding]:
    """Decode an inbound payload.

    Returns the parsed JSON value and the encoding that was used. Undecodable
    bytes come back as ``{"_raw_hex": ...}`` with encoding ``"raw"`` so that a
    capture file never drops data.
    """
    if looks_zlib(raw):
        try:
            return json.loads(zlib.decompress(raw).decode("utf-8")), "zlib"
        except (zlib.error, UnicodeDecodeError, json.JSONDecodeError):
            pass
    try:
        return json.loads(raw.decode("utf-8")), "json"
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"_raw_hex": raw.hex()}, "raw"


def encode(value: Any, *, compress: bool) -> bytes:
    """Encode an outbound payload, compact JSON, zlib-compressed when asked."""
    data = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return zlib.compress(data) if compress else data


def observed_encoding(samples: dict[str, Encoding]) -> bool | None:
    """Decide whether to compress outbound payloads from observed inbound ones.

    ``samples`` maps topic leaf (for example ``"DeviceMSG"``) to the encoding
    seen on it. ``heart_beat`` is ignored because it is always plain. Returns
    True for zlib, False for plain JSON, or None when nothing informative was
    seen yet.
    """
    informative = [enc for leaf, enc in samples.items() if leaf != "heart_beat" and enc != "raw"]
    if not informative:
        return None
    return any(enc == "zlib" for enc in informative)


def firmware_wants_zlib(version: str | None) -> bool | None:
    """Apply the vendor SDK rule: zlib when ``DeviceMSG.version`` >= 3.9.0.

    Returns None when the version is unknown or unparsable, so the caller can
    fall back to :func:`observed_encoding`.
    """
    if not version:
        return None
    parts: list[int] = []
    for piece in version.strip().split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    if not parts:
        return None
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3]) >= (3, 9, 0)


def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten nested dicts into dotted paths. Lists are kept as leaves.

    ``{"BatteryMSG": {"capacity": 90}}`` becomes ``{"BatteryMSG.capacity": 90}``.
    Keys that already contain dots (``system_info`` does this) are kept verbatim,
    which is why consumers should treat paths as opaque strings.
    """
    out: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, inner in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(inner, dict):
                out.update(flatten(inner, path))
            else:
                out[path] = inner
    else:
        out[prefix or "$"] = value
    return out


def decode_blob(value: Any) -> tuple[Any, str] | None:  # noqa: PLR0911 - each return is a distinct verdict
    """Decode a ``data`` field that carries JSON one more level down.

    Firmware 3.14 returns ``get_map`` and ``get_all_map_backup`` payloads as a
    base64 string of zlib-compressed JSON inside the already-decoded reply, and
    some replies carry a JSON string. Returns the inner value and a label
    (``"b64zlib"`` or ``"jsonstr"``), or None when ``value`` is not a blob.
    """
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text[:1] in "{[":
        try:
            return json.loads(text), "jsonstr"
        except json.JSONDecodeError:
            return None
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not looks_zlib(raw):
        return None
    try:
        return json.loads(zlib.decompress(raw).decode("utf-8")), "b64zlib"
    except (zlib.error, UnicodeDecodeError, json.JSONDecodeError):
        return None
