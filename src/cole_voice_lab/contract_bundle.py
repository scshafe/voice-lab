"""The corpus client's contract bundle (D-056 step 5, docs/design/HOUSE-PANEL.md §4).

@canonical contract_bundle -- ``contracts/corpus-client.v1/``: what a capture client other than
voice-lab-desktop (the house panel) needs to talk to the PC byte for byte. It is shaped like
lexinet's ``study.v1``, which the panel already pins by commit, ``manifest.json``'s SHA-256 and
the bundle digest (``bundle_digest``):

- ``manifest.json``: the contract's id and versions, its required semantics, the examples
  with what each is expected to be (an invalid one with where and why it is refused), and the
  SHA-256 and size of every other file of the bundle;
- ``openapi.json`` (3.1): the routes a corpus client's token reaches (``api.client_scope``),
  with their bodies and answers; its components are the contracts' JSON Schemas;
- ``schemas/``: those JSON Schemas one per file, as ``schema.py`` writes ``schemas/``;
- ``examples/`` and ``examples/invalid/``: the records with their exact bytes (canonical JSON
  and ``\\n``), among them a golden take package whose ``original.wav`` is given by an integer
  recipe (``WavRecipe``: the repository holds no audio) and the canonical-JSON vectors;
- ``README.md`` and ``PROTOCOL.md``, written by hand, which ``manifest.json`` binds by digest.

Everything but the two documents is generated here from the code's own contracts, and
``check`` (``scripts/verify``) fails when the bundle on disk differs from what the code would
write now, as ``schema.py --check`` does for ``schemas/``. ``scripts/guard-private-assets``
takes a bundle file's records as public only when ``manifest.json`` lists it with its exact
digest, which ``check`` proves is the generator's.

The examples are synthetic: no recording of Cole's, an example serial and firmware hash
(the USB ids are the Flex Linear-4's own, ``capture_profiles``), and the fixture catalog is
the built-in ``pilot-v2`` suite as version 1 of ``cole-corpus`` (what ``voice-lab catalog
init --subject-id cole-self --from-builtin pilot-v2`` publishes), so the golden take is
admissible on a server seeded that way.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, Field, JsonValue
from pydantic.json_schema import models_json_schema

from .alsa_evidence import (
    ALSA_FORMAT_ENCODINGS,
    ALSA_FORMAT_WIDTHS,
    MAX_BUFFER_FRAMES,
    MAX_PERIOD_FRAMES,
    MAX_PERIODS,
    MAX_TAKE_SECONDS,
    MAX_TOLERANCE_NS,
    MIN_PERIOD_FRAMES,
    WAV_HEADER_BYTES,
    AlsaContinuityEvidence,
    AlsaFormat,
    AlsaWav,
    period_deviations_ns,
)
from .alsa_evidence import MAX_EVIDENCE_BYTES as MAX_ALSA_EVIDENCE_BYTES
from .api.client_scope import CLIENT_PROFILE
from .api.contracts import (
    ApiMeta,
    ApiRefusal,
    ChunkAnswer,
    IntakeSessionRequest,
    IntakeSessionStatus,
)
from .api.corpus_contracts import (
    CatalogView,
    CorpusLibrary,
    CorpusRequestStatus,
    TakeDecisionRequest,
    TakeHistory,
)
from .audio import inspect_pcm_bytes
from .canonical import canonical_json_bytes, canonical_sha256
from .capture import ReadyMarker
from .capture_profiles import SEEED_USB_VENDOR_ID, XVF3800_LINEAR4_USB_PRODUCT_ID
from .corpus_takes import (
    MAX_PROFILE_TAKE_AUDIO_BYTES,
    CorpusTakeManifestV2,
    take_id_for,
    take_qc_report,
)
from .home_host import HomeHostApproval, HomeHostBinding, HomeHostLedgerEntry
from .intake import MAX_CONTROL_FILE_BYTES, IntakeReceipt
from .models import COLE_SUBJECT, PROMPT_SUITE_V2, ContractModel, PromptSuite, QcReport, Sha256
from .prompt_catalogs import load_builtin_prompt_suite
from .recording_contracts import CatalogReference, CatalogSnapshot, catalog_snapshot_path

CONTRACT_ID: Final = "corpus-client.v1"
BUNDLE_DIRECTORY: Final = Path("contracts") / CONTRACT_ID
MANIFEST: Final = "manifest.json"
OPENAPI: Final = "openapi.json"
#: The two documents written by hand; ``manifest.json`` binds them by digest.
AUTHORED: Final = ("PROTOCOL.md", "README.md")
CLIENT_AUDIENCE: Final = "voice-lab-panel"
#: The chunk size the server takes at most (``api.server.MAX_CHUNK_BYTES``), and the range
#: PROTOCOL.md recommends.
MAX_CHUNK_BYTES: Final = 64 * 1024 * 1024
REQUIRED_SEMANTICS: Final = (
    "device_code_sign_in_without_resource",
    "single_flight_refresh_rotation",
    "standing_approval_and_ledger_entry_before_the_first_byte",
    "corpus_take_v2_in_five_files",
    "canonical_json_byte_for_byte",
    "alsa_continuity_rule_in_integer_nanoseconds",
    "profile_scoped_library_and_history",
    "resumable_chunked_upload",
    "finalize_takes_only_the_clients_profile",
    "decisions_with_stable_request_ids_and_expected_revisions",
)
#: The contracts the bundle publishes, by component name.
COMPONENTS: Final[dict[str, type[BaseModel]]] = {
    "AlsaContinuityEvidence": AlsaContinuityEvidence,
    "ApiMeta": ApiMeta,
    "ApiRefusal": ApiRefusal,
    "CatalogView": CatalogView,
    "ChunkAnswer": ChunkAnswer,
    "CorpusLibrary": CorpusLibrary,
    "CorpusRequestStatus": CorpusRequestStatus,
    "CorpusTakeManifestV2": CorpusTakeManifestV2,
    "HomeHostApproval": HomeHostApproval,
    "HomeHostBinding": HomeHostBinding,
    "HomeHostLedgerEntry": HomeHostLedgerEntry,
    "IntakeReceipt": IntakeReceipt,
    "IntakeSessionRequest": IntakeSessionRequest,
    "IntakeSessionStatus": IntakeSessionStatus,
    "QcReport": QcReport,
    "ReadyMarker": ReadyMarker,
    "TakeDecisionRequest": TakeDecisionRequest,
    "TakeHistory": TakeHistory,
}

# -- The golden take ---------------------------------------------------------------------------

_T0: Final = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
_FIRST_TIMESTAMP_NS: Final = 9_876_543_210_000
_JITTER_NS: Final = (0, 120_000, -80_000, 40_000)
_PERIOD_FRAMES: Final = 1_024
_PERIODS: Final = 4
_SETTINGS: Final[dict[str, tuple[int | float, ...]]] = {
    "AEC_HPFONOFF": (1,),
    "PP_AGCGAIN": (31.6,),
    "PP_AGCONOFF": (1,),
    "PP_ECHOONOFF": (1,),
    "PP_LIMITONOFF": (0,),
    "PP_MIN_NN": (0.51,),
    "PP_MIN_NS": (0.15,),
    "PP_NLATTENONOFF": (1,),
    "SHF_BYPASS": (0,),
}
_PROCESSED: Final = ("aec", "agc", "noise-suppression", "echo-suppression", "high-pass")


class WavRecipe(ContractModel):
    """An ``original.wav`` given by integers, so the repository holds no audio and a client in
    any language makes the same bytes.

    The file is the plain 44-byte PCM header (RIFF, ``WAVE_FORMAT_PCM``: ``wav_header``), then
    ``frames`` frames of ``channels`` interleaved samples, frame after frame. The sample of
    channel ``c`` (from 0) in frame ``f`` (from 0) is ``((f * (c + 3) + offset) % modulus -
    center) * (c + 1) * scale``, little-endian and signed, in the width ``format`` gives. Every
    term is a non-negative integer before the subtraction, so ``%`` means the same in C++ and
    Python."""

    recipe: Literal["corpus-client.v1/interleaved-sawtooth"] = (
        "corpus-client.v1/interleaved-sawtooth"
    )
    format: Literal["S16_LE", "S24_3LE", "S32_LE"]
    rate: int = Field(ge=8_000, le=384_000)
    channels: int = Field(ge=1, le=32)
    frames: int = Field(ge=1)
    offset: int = Field(ge=0)
    modulus: int = Field(ge=1)
    center: int = Field(ge=0)
    scale: int = Field(ge=1)
    #: The resulting file.
    sha256: Sha256
    size_bytes: int = Field(gt=WAV_HEADER_BYTES)


class CanonicalJsonVector(ContractModel):
    """One value and the exact text of its canonical JSON (without the file's ``\\n``)."""

    value: JsonValue
    canonical: str


class CanonicalJsonVectors(ContractModel):
    """``examples/canonical-json-vectors.json``: what the canonical form makes of each value."""

    vectors: tuple[CanonicalJsonVector, ...] = Field(min_length=1)


def wav_header(*, channels: int, rate: int, width: int, data_bytes: int) -> bytes:
    """The 44-byte PCM WAV header (RIFF, ``fmt `` of 16 bytes, then ``data``)."""
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_bytes,
        b"WAVE",
        b"fmt ",
        16,
        1,
        channels,
        rate,
        rate * channels * width,
        channels * width,
        width * 8,
        b"data",
        data_bytes,
    )


