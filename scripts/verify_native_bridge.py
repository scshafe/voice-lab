"""Generate private synthetic bridge fixtures and execute the actual native clients.

The native helper must already be built. This runner never opens a microphone:
it always passes an explicit bounded PCM fixture to the synthetic capture adapter.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from test_recording_contracts import synthetic_contracts

from cole_voice_lab.canonical import canonical_sha256, sha256_file, write_canonical_json
from cole_voice_lab.corpus_store import open_corpus_store
from cole_voice_lab.models import PROMPT_SUITE_V2, Prompt, PromptSuite
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_contracts import (
    RECORDING_SCHEMA_MODELS,
    CaptureReservation,
    CaptureSealReceipt,
    RecordingBuildIdentity,
)
from cole_voice_lab.recording_service import recording_service_build_identity
from cole_voice_lab.recording_takes import RecordingTakeStore


def bridge_catalog(template: PromptSuite) -> PromptSuite:
    """The synthetic contract catalog as the prompt-suite/v2 the corpus store publishes.

    A silent prompt moves its text into instructions; everything else is unchanged.
    """

    return PromptSuite(
        schema_version=PROMPT_SUITE_V2,
        suite_id=template.suite_id,
        purpose=template.purpose,
        prompts=tuple(
            prompt
            if prompt.spoken
            else Prompt.model_validate(
                prompt.model_dump(exclude={"text"}) | {"instructions": prompt.content}
            )
            for prompt in template.prompts
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--binary-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    options = parser.parse_args()
    os.umask(0o077)
    package = options.package.resolve()
    binary = options.binary_directory.resolve()
    root = (options.output or Path(tempfile.mkdtemp(prefix="cole-native-bridge-"))).resolve()
    if root.is_relative_to(package) or root.is_relative_to(Path.cwd().resolve()):
        raise ValueError("Bridge evidence must stay outside the repository")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.stat().st_mode & 0o077:
        raise ValueError("Bridge evidence directory must be private")
    assets = AssetLayout.initialize(root / "assets")
    schemas = root / "schemas"
    schemas.mkdir(mode=0o700)
    for name, model in RECORDING_SCHEMA_MODELS.items():
        write_canonical_json(schemas / f"{name}.schema.json", model.model_json_schema())
    source_files = [
        (str(path.relative_to(package)), sha256_file(path)[0])
        for path in sorted((package / "Sources").rglob("*"))
        if path.is_file()
    ]
    source_digest = canonical_sha256(source_files)
    environment_digest = canonical_sha256(
        {"platform": platform.platform(), "machine": platform.machine()}
    )
    helper = binary / "ColeVoiceRecorderHelper"
    harness = binary / "RecordingBridgeHarness"
    helper_build = RecordingBuildIdentity(
        build_id="native-recorder-bridge-v1",
        source_sha256=source_digest,
        executable_sha256=sha256_file(helper)[0],
        environment_sha256=environment_digest,
    )
    client_build = RecordingBuildIdentity(
        build_id="native-client-bridge-v1",
        source_sha256=source_digest,
        executable_sha256=sha256_file(harness)[0],
        environment_sha256=environment_digest,
    )
    service_build = recording_service_build_identity()
    for name, build in (
        ("helper", helper_build),
        ("client", client_build),
        ("service", service_build),
    ):
        write_canonical_json(root / f"{name}-build.json", build)
    value = synthetic_contracts()
    template_group = value["recording-group"]
    # Every native group references a published store version (recording-group/v2); only
    # legacy continuation still embeds its catalog.
    if template_group.catalog is None:
        raise ValueError("Bridge template group must embed its synthetic catalog")
    catalog = bridge_catalog(template_group.catalog)
    with open_corpus_store(assets, create=True) as corpus:
        corpus.import_prompt_suite(catalog, source="manual")
        corpus.create_catalog(catalog.suite_id, purpose=catalog.purpose)
        reference = corpus.publish_version(catalog.suite_id)
        suite = corpus.materialize(reference).suite
    if suite != catalog:
        raise ValueError("Bridge store published a different catalog")
    store = RecordingTakeStore(assets, clock=lambda: datetime.now(UTC) - timedelta(seconds=1))
    group = store.open_group(
        reference=reference,
        suite=suite,
        subject_id=template_group.subject_id,
        capture_device=template_group.capture_device,
        capture_host="synthetic-host",
    )
    recorder_generation = "synthetic-recorder-generation"
    pcm = b"".join(
        sample.to_bytes(3, "little", signed=True)
        for _ in range(1200)
        for sample in (-8_388_608, 8_388_607, -1, 1)
    )
    pcm_path = root / "synthetic.pcm"
    pcm_path.write_bytes(pcm)
    helper_arguments = [
        "--asset-root",
        str(assets.root),
        "--process-generation",
        recorder_generation,
        "--build-identity",
        str(root / "helper-build.json"),
        "--expected-client-build",
        str(root / "client-build.json"),
        "--schema-directory",
        str(schemas),
        "--synthetic-pcm",
        str(pcm_path),
        "--synthetic-chunk-frames",
        "480",
    ]
    service_arguments = [
        "-m",
        "cole_voice_lab.recording_service",
        "--asset-root",
        str(assets.root),
        "--catalog-sha256",
        group.catalog_sha256,
        "--recording-group-id",
        group.group_id,
        "--process-generation",
        "synthetic-service-generation",
        "--build-identity",
        str(root / "service-build.json"),
        "--expected-client-build",
        str(root / "client-build.json"),
    ]
    manifest = {
        "asset_root": str(assets.root),
        "helper_executable": str(helper),
        "python_executable": sys.executable,
        "schema_directory": str(schemas),
        "recorder_generation": recorder_generation,
        "service_generation": "synthetic-service-generation",
        "synthetic_pcm": str(pcm_path),
        "helper_build": helper_build.model_dump(mode="json"),
        "client_build": client_build.model_dump(mode="json"),
        "service_build": service_build.model_dump(mode="json"),
        "helper_arguments": helper_arguments,
        "service_arguments": service_arguments,
        "service_environment": {
            "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        "group_id": group.group_id,
        "selection": {
            "catalog_sha256": group.catalog_sha256,
            "prompt_id": suite.prompts[0].prompt_id,
            "take_id": None,
            "selection_revision": 0,
            "index_revision": 0,
        },
    }
    write_canonical_json(root / "bridge.json", manifest)
    subprocess.run([str(harness), str(root / "bridge.json")], check=True, timeout=30)
    seal = CaptureSealReceipt.model_validate_json((root / "native-seal.json").read_bytes())
    original = assets.root / seal.original_path
    actual_sha256, actual_size = sha256_file(original)
    if (actual_sha256, actual_size) != (seal.original_sha256, seal.original_size_bytes):
        raise ValueError("Independent Python WAV digest does not match the native seal")
    if original.read_bytes()[44:] != pcm:
        raise ValueError("Independent Python PCM comparison failed")
    result = json.loads((root / "bridge-result.json").read_text())
    reservation_event = json.loads((root / "service-reservation.json").read_text())
    reservation = CaptureReservation.model_validate_json(
        json.dumps(reservation_event["take"]["reservation"])
    )
    if seal.requested_at != reservation.requested_at:
        raise ValueError("Native bridge changed the durable service reservation timestamp")
    if reservation_event["reservation_sha256"] != canonical_sha256(reservation):
        raise ValueError("Service reservation digest differs from Python canonical evidence")
    reopened_store = RecordingTakeStore(assets)
    durable = reopened_store.get_take(seal.take_id)
    if (
        durable.seal != seal
        or durable.decision != "draft"
        or durable.decision_revision != 0
        or durable.transport != "draft"
        or durable.export != "unprepared"
    ):
        raise ValueError("Helper seal was not durably registered as an independent unkept draft")
    # The independent worker may finish before this fixture reopens the store.
    # A completed status must have matching durable evidence, never just a flag.
    if durable.analysis != "pending":
        publications = [
            reopened_store.analysis_result(path.stem)
            for path in (assets.root / "provenance/recording-takes/.analysis").glob("*.json")
        ]
        matching = [
            publication
            for publication in publications
            if publication is not None
            and publication.result.take_id == seal.take_id
            and publication.result.source_sha256 == actual_sha256
            and publication.result.edit_revision == durable.edit_revision
            and publication.result.context_revision == reservation.group.context_revision
            and publication.result.state in {"running", "verified", "needs-review"}
            and (durable.analysis == "running" or publication.result.state == durable.analysis)
            and (publication.result.state == "running" or publication.source_identity is not None)
        ]
        if len(matching) != 1 or durable.analysis not in {"running", "verified", "needs-review"}:
            raise ValueError("Synthetic draft analysis lacks its independently bound result")
    # Failure injection reuses the actual returned reservation as its synthetic template.
    manifest["reservation"] = reservation.model_dump(mode="json")
    manifest["reservation_sha256"] = reservation_event["reservation_sha256"]
    write_canonical_json(root / "bridge.json", manifest)
    result["python_seal_validation"] = True
    result["durable_draft_reopen"] = True
    result["durable_analysis_state"] = durable.analysis
    result["python_wav_sha256"] = actual_sha256
    result["pcm_sha256"] = hashlib.sha256(pcm).hexdigest()
    write_canonical_json(root / "bridge-result.json", result)
    print(json.dumps({"result": "passed", "artifacts": str(root), "pcm_frames": len(pcm) // 3}))


if __name__ == "__main__":
    main()
