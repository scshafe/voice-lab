"""Independently verify the canonical evidence behind a native lab-package preparation.

Every assertion is re-derived from durable evidence through the canonical Python modules.
The harness report is read only to name what to look up — session id, take ids — never as
the answer, so a harness that reported the wrong result still fails here.
"""

from __future__ import annotations

import json
import math
import wave
from pathlib import Path

from cole_voice_lab.canonical import sha256_file, write_canonical_json
from cole_voice_lab.capture import expected_recording_paths
from cole_voice_lab.intake import validate_capture_package
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_contracts import RecordingPackageMap
from cole_voice_lab.recording_packages import (
    load_recording_package_map,
    resolve_native_package_alias,
)
from cole_voice_lab.recording_takes import RecordingTakeStore

PROMPTS = (
    "package-room-tone",
    "package-reading-one",
    "package-reading-two",
    "package-reading-three",
)


def fixture_pcm(frames: int) -> bytes:
    """Recompute the fixture signal here; never trust a digest the harness printed."""

    period = b"".join(
        round(2_000_000 * math.sin(index * math.tau / 480)).to_bytes(3, "little", signed=True)
        for index in range(480)
    )
    return (period * ((frames + 479) // 480))[: frames * 3]


def read_pcm(path: Path) -> tuple[int, bytes]:
    with wave.open(str(path), "rb") as source:
        assert (
            source.getnchannels(),
            source.getsampwidth(),
            source.getframerate(),
        ) == (1, 3, 48000)
        frames = source.getnframes()
        return frames, source.readframes(frames)


def verify(directory: Path) -> None:
    layout = AssetLayout.validate(directory / "assets")
    report = json.loads((directory / "native" / "package-report.json").read_text())
    store = RecordingTakeStore(layout)
    take_ids = report["take_ids"]
    assert sorted(take_ids) == sorted(PROMPTS), "Harness did not record the exact fixture catalog"

    originals: dict[str, str] = {}
    for prompt in PROMPTS:
        take = store.get_take(take_ids[prompt])
        assert take is not None, f"{prompt} take is missing from its canonical store"
        assert take.reservation.prompt.prompt_id == prompt
        assert take.decision == "kept", f"{prompt} is not kept: {take.decision}"
        assert take.analysis == "verified", f"{prompt} is not verified: {take.analysis}"
        assert take.export == "prepared", f"{prompt} was not marked prepared: {take.export}"
        assert take.seal is not None
        original = layout.root / take.seal.original_path
        frames, pcm = read_pcm(original)
        assert frames == take.seal.audio.frames == 4801
        assert pcm == fixture_pcm(frames), f"{prompt} original is not the exact fixture signal"
        originals[prompt] = take.seal.original_sha256

    # The map is re-read through the canonical loader, which re-checks it against its own
    # immutable registration operation and re-validates it against the published package.
    package_root = layout.root / f"tmp/capture-{report['session_id']}"
    validated = validate_capture_package(package_root)
    package_map = load_recording_package_map(layout, report["session_id"], package=validated)
    assert package_map is not None, "Locally prepared package has no canonical take map"
    assert validated.qc_report.status == "pass", validated.qc_report.status
    assert package_map.manifest_sha256 == validated.manifest_sha256

    # Member paths are re-derived from the selected suite, not read back from the map.
    assert package_map.selected_suite is not None
    assert expected_recording_paths(package_map.selected_suite) == tuple(
        entry.package_relative_path for entry in package_map.entries
    )
    assert len(package_map.entries) == len(PROMPTS)

    ready = package_root / "READY"
    assert ready.exists(), "Published package never reached READY"
    stage = list(layout.root.glob("tmp/.native-package-stage-*"))
    assert not stage, f"Package staging directories survived publication: {stage}"

    for entry in package_map.entries:
        take = store.get_take(entry.take_id)
        assert take is not None and take.seal is not None
        original = layout.root / take.seal.original_path
        member = package_root / entry.package_relative_path
        assert sha256_file(member)[0] == sha256_file(original)[0] == take.seal.original_sha256
        # Copied, never hard-linked into a writable staging alias.
        assert member.stat().st_nlink == 1 and original.stat().st_nlink == 1
        assert member.stat().st_ino != original.stat().st_ino
        frames, pcm = read_pcm(member)
        assert (frames, pcm) == (4801, fixture_pcm(4801))
        # READY is published last, so every member predates it.
        assert member.stat().st_mtime <= ready.stat().st_mtime

    evidence = {
        "verified_take_count": len(PROMPTS),
        "original_sha256": originals,
        "exact_source_frames": 4801,
        "session_id": package_map.session_id,
        "manifest_sha256": package_map.manifest_sha256,
        "package_path": package_map.package_path,
        "selected_suite_id": package_map.selected_suite.suite_id,
        "entry_count": len(package_map.entries),
        "qc_status": validated.qc_report.status,
        "ready_last": True,
        "stage_removed": True,
        "distinct_inodes": True,
        "alias_identity_bound": aliases_bind_their_takes(layout, package_map),
        "foreign_map_is_none": load_recording_package_map(layout, "native-foreign-session-000")
        is None,
    }
    write_canonical_json(directory / "native/python-package-evidence.json", evidence)


def aliases_bind_their_takes(layout: AssetLayout, package_map: RecordingPackageMap) -> bool:
    """Every package member resolves back to its exact kept original take, by identity.

    This is the boundary reference and dataset consumers use, so a package alias that lost
    its take identity would silently become a way around a removal decision.
    """

    for entry in package_map.entries:
        take = resolve_native_package_alias(
            layout,
            subject_id=entry.subject_id,
            session_id=package_map.session_id,
            prompt_id=entry.prompt.prompt_id,
            source_sha256=entry.original_sha256,
            manifest_sha256=package_map.manifest_sha256,
        )
        assert take is not None, f"{entry.take_id} package alias does not resolve to a take"
        assert take.reservation.take_id == entry.take_id
        assert take.decision == "kept"
    return True