def recipe_wav(
    *,
    alsa_format: str,
    rate: int,
    channels: int,
    frames: int,
    offset: int,
    modulus: int,
    center: int,
    scale: int,
) -> bytes:
    """The WAV a ``WavRecipe`` describes."""
    width = ALSA_FORMAT_WIDTHS[alsa_format]
    samples = bytearray()
    for frame in range(frames):
        for channel in range(channels):
            value = ((frame * (channel + 3) + offset) % modulus - center) * (channel + 1) * scale
            samples += value.to_bytes(width, "little", signed=True)
    header = wav_header(channels=channels, rate=rate, width=width, data_bytes=len(samples))
    return header + bytes(samples)


def fixture_catalog() -> tuple[CatalogReference, PromptSuite]:
    """The catalog the examples are recorded against: the built-in ``pilot-v2`` suite as
    version 1 of ``cole-corpus``, as ``voice-lab catalog init --subject-id cole-self
    --from-builtin pilot-v2`` publishes it."""
    builtin = load_builtin_prompt_suite("pilot-v2.json")
    suite = PromptSuite(
        schema_version=PROMPT_SUITE_V2,
        suite_id="cole-corpus",
        purpose=builtin.purpose,
        prompts=builtin.prompts,
    )
    digest = canonical_sha256(suite)
    snapshot = canonical_json_bytes(CatalogSnapshot.embedded(suite)) + b"\n"
    reference = CatalogReference(
        catalog_id="cole-corpus",
        catalog_version=1,
        catalog_sha256=digest,
        snapshot_sha256=hashlib.sha256(snapshot).hexdigest(),
        suite_id=suite.suite_id,
        purpose=suite.purpose,
        snapshot_path=catalog_snapshot_path(digest),
    )
    return reference, suite


