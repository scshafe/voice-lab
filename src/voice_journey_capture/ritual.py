"""The spine ritual: the plan, the guard around every segment, and the package at the end.

The segments, their order, labels, modes, durations and file names are
voice-journey ``capture.mjs``'s ``SPINE_SEGMENTS``/``planSession`` (Breathing
Studio phase A): room silence (10 s), the 94 dB calibrator tone (10 s, only
with a calibrator), sustained /a/ soft and loud, a slow glide, the anchor
song and free practice (skippable). Timed segments stop themselves; manual
ones stop on the person's key.

The guard: the input level must equal the pin (and not be muted) when the
session starts, before and after every segment, and at every poll while
recording; any change, or a level that cannot be read, aborts the session.
An aborted session is kept locally and never uploaded.
"""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from . import __version__, chain
from .devices import AudioBackend, DeviceError, InputDevice
from .levels import InputLevel, LevelReader, LevelUnreadable
from .package import SESSION_SCHEMA_VERSION, file_entries, validate_manifest, write_session_json
from .pin import CalFile, GuardError, Pin, StateStore, guard
from .recorder import SegmentRecording, SegmentResult, probe
from .store import SessionStore


@dataclass(frozen=True)
class SpineSegment:
    key: str
    label: str
    mode: str  # "timed" | "manual"
    seconds: int | None = None
    requires: str | None = None  # an option that must be on
    skip: str | None = None  # an option that removes it


SPINE_SEGMENTS: tuple[SpineSegment, ...] = (
    SpineSegment("room-silence", "room silence — stand still, say nothing (noise floor)", "timed", 10),
    SpineSegment("cal-tone", "94 dB calibrator tone on the capsule", "timed", 10, requires="cal_tone"),
    SpineSegment("vowel-soft", "sustained /a/ — comfortable pitch, soft", "manual"),
    SpineSegment("vowel-loud", "sustained /a/ — same pitch, full voice", "manual"),
    SpineSegment("glide", "slow glide — bottom of range to top and back", "manual"),
    SpineSegment("anchor-song", "the anchor song — one verse, same song every session", "manual"),
    SpineSegment("free-practice", "free practice — everything else, mic keeps rolling", "manual", skip="skip_free"),
)

#: A manual segment shorter than this is refused (an accidental double press); redo it.
MIN_MANUAL_SECONDS = 1.0


@dataclass(frozen=True)
class PlannedSegment:
    key: str
    label: str
    mode: str
    seconds: int | None
    file: str


def plan_segments(cal_tone: bool = False, skip_free: bool = False) -> list[PlannedSegment]:
    options = {"cal_tone": cal_tone, "skip_free": skip_free}
    out: list[PlannedSegment] = []
    for segment in SPINE_SEGMENTS:
        if segment.requires and not options[segment.requires]:
            continue
        if segment.skip and options[segment.skip]:
            continue
        out.append(PlannedSegment(segment.key, segment.label, segment.mode, segment.seconds, f"{len(out) + 1:02d}-{segment.key}.wav"))
    return out


def session_id_for(moment: datetime) -> str:
    """``s-YYYYMMDD-HHMMSS`` in local time, as capture.mjs names sessions."""
    return moment.strftime("s-%Y%m%d-%H%M%S")


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class RitualError(Exception):
    """The session cannot start or continue; the message says why."""


