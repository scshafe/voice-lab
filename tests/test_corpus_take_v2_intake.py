"""v2 takes into the PC (D-056 step 2): the validator, the PC's rules, the record and the vault.

A v2 package travels as a v1 package does (the same five files), and every dispatch point
knows it: ``intake.validate_package``, ``_raw_session_id`` and ``_admit``, ``record_takes``,
the vault's raw source and the recording library's raw checks.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from corpus_take_fixtures import make_take, seed_catalog
from panel_take_fixtures import (
    PERIOD_FRAMES,
    make_profile_take,
    reseal_profile_take,
)
from pydantic import ValidationError

from cole_voice_lab.alsa_evidence import period_deviations_ns
from cole_voice_lab.canonical import canonical_json_bytes, sha256_file, write_canonical_json
from cole_voice_lab.corpus_take_store import (
    CorpusTakeState,
    TakeProfileSummary,
    read_take_state,
    read_take_states,
    record_takes,
    state_path,
)
from cole_voice_lab.corpus_takes import (
    AUDIO_NAME,
    CONTINUITY_NAME,
    TAKE_FILES,
    ValidatedProfileTake,
    is_corpus_take_package,
    validate_profile_take_package,
)
from cole_voice_lab.diagnostic_roots import DIAGNOSTIC_SUBJECT_PREFIX, initialize_diagnostic_root
from cole_voice_lab.errors import ContractError, SecurityError
from cole_voice_lab.intake import validate_package
from cole_voice_lab.intake_publisher import publish_pending
from cole_voice_lab.library_replication import RawPackageSource, ReplicationItem
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_contracts import CatalogReference, CatalogSnapshot

type Catalog = tuple[CatalogReference, CatalogSnapshot]
NOW = datetime(2026, 10, 19, 12, 30, tzinfo=UTC)


@pytest.fixture
def layout(tmp_path: Path) -> AssetLayout:
    return AssetLayout.initialize(tmp_path / "data" / "voice-lab" / "lab")


@pytest.fixture
def catalog(layout: AssetLayout) -> Catalog:
    return seed_catalog(layout)


@pytest.fixture
def work(tmp_path: Path) -> Path:
    directory = tmp_path / "work"
    directory.mkdir()
    return directory


@pytest.fixture
def package(work: Path) -> Path:
    make_profile_take(work / "take")
    return work / "take"


def incoming(layout: AssetLayout, take: ValidatedProfileTake, package: Path) -> Path:
    """A copy of ``package`` where the API's staging would finalize it."""
    target = layout.directory("incoming") / take.session_id
    shutil.copytree(package, target)
    return target


# -- The validator ----------------------------------------------------------------------------


def test_a_v2_package_is_validated_and_judged_by_its_primary_channel(package: Path) -> None:
    take = validate_package(package)
    assert isinstance(take, ValidatedProfileTake)
    assert take == validate_profile_take_package(package)
    assert {path.name for path in package.iterdir()} == TAKE_FILES
    assert is_corpus_take_package(package)
    assert take.session_id == take.manifest.take_id
    assert take.evidence.frames == take.manifest.audio.audio.frames == 4 * PERIOD_FRAMES
    # The raw microphone of the primary channel is quiet: reported, not relaxed (D-056).
    [metric] = take.qc_report.metrics
    assert metric.path == AUDIO_NAME and metric.findings == ("peak-too-quiet",)
    assert take.qc_report.status == "review"


def test_qc_is_the_primary_channels_alone(work: Path) -> None:
    # Channel 3 (microphone 1) silent, every other channel loud: the take is judged silent.
    frames = 4 * PERIOD_FRAMES
    loud = (1 << 30).to_bytes(4, "little", signed=True)
    silent = (0).to_bytes(4, "little", signed=True)
    payload = b"".join(
        b"".join(silent if channel == 2 else loud for channel in range(6))
        for _frame in range(frames)
    )
    take = make_profile_take(work / "take", payload=payload)
    [metric] = take.qc_report.metrics
    assert "digital-silence-or-dropout" in metric.findings
    assert metric.peak_dbfs == -300.0


@pytest.mark.parametrize(("channels", "alsa_format"), [(2, "S16_LE"), (6, "S24_3LE")])
def test_every_image_and_width_is_validated(work: Path, channels: int, alsa_format: str) -> None:
    take = make_profile_take(work / "take", channels=channels, alsa_format=alsa_format)
    assert take.manifest.audio.audio.channels == channels


def test_tampered_audio_or_evidence_is_refused(package: Path) -> None:
    wav = package / AUDIO_NAME
    original = wav.read_bytes()
    wav.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
    with pytest.raises(SecurityError, match="recording hash or byte count was tampered"):
        validate_profile_take_package(package)
    wav.write_bytes(original)
    evidence = package / CONTINUITY_NAME
    document = json.loads(evidence.read_bytes())
    document["pcm"] = "hw:CARD=Other,DEV=0"
    write_canonical_json(evidence, document)
    with pytest.raises(SecurityError, match="continuity evidence hash or byte count"):
        validate_profile_take_package(package)