def _json(value: object) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def _profile() -> dict[str, Any]:
    return {
        "profile": CLIENT_PROFILE,
        "device": {
            "model": "reSpeaker Flex XVF3800 Linear-4",
            "usb_vendor_id": SEEED_USB_VENDOR_ID,
            "usb_product_id": XVF3800_LINEAR4_USB_PRODUCT_ID,
            "serial": "EXAMPLE-0001",
            "firmware": {
                "version": [1, 0, 4],
                "build_message": "reSpeaker_Flex v1.0.4",
                "build_repo_hash": "0123abcd",
            },
        },
        "room": "bathroom",
        "channels": [
            {"source": "processed", "mic": None, "routing": [7, 0], "processing": _PROCESSED},
            {"source": "processed", "mic": None, "routing": [7, 1], "processing": _PROCESSED},
            *(
                {"source": "mic", "mic": mic, "routing": [1, mic - 1], "processing": []}
                for mic in range(1, 5)
            ),
        ],
        "primary_channel": 3,
        "settings": _SETTINGS,
    }


def golden_take() -> dict[str, bytes]:
    """The golden take package (``examples/take/``): its four records and its WAV recipe."""
    alsa_format: AlsaFormat = "S16_LE"
    rate, channels = 16_000, 6
    frames = _PERIODS * _PERIOD_FRAMES
    offset, modulus, center, scale = 0, 2001, 1000, 4
    wav = recipe_wav(
        alsa_format=alsa_format,
        rate=rate,
        channels=channels,
        frames=frames,
        offset=offset,
        modulus=modulus,
        center=center,
        scale=scale,
    )
    audio_sha256 = hashlib.sha256(wav).hexdigest()
    recipe = WavRecipe(
        format=alsa_format,
        rate=rate,
        channels=channels,
        frames=frames,
        offset=offset,
        modulus=modulus,
        center=center,
        scale=scale,
        sha256=audio_sha256,
        size_bytes=len(wav),
    )
    periods = [
        (
            (index + 1) * _PERIOD_FRAMES + 256,
            _FIRST_TIMESTAMP_NS + index * _PERIOD_FRAMES * 62_500 + _JITTER_NS[index],
        )
        for index in range(_PERIODS)
    ]
    deviations = period_deviations_ns(periods, rate)
    evidence = AlsaContinuityEvidence(
        simulated=False,
        pcm="hw:CARD=Array,DEV=0",
        access="rw_interleaved",
        format=alsa_format,
        rate=rate,
        channels=channels,
        period_frames=_PERIOD_FRAMES,
        buffer_frames=4 * _PERIOD_FRAMES,
        tstamp_type="monotonic_raw",
        xruns=0,
        periods=tuple(periods),
        tolerance_ns=1_000_000,
        max_abs_deviation_ns=max(abs(value) for value in deviations),
        frames=frames,
        wav=AlsaWav(sha256=audio_sha256, size_bytes=len(wav)),
        started_at=_T0,
        stop_reason="requested",
        success=True,
        error=None,
    )
    evidence_bytes = _json(evidence)
    reference, suite = fixture_catalog()
    prompt = next(item for item in suite.prompts if item.spoken)
    manifest = CorpusTakeManifestV2.model_validate_json(
        canonical_json_bytes(
            {
                "schema_version": "cole-voice-lab.corpus-take/v2",
                "take_id": take_id_for(audio_sha256),
                "subject_id": COLE_SUBJECT,
                "prompt": prompt.model_dump(mode="json"),
                "prompt_sha256": canonical_sha256(prompt),
                "catalog": reference.model_dump(mode="json"),
                "requested_at": "2026-10-19T12:00:00Z",
                "capture_host": "house-panel",
                "client": {
                    "name": "embedded-house-panel",
                    "version": "0.0.0-example",
                    "platform": "Linux aarch64",
                },
                "capture_profile": _profile(),
                "audio": {
                    "path": "original.wav",
                    "sha256": audio_sha256,
                    "size_bytes": len(wav),
                    "audio": {
                        "sample_rate_hz": rate,
                        "channels": channels,
                        "sample_width_bytes": ALSA_FORMAT_WIDTHS[alsa_format],
                        "encoding": ALSA_FORMAT_ENCODINGS[alsa_format],
                        "frames": frames,
                        "duration_seconds": frames / rate,
                    },
                },
                "continuity": {
                    "path": "continuity.json",
                    "sha256": hashlib.sha256(evidence_bytes).hexdigest(),
                    "size_bytes": len(evidence_bytes),
                    "pcm": evidence.pcm,
                    "simulated": evidence.simulated,
                    "started_at": "2026-10-19T12:00:00Z",
                    "stop_reason": evidence.stop_reason,
                    "tolerance_ns": evidence.tolerance_ns,
                    "max_abs_deviation_ns": evidence.max_abs_deviation_ns,
                },
            }
        )
    )
    manifest_bytes = _json(manifest)
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    inspection = inspect_pcm_bytes(wav, channel=manifest.capture_profile.primary_channel)
    report = take_qc_report(
        manifest.take_id, prompt, manifest_sha256, inspection, _T0 + timedelta(seconds=4)
    )
    report_bytes = _json(report)
    ready = ReadyMarker(
        session_id=manifest.take_id,
        manifest_sha256=manifest_sha256,
        qc_report_sha256=hashlib.sha256(report_bytes).hexdigest(),
        finalized_at=_T0 + timedelta(seconds=5),
    )
    return {
        "READY": _json(ready),
        "continuity.json": evidence_bytes,
        "manifest.json": manifest_bytes,
        "original.wav.recipe.json": _json(recipe),
        "qc.json": report_bytes,
    }


