"""The level pin (the Phase A guard) and the cal file, kept in the app's data dir.

``state.json`` holds the pinned level and the imported cal file;
``level-pins.jsonl`` logs every pin (a re-pin is a chain era event: the
sessions before and after it are not directly comparable). Nothing here is
secret or private: levels, a device name, a cal file's name and hash.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import chain
from .levels import InputLevel


class GuardError(Exception):
    """Recording is refused; the message says why and what to do."""


class NotPinned(GuardError):
    pass


class LevelMoved(GuardError):
    pass


class LevelMuted(GuardError):
    pass


class CalFileError(Exception):
    pass


@dataclass(frozen=True)
class Pin:
    level: InputLevel
    device_name: str
    pinned_at: str

    def to_json(self) -> dict:
        return {"chainId": chain.CHAIN_ID, "level": self.level.to_json(), "deviceName": self.device_name, "pinnedAt": self.pinned_at}

    @classmethod
    def from_json(cls, data: dict) -> Pin:
        return cls(InputLevel.from_json(data["level"]), str(data["deviceName"]), str(data["pinnedAt"]))


@dataclass(frozen=True)
class CalFile:
    name: str
    sha256: str
    serial: str | None
    path: Path


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def guard(pin: Pin | None, level: InputLevel) -> None:
    """Refuse unless ``level`` is the pinned setting, unmuted."""
    if level.muted:
        raise LevelMuted(f"the UMIK-1 input is muted ({level.control}); unmute it")
    if pin is None:
        raise NotPinned(
            f"the input level is not pinned yet. Set the UMIK-1's input level deliberately once "
            f"(now {level.volume}%), then press Pin level. Re-pinning later is a logged chain era event."
        )
    if not pin.level.same_setting(level):
        raise LevelMoved(
            f"the input level is {level.volume}% ({level.source}) but chain {chain.CHAIN_ID} is pinned at "
            f"{pin.level.volume}% ({pin.level.source}, {pin.pinned_at}). A moved slider invalidates the SPL "
            "reference: restore it, or re-pin deliberately (a logged era event)."
        )


class StateStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.state_path = self.data_dir / "state.json"
        self.log_path = self.data_dir / "level-pins.jsonl"
        self.cal_dir = self.data_dir / "calibration"

    def _read(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise GuardError(f"the app state file {self.state_path} is damaged ({type(exc).__name__}); fix or remove it") from exc
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        tmp.replace(self.state_path)

    # --- the pin --------------------------------------------------------------------
    def pin(self) -> Pin | None:
        raw = self._read().get("pin")
        return Pin.from_json(raw) if raw else None

    def set_pin(self, level: InputLevel, device_name: str) -> tuple[Pin | None, Pin]:
        """Pin ``level`` and log it; returns (previous, new). A muted input cannot be pinned."""
        if level.muted:
            raise LevelMuted("a muted input cannot be pinned; unmute it first")
        state = self._read()
        previous = Pin.from_json(state["pin"]) if state.get("pin") else None
        new = Pin(level, device_name, now_iso())
        state["pin"] = new.to_json()
        self._write(state)
        event = {
            "at": new.pinned_at,
            "event": "re-pin (chain era event)" if previous and not previous.level.same_setting(level) else "pin",
            "chainId": chain.CHAIN_ID,
            "deviceName": device_name,
            "level": level.to_json(),
            "previous": previous.level.to_json() if previous else None,
        }
        with self.log_path.open("a") as log:
            log.write(json.dumps(event) + "\n")
        return previous, new

    def log_check(self, kind: str, level: InputLevel | None, ok: bool, session_id: str | None = None) -> None:
        """Level reads around a session are logged too (counts and levels only)."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a") as log:
            log.write(json.dumps({"at": now_iso(), "event": kind, "ok": ok, "sessionId": session_id, "level": level.to_json() if level else None}) + "\n")

    # --- the cal file ---------------------------------------------------------------
    def cal_file(self) -> CalFile | None:
        raw = self._read().get("calFile")
        if not raw:
            return None
        return CalFile(raw["name"], raw["sha256"], raw.get("serial"), self.cal_dir / raw["name"])

    def import_cal_file(self, source: Path) -> CalFile:
        """Copy miniDSP's per-serial cal file into the data dir (the hash is taken from the copy)."""
        data = read_cal_file(Path(source))
        self.cal_dir.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(source).name)[:96] or "umik1-cal.txt"
        dest = self.cal_dir / name
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(dest)
        cal = CalFile(name, hashlib.sha256(data).hexdigest(), cal_serial(data), dest)
        state = self._read()
        state["calFile"] = {"name": cal.name, "sha256": cal.sha256, "serial": cal.serial, "importedAt": now_iso()}
        self._write(state)
        return cal

    def current_cal_sha256(self) -> tuple[CalFile | None, str | None]:
        """The imported cal file and its hash now (None if missing); a changed file is an error."""
        cal = self.cal_file()
        if cal is None:
            return None, None
        try:
            digest = hashlib.sha256(cal.path.read_bytes()).hexdigest()
        except OSError as exc:
            raise CalFileError(f"the imported cal file {cal.path} cannot be read; import it again") from exc
        if digest != cal.sha256:
            raise CalFileError(f"the imported cal file {cal.path} changed since it was imported; import it again")
        return cal, digest


_NUMBERS = re.compile(r"^\s*[-+]?\d+(\.\d+)?([eE][-+]?\d+)?[\s,;]+[-+]?\d+(\.\d+)?([eE][-+]?\d+)?(\s|$)")


def read_cal_file(path: Path) -> bytes:
    """A miniDSP cal file: text, a "Sens Factor"/serial header, then frequency/dB lines."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise CalFileError(f"cannot read {path}: {exc.strerror}") from exc
    if len(data) > 1024 * 1024:
        raise CalFileError("that file is too large to be a UMIK-1 calibration file")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    rows = sum(1 for line in text.splitlines() if _NUMBERS.match(line))
    if rows < 10:
        raise CalFileError("that does not look like a UMIK-1 calibration file (expected frequency / dB lines)")
    return data


def cal_serial(data: bytes) -> str | None:
    match = re.search(rb"SERNO:\s*([0-9-]{4,16})", data[:400])
    return match.group(1).decode() if match else None