def _rebind_evidence(package: Path, **changes: Any) -> None:
    """Change the evidence, then bind the manifest's summary to its new digest only."""
    path = package / CONTINUITY_NAME
    write_canonical_json(path, {**json.loads(path.read_bytes()), **changes})
    sha256, size = sha256_file(path)
    reseal_profile_take(package, continuity={"sha256": sha256, "size_bytes": size})


def test_the_evidence_is_bound_to_the_wav_and_to_its_summary(package: Path) -> None:
    _rebind_evidence(package, pcm="hw:CARD=Other,DEV=0")
    with pytest.raises(ContractError, match="continuity summary disagrees"):
        validate_profile_take_package(package)


def test_the_evidence_describes_this_wav(work: Path) -> None:
    take = make_profile_take(work / "take")
    other = dict(take.evidence.model_dump(mode="json")["wav"], sha256="f" * 64)
    _rebind_evidence(work / "take", wav=other)
    with pytest.raises(SecurityError, match="describes other audio"):
        validate_profile_take_package(work / "take")


def test_the_wav_is_in_the_format_the_pcm_was_opened_with(work: Path) -> None:
    # The same byte count in another layout: 12 channels of s16 for 6 of s32.
    make_profile_take(work / "take")
    _rebind_evidence(work / "take", channels=12, format="S16_LE")
    with pytest.raises(ContractError, match="format the PCM was opened with"):
        validate_profile_take_package(work / "take")


def test_unsuccessful_evidence_is_refused(work: Path) -> None:
    make_profile_take(work / "take")
    _rebind_evidence(work / "take", success=False, error="snd_pcm_readi: -32", wav=None)
    with pytest.raises(ContractError, match="did not succeed: snd_pcm_readi"):
        validate_profile_take_package(work / "take")


def test_a_lost_period_fails_validation(work: Path) -> None:
    # The client measured it honestly, and it is still beyond the tolerance.
    take = make_profile_take(work / "take")
    pairs = [list(pair) for pair in take.evidence.periods]
    lost = pairs[:2] + [[position, timestamp + 64_000_000] for position, timestamp in pairs[2:]]
    deviations = period_deviations_ns([tuple(pair) for pair in lost], 16_000)
    _rebind_evidence(
        work / "take",
        periods=lost,
        max_abs_deviation_ns=max(abs(value) for value in deviations),
    )
    with pytest.raises(ContractError, match="beyond the tolerance"):
        validate_profile_take_package(work / "take")


def test_the_file_set_is_exact(package: Path) -> None:
    (package / "notes.txt").write_text("undeclared")
    with pytest.raises(SecurityError, match="undeclared=\\['notes.txt'\\]"):
        validate_profile_take_package(package)


# -- Publishing and the PC's rules --------------------------------------------------------------


def test_a_v2_take_is_published_with_its_profile_in_its_record(
    layout: AssetLayout, catalog: Catalog, work: Path
) -> None:
    take = make_profile_take(work / "take", catalog=catalog)
    incoming(layout, take, work / "take")
    summary = publish_pending(layout, min_free_bytes=0, clock=lambda: NOW)
    assert [receipt.session_id for receipt in summary.accepted] == [take.session_id]
    assert summary.takes_recorded == [take.session_id]
    raw = layout.directory("raw") / take.manifest_sha256
    published = validate_package(raw, allow_intake_receipt=True)
    assert isinstance(published, ValidatedProfileTake) and published.manifest == take.manifest
    state = read_take_state(layout, take.session_id)
    assert state is not None
    assert state.capture_profile == TakeProfileSummary(
        profile="xvf3800-linear-4",
        room="bathroom",
        device_model="reSpeaker Flex XVF3800 Linear-4",
        firmware_version=(1, 0, 4),
        primary_channel=3,
    )
    assert state.device == "reSpeaker Flex XVF3800 Linear-4"
    assert state.audio == take.manifest.audio and state.qc_status == "review"
    assert state.origin is None
    # The record round-trips through its file, and is rebuilt from raw/ byte for byte.
    path = state_path(layout, take.session_id)
    payload = path.read_bytes()
    assert CorpusTakeState.model_validate_json(payload) == state
    path.unlink()
    assert record_takes(layout)[1] == (take.session_id,)
    assert path.read_bytes() == payload


def test_a_v1_record_keeps_its_bytes_beside_a_v2_one(
    layout: AssetLayout, catalog: Catalog, work: Path
) -> None:
    desktop = make_take(work / "desktop", catalog=catalog)
    shutil.copytree(work / "desktop", layout.directory("incoming") / desktop.session_id)
    panel = make_profile_take(work / "panel", catalog=catalog)
    incoming(layout, panel, work / "panel")
    publish_pending(layout, min_free_bytes=0)
    desktop_bytes = state_path(layout, desktop.session_id).read_bytes()
    assert b"capture_profile" not in desktop_bytes
    assert b'"capture_profile"' in state_path(layout, panel.session_id).read_bytes()
    states = {state.take_id: state for state in read_take_states(layout)}
    assert states[desktop.session_id].capture_profile is None
    assert canonical_json_bytes(states[desktop.session_id]) + b"\n" == desktop_bytes