def golden_wav() -> bytes:
    """The golden take's ``original.wav``, made from its recipe."""
    recipe = WavRecipe.model_validate_json(golden_take()["original.wav.recipe.json"])
    return recipe_wav(
        alsa_format=recipe.format,
        rate=recipe.rate,
        channels=recipe.channels,
        frames=recipe.frames,
        offset=recipe.offset,
        modulus=recipe.modulus,
        center=recipe.center,
        scale=recipe.scale,
    )


# -- The other examples ------------------------------------------------------------------------


def _session_request(take: Mapping[str, bytes], wav: bytes) -> IntakeSessionRequest:
    files = {name: data for name, data in take.items() if name != "original.wav.recipe.json"}
    files["original.wav"] = wav
    return IntakeSessionRequest.model_validate_json(
        canonical_json_bytes(
            {
                "session_id": ReadyMarker.model_validate_json(take["READY"]).session_id,
                "manifest_sha256": hashlib.sha256(take["manifest.json"]).hexdigest(),
                "files": [
                    {
                        "path": name,
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "size_bytes": len(data),
                    }
                    for name, data in sorted(files.items())
                ],
            }
        )
    )


def _home_host_records(session_id: str, manifest_sha256: str) -> dict[str, bytes]:
    approval = HomeHostApproval.model_validate(
        {
            "approval_id": "pc-home-host",
            "approved": True,
            "endpoint": "voice-lab.example.ts.net",
            "tailnet_suffix": "example.ts.net",
            "node_stable_id": "nEXAMPLE11CNTRL",
            "classes": ("capture",),
            "approved_at": _T0 - timedelta(days=1),
            "expires_at": _T0 + timedelta(days=300),
            "approved_by": "Cole",
            "statement": "Uploads of my own takes from the house panel to my own PC.",
        }
    )
    approval_bytes = _json(approval)
    approval_sha256 = hashlib.sha256(approval_bytes).hexdigest()
    binding = HomeHostBinding(
        approval_id=approval.approval_id,
        approval_sha256=approval_sha256,
        bound_at=_T0 - timedelta(days=1) + timedelta(minutes=1),
    )
    entry = HomeHostLedgerEntry(
        sequence=1,
        previous_sha256=None,
        event="upload-started",
        at=_T0 + timedelta(seconds=6),
        approval_id=approval.approval_id,
        approval_sha256=approval_sha256,
        endpoint=approval.endpoint,
        node_stable_id=approval.node_stable_id,
        session_id=session_id,
        manifest_sha256=manifest_sha256,
    )
    return {
        "examples/home-host-approval.json": approval_bytes,
        "examples/home-host-binding.json": _json(binding),
        "examples/home-host-ledger-upload-started.json": _json(entry),
    }


