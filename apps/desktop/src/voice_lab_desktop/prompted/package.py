"""A finished prompted session as a capture package (phase 4c, D-042).

The package is what ``cole_voice_lab.capture.finalize_capture_session`` writes and the
strict validator accepts: the batch's suite (``catalog.batch_suite``), one WAV per batch
prompt at the path the suite gives it (room tone included), ``manifest.json``, ``qc.json``
and ``READY`` last. Each WAV is the ORIGINAL bytes of the prompt's take kept most recently,
copied (a new inode, 0600), never the trimmed frames: a trim is a local recipe (D-012), as
``recording_packages`` treats the Swift app's takes. Take history, takes not chosen, trims
and continuity evidence stay on this machine.

The device is ``CaptureDevice(backend="portaudio")`` (D-039): the app's capture worker,
whose continuity evidence is its own contract. ``captured_at`` is the earliest kept take's
request time, ``finalized_at`` the session's persisted finish time, so a retried build writes
the same bytes; an existing package is validated and reused, never overwritten.
"""

from __future__ import annotations

import os
import secrets
import shutil
from pathlib import Path
from typing import Final

from cole_voice_lab.audio import inspect_canonical_wav
from cole_voice_lab.canonical import open_regular_no_follow
from cole_voice_lab.capture import expected_recording_paths, finalize_capture_session
from cole_voice_lab.errors import SecurityError
from cole_voice_lab.intake import validate_capture_package
from cole_voice_lab.journey.measurement_package import DEFAULT_SUBJECT_ID
from cole_voice_lab.models import CaptureDevice

from . import catalog
from .store import PromptedStore, SessionRecord, StoreError, TakeRecord

CAPTURE_HOST: Final = "voice-lab-desktop"


class PackageNotReady(Exception):
    """The session cannot become a package yet; the message says what is missing."""


def kept_takes(store: PromptedStore, session: SessionRecord) -> dict[str, TakeRecord]:
    """The take each batch prompt contributes. Raises: ``PackageNotReady`` naming the
    prompts without a kept take."""
    chosen: dict[str, TakeRecord] = {}
    missing = []
    for prompt_id in session.prompt_ids:
        record = store.kept(session, prompt_id)
        if record is None:
            missing.append(prompt_id)
        else:
            chosen[prompt_id] = record
    if missing:
        names = ", ".join(missing[:6]) + (
            f" and {len(missing) - 6} more" if len(missing) > 6 else ""
        )
        raise PackageNotReady(f"{len(missing)} prompt(s) have no kept take: {names}")
    return chosen


def capture_device(takes: dict[str, TakeRecord]) -> CaptureDevice:
    """The package's device: the one every kept take was recorded on.

    Raises: ``PackageNotReady`` when the takes name different devices."""
    devices = sorted({record.device for record in takes.values()})
    if len(devices) != 1:
        raise PackageNotReady(
            "the kept takes were recorded on different devices: " + "; ".join(devices)
        )
    device = devices[0]
    return CaptureDevice(
        backend="portaudio",
        device_id=f"umik-1:{device}"[:500],
        display_name=f"miniDSP UMIK-1 via PortAudio ({device})"[:500],
    )


def build(store: PromptedStore, session: SessionRecord) -> tuple[Path, str]:
    """The finished session's package directory and its manifest digest.

    The session must be finished (``finished_at`` set). Raises: ``PackageNotReady`` (a
    prompt without a kept take; takes on different devices), ``StoreError`` (a kept take
    whose original changed on disk), ``ContractError``/``SecurityError`` from the capture
    rules."""
    if session.finished_at is None:
        raise PackageNotReady("finish the session first")
    takes = kept_takes(store, session)
    suite = catalog.batch_suite(session.catalog, session.prompt_ids)
    device = capture_device(takes)
    target = store.package_dir(session.session_id)
    if target.exists():
        validated = validate_capture_package(target)
        if {f.sha256 for f in validated.manifest.files} != {r.sha256 for r in takes.values()}:
            raise SecurityError(f"the package at {target} does not hold this session's kept takes")
        return target, validated.manifest_sha256
    session_dir = store.session_dir(session.session_id)
    sources: dict[str, Path] = {}
    for prompt_id, record in takes.items():
        original = session_dir / "takes" / prompt_id / str(record.take_number) / "original.wav"
        inspection = inspect_canonical_wav(original)
        if inspection.sha256 != record.sha256 or inspection.size_bytes != record.size_bytes:
            raise StoreError(
                f"take {record.take_number} of {prompt_id} changed on disk since it was "
                "recorded; it is never packaged"
            )
        sources[prompt_id] = original
    packages = store.packages_root
    packages.mkdir(mode=0o700, parents=True, exist_ok=True)
    work = packages / f".{session.session_id}.{secrets.token_hex(6)}"
    work.mkdir(mode=0o700)
    try:
        for prompt, relative in zip(suite.prompts, expected_recording_paths(suite), strict=True):
            destination = work / relative
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with (
                open_regular_no_follow(sources[prompt.prompt_id]) as source,
                open(destination, "xb") as sink,
            ):
                os.chmod(destination, 0o600)
                shutil.copyfileobj(source, sink, 1024 * 1024)
        captured_at = min(record.requested_at for record in takes.values())
        result = finalize_capture_session(
            work,
            session_id=session.session_id,
            subject_id=DEFAULT_SUBJECT_ID,
            prompt_suite=suite,
            device=device,
            captured_at=captured_at,
            finalized_at=session.finished_at,
            capture_host=CAPTURE_HOST,
        )
        validate_capture_package(work)
        os.rename(work, target)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return target, result.manifest_sha256