@dataclass
class Ritual:
    backend: AudioBackend
    device: InputDevice
    levels: LevelReader
    state: StateStore
    sessions: SessionStore
    distance_cm: int
    cal_tone: bool = False
    skip_free: bool = False
    now: Callable[[], datetime] = field(default=lambda: datetime.now().astimezone())
    sample_rate: int = chain.SAMPLE_RATE

    plan: list[PlannedSegment] = field(init=False, default_factory=list)
    session_id: str = field(init=False, default="")
    session_dir: Path | None = field(init=False, default=None)
    started_at: datetime | None = field(init=False, default=None)
    pin: Pin | None = field(init=False, default=None)
    level: InputLevel | None = field(init=False, default=None)
    cal: CalFile | None = field(init=False, default=None)
    cal_sha256: str | None = field(init=False, default=None)
    results: list[dict] = field(init=False, default_factory=list)
    recording: SegmentRecording | None = field(init=False, default=None)
    status: str = field(init=False, default="new")  # new | open | completed | aborted
    error: str | None = field(init=False, default=None)
    manifest: dict | None = field(init=False, default=None)
    _segment_started_at: datetime | None = field(init=False, default=None)

    # --- checks ----------------------------------------------------------------------
    def read_level(self, when: str) -> InputLevel:
        """Read and guard the level; any failure is logged and raised (the caller aborts)."""
        try:
            level = self.levels.read(self.device)
        except LevelUnreadable as exc:
            self.state.log_check(when, None, False, self.session_id or None)
            raise RitualError(f"the input level cannot be read ({exc}); recording is refused") from exc
        try:
            guard(self.pin, level)
        except GuardError as exc:
            self.state.log_check(when, level, False, self.session_id or None)
            raise RitualError(str(exc)) from exc
        return level

    # --- the session -----------------------------------------------------------------
    def begin(self) -> None:
        """Every precondition, then the session dir. Nothing is created when a check fails."""
        if self.status != "new":
            raise RitualError("this session has already started")
        self.pin = self.state.pin()
        self.level = self.read_level("session-start")
        self.cal, self.cal_sha256 = self.state.current_cal_sha256()
        try:
            stats = probe(self.backend, self.device, self.sample_rate)
        except DeviceError as exc:
            raise RitualError(str(exc)) from exc
        if stats.digital_silence:
            hint = (" On macOS, allow microphone access for the app that runs this one (System Settings → "
                    "Privacy & Security → Microphone)." if sys.platform == "darwin" else "")
            raise RitualError(f"the UMIK-1 delivers digital silence (every sample is zero); a live capsule never does.{hint}")
        start = self.now()
        self.session_id = session_id_for(start)
        try:
            self.session_dir = self.sessions.new_dir(self.session_id)
        except FileExistsError as exc:
            raise RitualError("a session with this id already exists; wait a second and start again") from exc
        self.started_at = start
        self.plan = plan_segments(self.cal_tone, self.skip_free)
        self.status = "open"
        self.state.log_check("session-start", self.level, True, self.session_id)

    @property
    def index(self) -> int:
        """The next segment to record (== len(plan) when all are done)."""
        return len(self.results)

    @property
    def current(self) -> PlannedSegment | None:
        return self.plan[self.index] if self.status == "open" and self.index < len(self.plan) else None

    def start_segment(self) -> SegmentRecording:
        segment = self.current
        if segment is None or self.recording is not None:
            raise RitualError("no segment is waiting to be recorded")
        assert self.session_dir is not None
        try:
            self.read_level(f"before {segment.key}")
        except RitualError as exc:
            self.abort(str(exc))
            raise
        max_frames = segment.seconds * self.sample_rate if segment.seconds else None
        try:
            self.recording = SegmentRecording(self.backend, self.device, self.session_dir / segment.file, self.sample_rate, max_frames)
            self._segment_started_at = self.now()
            self.recording.start()
        except DeviceError as exc:
            self.recording = None
            self.abort(str(exc))
            raise RitualError(str(exc)) from exc
        return self.recording

    def poll_level(self) -> None:
        """While recording: the level must not move. A change aborts the session."""
        if self.recording is None:
            return
        segment = self.current
        try:
            self.read_level(f"during {segment.key if segment else '?'}")
        except RitualError as exc:
            self.abort(str(exc))
            raise

    def finish_segment(self) -> dict:
        """Stop (manual) or collect (timed) the current segment and check the level after it."""
        segment = self.current
        recording = self.recording
        if segment is None or recording is None:
            raise RitualError("no segment is recording")
        self.recording = None
        try:
            result: SegmentResult = recording.stop()
        except DeviceError as exc:
            self.abort(str(exc))
            raise RitualError(str(exc)) from exc
        try:
            self.read_level(f"after {segment.key}")
        except RitualError as exc:
            self.abort(str(exc))
            raise
        if segment.mode == "manual" and result.frames < MIN_MANUAL_SECONDS * self.sample_rate:
            (self.session_dir / segment.file).unlink(missing_ok=True)
            raise RitualError(f"that take of {segment.key} was shorter than {MIN_MANUAL_SECONDS:.0f} s; record it again")
        if result.digital_silence:
            self.abort(f"{segment.key} recorded digital silence (every sample zero): the input stopped delivering audio")
            raise RitualError(self.error or "digital silence")
        entry = {
            "key": segment.key,
            "file": segment.file,
            "label": segment.label,
            "mode": segment.mode,
            "startedAt": iso(self._segment_started_at or self.now()),
            "sizeBytes": result.size_bytes,
            "frames": result.frames,
            "durationSeconds": round(result.frames / self.sample_rate, 3),
            "peakDbfs": result.peak_dbfs,
            "rmsDbfs": result.rms_dbfs,
            "clippedSamples": result.clipped_samples,
            "inputOverflows": result.input_overflows,
        }
        self.results.append(entry)
        return entry

    def redo_last(self) -> None:
        """Throw away the last finished segment so it is recorded again."""
        if self.status != "open" or self.recording is not None or not self.results:
            raise RitualError("there is no finished segment to redo")
        last = self.results.pop()
        assert self.session_dir is not None
        (self.session_dir / last["file"]).unlink(missing_ok=True)

    def complete(self) -> dict:
        if self.status != "open" or self.index < len(self.plan):
            raise RitualError("the session is not finished")
        assert self.session_dir is not None and self.level is not None
        try:
            self.read_level("session-end")
        except RitualError as exc:
            self.abort(str(exc))
            raise
        manifest = self._manifest("completed")
        manifest["files"] = file_entries(self.session_dir, [r["file"] for r in self.results])
        validate_manifest(manifest)
        write_session_json(self.session_dir, manifest)
        self.status = "completed"
        self.manifest = manifest
        self.state.log_check("session-complete", self.level, True, self.session_id)
        return manifest

    def abort(self, reason: str) -> None:
        """Stop everything; keep what was recorded with the reason; never uploaded."""
        if self.status in ("completed", "aborted"):
            return
        if self.recording is not None:
            recording, self.recording = self.recording, None
            try:
                recording.stop()
            except DeviceError:
                pass
        self.error = reason
        if self.status == "open" and self.session_dir is not None:
            write_session_json(self.session_dir, self._manifest("aborted"))
        self.status = "aborted"

    def _manifest(self, status: str) -> dict:
        assert self.started_at is not None and self.level is not None and self.pin is not None
        out = {
            "schemaVersion": SESSION_SCHEMA_VERSION,
            "sessionId": self.session_id,
            "chainId": chain.CHAIN_ID,
            "chainKind": chain.CHAIN_KIND,
            "controlled": chain.CONTROLLED,
            "startedAt": iso(self.started_at),
            "endedAt": iso(self.now()),
            "status": status,
            "capture": {
                "sampleRate": self.sample_rate,
                "bitDepth": chain.BIT_DEPTH,
                "channels": chain.CHANNELS,
                "inputVolume": self.level.volume,
                "distanceCm": self.distance_cm,
                "calFile": self.cal.name if self.cal else None,
                "calFileSha256": self.cal_sha256,
                "calSerial": self.cal.serial if self.cal else None,
                "inputLevel": {k: v for k, v in self.level.to_json().items() if k != "volume"},
                "levelPinnedAt": self.pin.pinned_at,
                "device": {"name": self.device.name, "hostApi": self.device.host_api, "usbId": self.device.usb_id},
                "client": {"name": "voice-journey-capture", "version": __version__, "platform": sys.platform, "machine": platform.machine()},
            },
            "segments": list(self.results),
        }
        if self.error and status == "aborted":
            out["error"] = self.error
        return out