#: Canonical JSON as the panel writes it: values, and the exact text of each (floats in
#: Python's repr, keys in code-point order, UTF-8 unescaped, compact separators).
_VECTORS: Final[tuple[object, ...]] = (
    {"b": 1, "a": [True, False, None], "B": "upper before lower"},
    {"é": "after z", "z": "before é", "→": "arrows sort by code point"},
    {"float": 31.6, "small": 1e-07, "large": 1e22, "negative": -0.5, "zero": 0.0},
    {"integers": [0, -1, 9_007_199_254_740_993, 9_223_372_036_854_775_807]},
    {"repr": [0.1, 0.15, 0.51, 1.5e-05, 123456789012345680.0, 2.5, 100.0]},
    {"nested": {"z": {"b": [1, {"d": 4, "c": 3}], "a": ""}}},
    {"escaped": 'quote " backslash \\ newline \n tab \t control \u0001'},
    {"short escapes": "\b\f\n\r\t", "long escapes": "\u0000\u001f", "raw": "\u007f é →"},
)


def vectors() -> bytes:
    """``examples/canonical-json-vectors.json``: each value with its canonical text."""
    return _json(
        {
            "vectors": [
                {"value": value, "canonical": canonical_json_bytes(value).decode("utf-8")}
                for value in _VECTORS
            ]
        }
    )


def _invalid(record: Mapping[str, Any], **changes: Any) -> bytes:
    """``record`` with ``changes`` merged one level deep: an invalid example's bytes."""
    document = dict(record)
    for key, value in changes.items():
        document[key] = {**document[key], **value} if isinstance(value, dict) else value
    return _json(document)


def examples() -> tuple[dict[str, bytes], list[dict[str, Any]]]:
    """Every example file, by path, and its entries for ``manifest.json``'s ``examples``."""
    take = golden_take()
    wav = golden_wav()
    request = _session_request(take, wav)
    files: dict[str, bytes] = {f"examples/take/{name}": data for name, data in take.items()}
    files["examples/intake-session-request.json"] = _json(request)
    files.update(_home_host_records(request.session_id, request.manifest_sha256))
    files["examples/take-decision-request.json"] = _json(
        TakeDecisionRequest(
            request_id="req-" + "0123456789abcdef" * 2,
            decision="kept",
            expected_revision=0,
        )
    )
    files["examples/refusal-client-scope.json"] = _json(
        ApiRefusal(
            error="client_scope",
            detail="this client's token reaches only the house panel's routes "
            "(docs/HOME_HOST_INTAKE.md)",
        )
    )
    files["examples/refusal-take-profile-not-allowed.json"] = _json(
        ApiRefusal(
            error="take_profile_not_allowed",
            detail=f"this client sends only takes of capture profile {CLIENT_PROFILE} "
            "(cole-voice-lab.corpus-take/v2)",
        )
    )
    files["examples/canonical-json-vectors.json"] = vectors()

    manifest = json.loads(take["manifest.json"])
    evidence = json.loads(take["continuity.json"])
    audio = manifest["audio"]["audio"]
    files["examples/invalid/manifest-48-khz.json"] = _invalid(
        manifest,
        audio={
            **manifest["audio"],
            "audio": {
                **audio,
                "sample_rate_hz": 48_000,
                "duration_seconds": audio["frames"] / 48_000,
            },
        },
    )
    device = manifest["capture_profile"]["device"]
    files["examples/invalid/manifest-another-vendor.json"] = _invalid(
        manifest,
        capture_profile={
            **manifest["capture_profile"],
            "device": {**device, "usb_vendor_id": 0x20B1},
        },
    )
    files["examples/invalid/manifest-another-product.json"] = _invalid(
        manifest,
        capture_profile={
            **manifest["capture_profile"],
            "device": {**device, "usb_product_id": 0x0018},
        },
    )
    shifted = [
        [position, timestamp + (_PERIOD_FRAMES * 62_500 if index >= 2 else 0)]
        for index, (position, timestamp) in enumerate(evidence["periods"])
    ]
    pairs = [(int(position), int(timestamp)) for position, timestamp in shifted]
    honest = max(abs(value) for value in period_deviations_ns(pairs, evidence["rate"]))
    files["examples/invalid/continuity-lost-period.json"] = _invalid(
        evidence, periods=shifted, max_abs_deviation_ns=honest
    )
    files["examples/invalid/take-decision-request-unknown-field.json"] = _invalid(
        json.loads(files["examples/take-decision-request.json"]), note="no such field"
    )

    publisher = "the publisher refuses the package: the session's state is refused"
    entries: list[dict[str, Any]] = [
        {"path": "examples/take/manifest.json", "schema": "CorpusTakeManifestV2"},
        {"path": "examples/take/continuity.json", "schema": "AlsaContinuityEvidence"},
        {"path": "examples/take/qc.json", "schema": "QcReport"},
        {"path": "examples/take/READY", "schema": "ReadyMarker"},
        {"path": "examples/take/original.wav.recipe.json", "schema": "WavRecipe"},
        {"path": "examples/intake-session-request.json", "schema": "IntakeSessionRequest"},
        {"path": "examples/home-host-approval.json", "schema": "HomeHostApproval"},
        {"path": "examples/home-host-binding.json", "schema": "HomeHostBinding"},
        {
            "path": "examples/home-host-ledger-upload-started.json",
            "schema": "HomeHostLedgerEntry",
        },
        {"path": "examples/take-decision-request.json", "schema": "TakeDecisionRequest"},
        {"path": "examples/refusal-client-scope.json", "schema": "ApiRefusal"},
        {"path": "examples/refusal-take-profile-not-allowed.json", "schema": "ApiRefusal"},
        {"path": "examples/canonical-json-vectors.json", "schema": "CanonicalJsonVectors"},
    ]
    for entry in entries:
        entry["expected"] = "valid"
    entries += [
        {
            "path": "examples/invalid/manifest-48-khz.json",
            "schema": "CorpusTakeManifestV2",
            "expected": "invalid",
            "refusal": {"where": publisher, "message_contains": "takes 16 kHz audio"},
        },
        {
            "path": "examples/invalid/manifest-another-vendor.json",
            "schema": "CorpusTakeManifestV2",
            "expected": "invalid",
            "refusal": {"where": publisher, "message_contains": "Seeed USB device"},
        },
        {
            "path": "examples/invalid/manifest-another-product.json",
            "schema": "CorpusTakeManifestV2",
            "expected": "invalid",
            "refusal": {"where": publisher, "message_contains": "product 0x0022"},
        },
        {
            "path": "examples/invalid/continuity-lost-period.json",
            "schema": "AlsaContinuityEvidence",
            "expected": "invalid",
            "refusal": {"where": publisher, "message_contains": "beyond the tolerance"},
        },
        {
            "path": "examples/invalid/take-decision-request-unknown-field.json",
            "schema": "TakeDecisionRequest",
            "expected": "invalid",
            "refusal": {"where": "the API answers 422", "code": "invalid_request"},
        },
    ]
    return files, entries


