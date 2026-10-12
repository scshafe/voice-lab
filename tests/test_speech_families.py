"""Model families behind the speech path (speech/families.py, speech/f5.py, D-048).

A second family is a test double here: it binds a speaker-profile file and no reference
clip, has no notion of flow steps, renders at 22.05 kHz and cannot seed. The records it
would write are checked against the common fields only, which is the point of the design:
such a family needs a member in the ``ReleaseModel`` and ``RenderSettings`` unions and
nothing else.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from speech_fixtures import NOW, FakeBackend, Lab, build_lab

from cole_voice_lab.speech.backend import SpeechBackendError
from cole_voice_lab.speech.config import F5SpeechRuntime
from cole_voice_lab.speech.contracts import (
    F5RenderSettings,
    FileDigest,
    RenderFingerprint,
    SpeechRenderRecord,
)
from cole_voice_lab.speech.f5 import F5_TTS, F5ResidentBackend
from cole_voice_lab.speech.families import (
    ReleaseBackend,
    UnknownModelFamily,
    family_for,
    family_ids,
    run_family,
)
from cole_voice_lab.speech.release import VerifiedRelease, VoiceRelease, current_release


@pytest.fixture
def lab(tmp_path: Path) -> Lab:
    return build_lab(tmp_path)


def test_f5_tts_is_the_one_registered_family(lab: Lab) -> None:
    assert family_ids() == ("f5-tts",)
    assert family_for("f5-tts") is F5_TTS
    assert (F5_TTS.native_sample_rate_hz, F5_TTS.seeded) == (24_000, True)
    with pytest.raises(UnknownModelFamily):
        family_for("piper")
    assert run_family(lab.layout, "finetune-a") == "f5-tts"
    release, _ = current_release(lab.layout)
    assert release.model_family == "f5-tts" and release.model.family == "f5-tts"
    assert [item.role for item in release.files] == ["checkpoint", "reference"]


def test_the_f5_backend_takes_the_configured_runtime_only_when_it_is_f5s(lab: Lab) -> None:
    backend = F5_TTS.backend(lab.config)
    assert isinstance(backend, F5ResidentBackend) and backend.runtime is None
    runtime = F5SpeechRuntime(runtime_root="/nowhere/f5")
    configured = lab.config.model_copy(update={"runtime": runtime})
    assert configured.runtime_for("f5-tts") == runtime and configured.runtime_for("other") is None
    built = F5_TTS.backend(configured)
    assert isinstance(built, F5ResidentBackend) and built.runtime == runtime


class StubBackend(FakeBackend):
    """The fake backend, answering every release with one fixed fingerprint."""

    def prepare(self, release: VerifiedRelease) -> RenderFingerprint:
        self.prepared.append(release)
        self.loaded = True
        return fingerprint()


class OtherFamily:
    """A family double: whatever it is asked for, its own stub backend."""

    family_id = "other"
    native_sample_rate_hz = 22_050
    seeded = False

    def __init__(self) -> None:
        self.backends: list[StubBackend] = []

    def backend(self, config: Any) -> StubBackend:
        made = StubBackend()
        self.backends.append(made)
        return made


def test_the_release_backend_hands_each_release_to_its_familys_backend(lab: Lab) -> None:
    release, digest = current_release(lab.layout)
    verified = VerifiedRelease(release=release, digest=digest)
    f5, other = OtherFamily(), OtherFamily()
    released = {"f5-tts": f5, "other": other}

    def families(family_id: str) -> Any:
        if family_id not in released:
            raise UnknownModelFamily(family_id)
        return released[family_id]

    backend = ReleaseBackend(lab.config, families=families)
    assert not backend.loaded
    with pytest.raises(SpeechBackendError) as unprepared:
        backend.render_segment("x", seed=1, output=lab.root / "x.wav", deadline_seconds=1)
    assert unprepared.value.code == "render_failed"
    backend.prepare(verified)
    backend.prepare(verified)
    assert len(f5.backends) == 1 and backend.loaded
    backend.render_segment("Hello.", seed=1, output=lab.root / "one.wav", deadline_seconds=1)
    assert f5.backends[0].calls == [("Hello.", 1)]
    # A release of another family unloads the first family's model before loading its own.
    other_release = VerifiedRelease(
        release=release.model_copy(update={"model_family": "other"}), digest=digest
    )
    backend.prepare(other_release)
    assert f5.backends[0].unloads == 1 and len(other.backends) == 1
    unknown = VerifiedRelease(
        release=release.model_copy(update={"model_family": "unknown"}), digest=digest
    )
    with pytest.raises(SpeechBackendError) as refused:
        backend.prepare(unknown)
    assert refused.value.code == "runtime_unavailable"
    backend.unload()
    assert not backend.loaded


def test_a_release_of_an_unknown_family_is_never_verified(lab: Lab) -> None:
    from cole_voice_lab.finetune import FineTuneDigestCache
    from cole_voice_lab.speech.release import VoiceReleaseError, verify_release

    release, digest = current_release(lab.layout)
    stranger = release.model_copy(update={"model_family": "unknown"})
    with pytest.raises(VoiceReleaseError) as refused:
        verify_release(lab.layout, stranger, digest, digests=FineTuneDigestCache())
    assert refused.value.code == "release_changed"


def test_the_common_fields_hold_a_family_without_a_reference_or_flow_steps(lab: Lab) -> None:
    release, _ = current_release(lab.layout)
    data = release.model_dump(mode="json")

    def validate(value: dict[str, Any]) -> VoiceRelease:
        return VoiceRelease.model_validate_json(json.dumps(value))

    # Files are by role: a speaker profile instead of a reference clip fits the common part.
    data["files"] = [
        data["files"][0],
        {"role": "speaker-profile", "path": "derived/x/speaker.npy", "sha256": "c" * 64},
    ]
    assert validate(data).file("speaker-profile").sha256 == "c" * 64
    with pytest.raises(KeyError):
        validate(data).file("reference")
    with pytest.raises(ValidationError, match="each file role once"):
        validate({**data, "files": [data["files"][0]] * 2})
    with pytest.raises(ValidationError, match="its family's"):
        validate({**data, "model_family": "other"})
    # Until such a family is registered its section is refused, not guessed.
    with pytest.raises(ValidationError):
        validate({**data, "model": {"family": "other"}})


def fingerprint(**changes: Any) -> RenderFingerprint:
    fields: dict[str, Any] = {
        "model_family": "f5-tts",
        "backend": "fake",
        "files": (FileDigest(role="checkpoint", sha256="a" * 64),),
        "runtime": {},
        "device": "cpu",
        "native_sample_rate_hz": 22_050,
        "seeded": False,
        "settings": F5RenderSettings(nfe_steps=16),
        "voice_lab_version": "test",
        **changes,
    }
    return RenderFingerprint.model_validate(fields)


def test_a_fingerprint_and_its_ledger_entry_are_of_one_family() -> None:
    assert fingerprint().native_sample_rate_hz == 22_050
    with pytest.raises(ValidationError, match="its family's"):
        fingerprint(model_family="other")
    record = {
        "job_id": "sp-" + "a" * 24,
        "caller_id": "agent-server",
        "text_hmac_sha256": "b" * 64,
        "text_characters": 3,
        "release": {"release_id": "vr-0001", "release_sha256": "c" * 64},
        "model_family": "other",
        "consent_id": "cole-self-v2",
        "consent_sha256": "d" * 64,
        "outcome": "cancelled",
        "failure": None,
        "fingerprint": fingerprint(),
        "seed": 1,
        "segment_count": 1,
        "segment_sha256": (),
        "output": None,
        "created_at": NOW,
        "started_at": NOW,
        "finished_at": NOW,
        "queue_ms": 0,
        "render_ms": 0,
        "audio_ms": None,
    }
    with pytest.raises(ValidationError, match="release's family"):
        SpeechRenderRecord.model_validate(record)
    assert SpeechRenderRecord.model_validate({**record, "model_family": "f5-tts"}).seed == 1
