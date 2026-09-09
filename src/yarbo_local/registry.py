"""Command registry loaded from ``protocol/commands.yaml``.

The registry is the allowlist. A command that is not in it cannot be sent. A
command whose status is ``candidate`` cannot be sent either unless the caller
explicitly opts in, which the Studio does and the Home Assistant integration
never does. Commands with risk ``confirm`` need an explicit confirmation flag
per call. Forbidden names are not entries at all; they exist only so tests can
assert nobody registered one.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
import fnmatch
import functools
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from .exceptions import CommandRefusedError

ACKS = frozenset({"data_feedback", "plan_feedback", "recharge_feedback", "none"})
RISKS = frozenset({"safe", "confirm"})


def data_path(name: str) -> Path:
    """Locate a protocol data file, installed or in a checkout."""
    packaged = Path(str(resources.files("yarbo_local").joinpath("data", name)))
    if packaged.exists():
        return packaged
    checkout = Path(__file__).resolve().parents[2] / "protocol" / name
    if checkout.exists():
        return checkout
    raise FileNotFoundError(f"protocol data file {name!r} not found")


def _tri(value: Any) -> bool | None:
    """Normalise controller/awake flags: true/false, or None for n/a and unknown."""
    if isinstance(value, bool):
        return value
    return None


@dataclass(frozen=True, slots=True)
class Command:
    """One registered command on ``snowbot/{sn}/app/<name>``."""

    name: str
    payload: Any
    ack: str
    controller: bool | None
    awake: bool
    heads: tuple[int, ...]
    risk: str
    status: str
    evidence: str | None
    firmware: str | None
    notes: str
    source: str

    @property
    def verified(self) -> bool:
        return self.status == "verified"

    @property
    def expects_reply(self) -> bool:
        return self.ack == "data_feedback"


class Registry:
    """The allowlist and the rules for using it."""

    def __init__(
        self,
        commands: dict[str, Command],
        forbidden: dict[str, list[str]],
        *,
        firmware_note: str = "",
    ) -> None:
        self._commands = commands
        self._forbidden = forbidden
        self.firmware_note = firmware_note

    @classmethod
    @functools.cache
    def default(cls) -> Registry:
        """The packaged registry, parsed once per process.

        Reads a file; hosts with an event loop should call this from an executor
        the first time.
        """
        return cls.load()

    @classmethod
    def load(cls, path: Path | None = None) -> Registry:
        source = path or data_path("commands.yaml")
        with source.open(encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
        commands: dict[str, Command] = {}
        for name, entry in raw["commands"].items():
            ack = str(entry.get("ack", "none")).split()[0]
            if ack not in ACKS:
                raise ValueError(f"{name}: unknown ack {ack!r}")
            risk = str(entry.get("risk", "safe"))
            if risk not in RISKS:
                raise ValueError(f"{name}: unknown risk {risk!r}")
            commands[name] = Command(
                name=name,
                payload=entry.get("payload", {}),
                ack=ack,
                controller=_tri(entry.get("controller")),
                awake=bool(entry.get("awake", False)),
                heads=tuple(int(h) for h in entry.get("heads", ())),
                risk=risk,
                status=str(entry.get("status", "candidate")),
                evidence=entry.get("evidence"),
                firmware=str(entry["firmware"]) if entry.get("firmware") else None,
                notes=str(entry.get("notes", "") or entry.get("verified_notes", "")),
                source=str(entry.get("source", "")),
            )
        forbidden = {
            group: [str(n).split(" ")[0] for n in names]
            for group, names in raw.get("forbidden", {}).items()
        }
        return cls(commands, forbidden, firmware_note=str(raw.get("firmware_note", "")))

    def __contains__(self, name: str) -> bool:
        return name in self._commands

    def __iter__(self) -> Iterator[Command]:
        return iter(self._commands.values())

    def get(self, name: str) -> Command:
        try:
            return self._commands[name]
        except KeyError:
            raise CommandRefusedError(f"{name!r} is not a registered command") from None

    @property
    def verified(self) -> list[Command]:
        return [c for c in self._commands.values() if c.verified]

    def is_forbidden(self, name: str) -> bool:
        for names in self._forbidden.values():
            for pattern in names:
                if fnmatch.fnmatchcase(name, pattern) or name == pattern:
                    return True
        return False

    def check(self, name: str, *, allow_candidates: bool, confirmed: bool) -> Command:
        """Return the command if it may be sent under these rules, else raise."""
        if self.is_forbidden(name):
            raise CommandRefusedError(f"{name!r} is forbidden and will never be sent")
        cmd = self.get(name)
        if not cmd.verified and not allow_candidates:
            raise CommandRefusedError(
                f"{name!r} is a candidate, not verified on hardware; refusing to send"
            )
        if cmd.risk == "confirm" and not confirmed:
            raise CommandRefusedError(f"{name!r} needs confirmed=True")
        return cmd