# -- The OpenAPI document ----------------------------------------------------------------------


def _ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{name}"}


def _json_content(name: str) -> dict[str, Any]:
    return {"application/json": {"schema": _ref(name)}}


def _answers(success: Mapping[str, tuple[str, str | None]]) -> dict[str, Any]:
    answers: dict[str, Any] = {}
    for status, (description, component) in success.items():
        answers[status] = {"description": description}
        if component is not None:
            answers[status]["content"] = _json_content(component)
    answers["default"] = {
        "description": "a refusal: its code (ApiErrorCode) and a message; PROTOCOL.md says "
        "which to retry",
        "content": _json_content("ApiRefusal"),
    }
    return answers


def _parameter(name: str, where: str, description: str, **schema: Any) -> dict[str, Any]:
    return {
        "name": name,
        "in": where,
        "required": True,
        "description": description,
        "schema": {"type": "string", **schema},
    }


_PROFILE: Final = _parameter(
    "profile", "query", "the client's capture profile: its view only", enum=[CLIENT_PROFILE]
)
_ETAG: Final = {"304": ("If-None-Match names the current ETag", None)}


def _operation_id(method: str, path: str) -> str:
    """``get_api_v1_corpus_library`` for ``GET /api/v1/corpus/library``."""
    name = path.strip("/").replace("/", "_").replace("{", "").replace("}", "")
    return f"{method}_{name}"