def test_simulated_audio_and_a_diagnostic_root_are_refused(
    layout: AssetLayout, catalog: Catalog, work: Path
) -> None:
    simulated = make_profile_take(work / "simulated", catalog=catalog, simulated=True)
    incoming(layout, simulated, work / "simulated")
    [refusal] = publish_pending(layout, min_free_bytes=0).refused
    assert refusal.error.startswith("take_simulated: ")
    assert list(layout.directory("raw").iterdir()) == []


def test_a_diagnostic_root_takes_no_v2_take(tmp_path: Path, work: Path) -> None:
    marked, _marker = initialize_diagnostic_root(
        tmp_path / "diagnostic" / "assets", subject_id=f"{DIAGNOSTIC_SUBJECT_PREFIX}panel"
    )
    take = make_profile_take(work / "take")
    incoming(marked, take, work / "take")
    [refusal] = publish_pending(marked, min_free_bytes=0).refused
    assert refusal.error.startswith("take_diagnostic_root: ")


def test_a_v2_take_of_a_prompt_its_catalog_does_not_hold_is_refused(
    layout: AssetLayout, catalog: Catalog, work: Path
) -> None:
    prompt = catalog[1].suite.prompts[0].model_copy(update={"text": "Words nobody was asked."})
    take = make_profile_take(work / "take", catalog=catalog, prompt=prompt)
    incoming(layout, take, work / "take")
    [refusal] = publish_pending(layout, min_free_bytes=0).refused
    assert refusal.error.startswith("take_prompt_not_in_catalog: ")


def test_a_v2_take_waits_for_its_catalog_version(
    layout: AssetLayout, catalog: Catalog, work: Path
) -> None:
    reference = catalog[0].model_copy(update={"catalog_version": 7})
    take = make_profile_take(work / "take", catalog=(reference, catalog[1]))
    incoming(layout, take, work / "take")
    summary = publish_pending(layout, min_free_bytes=0)
    assert summary.accepted == [] and summary.refused == []
    assert [entry["error"].split(":")[0] for entry in summary.deferred] == ["take_catalog_missing"]


def test_the_same_audio_described_again_is_a_collision(
    layout: AssetLayout, catalog: Catalog, work: Path
) -> None:
    first = make_profile_take(work / "first", catalog=catalog)
    incoming(layout, first, work / "first")
    assert len(publish_pending(layout, min_free_bytes=0).accepted) == 1
    later = make_profile_take(work / "later", catalog=catalog, requested_at="2026-10-19T12:01:00Z")
    assert later.session_id == first.session_id
    assert later.manifest_sha256 != first.manifest_sha256
    incoming(layout, later, work / "later")
    [refusal] = publish_pending(layout, min_free_bytes=0).refused
    assert "session ID collision" in refusal.error
    # The identical package again is a verified duplicate.
    incoming(layout, first, work / "first")
    assert publish_pending(layout, min_free_bytes=0).accepted[0].session_id == first.session_id


# -- The record's own rules, and the vault ------------------------------------------------------


def test_the_record_holds_a_profiles_audio_only_with_its_profile(
    layout: AssetLayout, catalog: Catalog, work: Path
) -> None:
    take = make_profile_take(work / "take", catalog=catalog)
    incoming(layout, take, work / "take")
    publish_pending(layout, min_free_bytes=0)
    document = json.loads(state_path(layout, take.session_id).read_bytes())
    without = {key: value for key, value in document.items() if key != "capture_profile"}
    with pytest.raises(ValidationError, match="canonical format"):
        CorpusTakeState.model_validate_json(canonical_json_bytes(without))
    imported = {**document, "origin": {"kind": "imported", "source_take_id": document["take_id"]}}
    with pytest.raises(ValidationError, match="names no profile"):
        CorpusTakeState.model_validate_json(canonical_json_bytes(imported))
    beyond = {**document, "capture_profile": {**document["capture_profile"], "primary_channel": 7}}
    with pytest.raises(ValidationError, match="capture profile's format"):
        CorpusTakeState.model_validate_json(canonical_json_bytes(beyond))


def test_the_vault_reads_a_raw_v2_take(layout: AssetLayout, catalog: Catalog, work: Path) -> None:
    take = make_profile_take(work / "take", catalog=catalog)
    incoming(layout, take, work / "take")
    publish_pending(layout, min_free_bytes=0)
    items = {
        item.path: item
        for item in RawPackageSource().items(layout)
        if isinstance(item, ReplicationItem)
    }
    raw = f"raw/{take.manifest_sha256}"
    audio = items[f"{raw}/{AUDIO_NAME}"]
    assert (audio.expected_sha256, audio.expected_size) == (
        take.manifest.audio.sha256,
        take.manifest.audio.size_bytes,
    )
    evidence = items[f"{raw}/{CONTINUITY_NAME}"]
    assert (evidence.expected_sha256, evidence.expected_size) == (
        take.manifest.continuity.sha256,
        take.manifest.continuity.size_bytes,
    )
    assert set(items) == {
        f"{raw}/{name}" for name in (*TAKE_FILES, "intake-qc.json", "intake-receipt.json")
    }