def openapi() -> dict[str, Any]:
    """The OpenAPI 3.1 document of the routes a corpus client's token reaches."""
    _, top = models_json_schema(
        [(model, "validation") for model in _published().values()],
        ref_template="#/components/schemas/{model}",
    )
    schemas = dict(sorted(top["$defs"].items()))
    session = _parameter("session_id", "path", "the take's id (take-<SHA-256 of original.wav>)")
    read = {"get", "head"}
    paths: dict[str, Any] = {
        "/api/v1/meta": {
            "get": {"answers": {"200": ("the server's build and API versions", "ApiMeta")}}
        },
        "/api/v1/catalog": dict.fromkeys(
            read,
            {"answers": {"200": ("the active catalog version (ETag)", "CatalogView"), **_ETAG}},
        ),
        "/api/v1/corpus/library": dict.fromkeys(
            read,
            {
                "parameters": [_PROFILE],
                "answers": {
                    "200": ("the client's profile's view (ETag)", "CorpusLibrary"),
                    **_ETAG,
                },
            },
        ),
        "/api/v1/corpus/prompts/{prompt_id}/takes": dict.fromkeys(
            read,
            {
                "parameters": [
                    _parameter("prompt_id", "path", "a prompt of the active version"),
                    _PROFILE,
                ],
                "answers": {
                    "200": ("the prompt's takes of the profile (ETag)", "TakeHistory"),
                    **_ETAG,
                },
            },
        ),
        "/api/v1/intake/sessions": {
            "post": {
                "body": "IntakeSessionRequest",
                "answers": {
                    "201": ("the session, opened", "IntakeSessionStatus"),
                    "200": (
                        "the session, already open: resume from its files",
                        "IntakeSessionStatus",
                    ),
                },
            }
        },
        "/api/v1/intake/sessions/{session_id}": {
            "get": {
                "parameters": [session],
                "answers": {
                    "200": ("the session's state and each file's progress", "IntakeSessionStatus")
                },
            }
        },
        "/api/v1/intake/sessions/{session_id}/files/{path}": {
            "put": {
                "parameters": [
                    session,
                    _parameter("path", "path", "the declared path, percent-encoded as one segment"),
                    _parameter(
                        "Content-Range",
                        "header",
                        "bytes S-E/T, S the bytes the server holds (bytes */0 for an empty file)",
                    ),
                ],
                "octets": True,
                "answers": {
                    "200": ("the file is complete and hashes to its declaration", "ChunkAnswer"),
                    "202": ("the chunk is held: send the next range", "ChunkAnswer"),
                },
            }
        },
        "/api/v1/intake/sessions/{session_id}/finalize": {
            "post": {
                "parameters": [session],
                "answers": {
                    "200": (
                        "every file re-hashed and the package handed to the publisher; "
                        "a client's only if it is a v2 take of its profile",
                        "IntakeSessionStatus",
                    )
                },
            }
        },
        "/api/v1/intake/sessions/{session_id}/receipt": {
            "get": {
                "parameters": [session],
                "answers": {"200": ("the intake receipt, once accepted", "IntakeReceipt")},
            }
        },
        "/api/v1/corpus/takes/{take_id}/decision": {
            "post": {
                "parameters": [_parameter("take_id", "path", "a published take of the profile")],
                "body": "TakeDecisionRequest",
                "answers": {"202": ("the request, queued (Location)", "CorpusRequestStatus")},
            }
        },
        "/api/v1/corpus/requests/{request_id}": {
            "get": {
                "parameters": [_parameter("request_id", "path", "req- and 32 hex digits")],
                "answers": {"200": ("the request's status and outcome", "CorpusRequestStatus")},
            }
        },
    }
    rendered: dict[str, Any] = {}
    for path, operations in paths.items():
        rendered[path] = {}
        for method in sorted(operations):
            spec = operations[method]
            operation: dict[str, Any] = {
                "operationId": _operation_id(method, path),
                "security": [{"bearer": []}],
                "responses": _answers(spec["answers"]),
            }
            if "parameters" in spec:
                operation["parameters"] = spec["parameters"]
            if "body" in spec:
                operation["requestBody"] = {
                    "required": True,
                    "content": _json_content(spec["body"]),
                }
            if spec.get("octets"):
                operation["requestBody"] = {
                    "required": True,
                    "content": {
                        "application/octet-stream": {
                            "schema": {
                                "type": "string",
                                "contentMediaType": "application/octet-stream",
                            }
                        }
                    },
                }
            rendered[path][method] = operation
    return {
        "openapi": "3.1.0",
        "jsonSchemaDialect": "https://json-schema.org/draft/2020-12/schema",
        "info": {
            "title": "voice-lab corpus client API",
            "version": CONTRACT_ID,
            "description": "The routes a corpus client's token reaches on the PC's /api/v1 "
            "(D-056): the house panel's.",
            "x-contract-id": CONTRACT_ID,
            "x-schema-version": 1,
        },
        "servers": [
            {
                "url": "https://{voice_lab_host}",
                "variables": {
                    "voice_lab_host": {
                        "default": "voice-lab.example.ts.net",
                        "description": "voice-lab.<tailnet>: the PC, on the tailnet only.",
                    }
                },
            }
        ],
        "x-voice-lab-contract": {
            "canonical": CONTRACT_ID,
            "client_audience": CLIENT_AUDIENCE,
            "capture_profile": CLIENT_PROFILE,
            "response_style": "raw_success_resource",
            "error_style": "ApiRefusal",
            "compatibility": {
                "optional_response_fields_may_be_added": True,
                "unknown_request_fields_rejected": True,
                "new_required_field_requires_new_contract": True,
                "new_required_semantic_requires_new_contract": True,
                "semantic_change_requires_new_contract": True,
            },
            "bounds": {
                "chunk_bytes_max": MAX_CHUNK_BYTES,
                "control_file_bytes_max": MAX_CONTROL_FILE_BYTES,
                "continuity_evidence_bytes_max": MAX_ALSA_EVIDENCE_BYTES,
                "take_audio_bytes_max": MAX_PROFILE_TAKE_AUDIO_BYTES,
                "take_seconds_max": MAX_TAKE_SECONDS,
                "period_frames_min": MIN_PERIOD_FRAMES,
                "period_frames_max": MAX_PERIOD_FRAMES,
                "buffer_frames_max": MAX_BUFFER_FRAMES,
                "periods_max": MAX_PERIODS,
                "tolerance_ns_max": MAX_TOLERANCE_NS,
                "wav_header_bytes": WAV_HEADER_BYTES,
            },
            "required_semantics": list(REQUIRED_SEMANTICS),
        },
        "paths": rendered,
        "components": {
            "securitySchemes": {
                "bearer": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "JWT",
                    "description": f"a Pocket ID access token whose aud names {CLIENT_AUDIENCE}",
                }
            },
            "schemas": schemas,
        },
    }


# -- The bundle --------------------------------------------------------------------------------


def _published() -> dict[str, type[BaseModel]]:
    """Every contract the bundle publishes a schema of: the API's and the bundle's own."""
    published: dict[str, type[BaseModel]] = {
        **COMPONENTS,
        "CanonicalJsonVectors": CanonicalJsonVectors,
        "WavRecipe": WavRecipe,
    }
    return dict(sorted(published.items()))


def _pretty(value: object) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _schema_file(model: type[BaseModel]) -> bytes:
    return (json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n").encode()


def generated(authored: Mapping[str, bytes]) -> dict[str, bytes]:
    """Every file of the bundle by relative path, ``authored`` (the two documents) included,
    with ``manifest.json`` binding all the others."""
    files: dict[str, bytes] = dict(authored)
    files[OPENAPI] = _pretty(openapi())
    for name, model in _published().items():
        files[f"schemas/{name}.schema.json"] = _schema_file(model)
    example_files, entries = examples()
    files.update(example_files)
    manifest = {
        "manifest_version": 1,
        "contract_id": CONTRACT_ID,
        "schema_version": 1,
        "openapi_path": OPENAPI,
        "required_semantics": list(REQUIRED_SEMANTICS),
        "examples": entries,
        "files": [
            {
                "path": path,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
            }
            for path, data in sorted(files.items())
        ],
    }
    files[MANIFEST] = _pretty(manifest)
    return dict(sorted(files.items()))


def bundle_digest(files: Mapping[str, bytes]) -> str:
    """The bundle's digest, as the panel pins it: each path in sorted order, hashed as
    ``path NUL contents NUL``."""
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.encode("utf-8") + b"\0" + files[path] + b"\0")
    return digest.hexdigest()


def _on_disk(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() or path.is_symlink()
    }


def _authored(root: Path) -> dict[str, bytes]:
    found: dict[str, bytes] = {}
    for name in AUTHORED:
        path = root / name
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError(f"the bundle's {name} is missing: {path}")
        found[name] = path.read_bytes()
    return found


def drift(root: Path) -> list[str]:
    """The bundle's paths that differ from what the code generates now (missing, changed or
    extra), empty when the bundle is current."""
    expected = generated(_authored(root))
    actual = _on_disk(root)
    return sorted(
        {path for path in expected.keys() | actual.keys() if expected.get(path) != actual.get(path)}
    )


def write(root: Path) -> None:
    """Write the generated files under ``root`` (the documents must already be there) and
    remove any file the bundle no longer has."""
    expected = generated(_authored(root))
    for path in _on_disk(root):
        if path not in expected:
            (root / path).unlink()
    for path, data in expected.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file() or target.read_bytes() != data:
            target.write_bytes(data)


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m cole_voice_lab.contract_bundle write|check|digest [--root DIR]``."""
    parser = argparse.ArgumentParser(prog="python -m cole_voice_lab.contract_bundle")
    parser.add_argument("command", choices=("write", "check", "digest"))
    parser.add_argument("--root", type=Path, default=BUNDLE_DIRECTORY)
    options = parser.parse_args(argv)
    root: Path = options.root
    if options.command == "write":
        write(root)
        return 0
    if options.command == "digest":
        files = _on_disk(root)
        manifest = hashlib.sha256(files[MANIFEST]).hexdigest()
        print(json.dumps({"manifest_sha256": manifest, "bundle_digest": bundle_digest(files)}))
        return 0
    changed = drift(root)
    for path in changed:
        print(f"contract bundle drift: {root / path}")
    return 1 if changed else 0


if __name__ == "__main__":
    sys.exit(main())
