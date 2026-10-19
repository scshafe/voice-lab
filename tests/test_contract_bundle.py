"""The corpus client's contract bundle (D-056 step 5, docs/design/HOUSE-PANEL.md §4).

The bundle on disk is what the code generates; its golden take is a package the PC's
validator accepts and a server seeded with the fixture catalog admits; each example is what
its manifest entry says; the vectors are the canonical form; and the OpenAPI document names
exactly the routes a corpus client's token reaches.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
import shutil
import wave
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from cole_voice_lab import cli
from cole_voice_lab import contract_bundle as bundle
from cole_voice_lab.api.client_scope import client_may
from cole_voice_lab.canonical import canonical_json_bytes
from cole_voice_lab.corpus_read_model import read_active_catalog
from cole_voice_lab.corpus_take_store import read_take_state
from cole_voice_lab.corpus_takes import validate_profile_take_package
from cole_voice_lab.intake_publisher import publish_pending
from cole_voice_lab.models import COLE_SUBJECT
from cole_voice_lab.paths import AssetLayout

REPOSITORY = Path(__file__).resolve().parents[1]
ROOT = REPOSITORY / bundle.BUNDLE_DIRECTORY


def _manifest() -> dict[str, Any]:
    return dict(json.loads((ROOT / bundle.MANIFEST).read_bytes()))


def _models() -> dict[str, type[BaseModel]]:
    return {
        **bundle.COMPONENTS,
        "CanonicalJsonVectors": bundle.CanonicalJsonVectors,
        "WavRecipe": bundle.WavRecipe,
    }


def _write_take(directory: Path) -> None:
    directory.mkdir()
    for name, data in bundle.golden_take().items():
        if name != "original.wav.recipe.json":
            (directory / name).write_bytes(data)
    (directory / "original.wav").write_bytes(bundle.golden_wav())


def test_the_bundle_on_disk_is_what_the_code_generates() -> None:
    assert bundle.drift(ROOT) == []


def test_the_golden_wav_is_its_recipe() -> None:
    wav = bundle.golden_wav()
    recipe = bundle.WavRecipe.model_validate_json(
        (ROOT / "examples/take/original.wav.recipe.json").read_bytes()
    )
    assert hashlib.sha256(wav).hexdigest() == recipe.sha256 and len(wav) == recipe.size_bytes
    # The plain 44-byte header: what Python's wave module writes for the same frames.
    written = io.BytesIO()
    with wave.open(written, "wb") as writer:
        writer.setnchannels(recipe.channels)
        writer.setsampwidth(2)
        writer.setframerate(recipe.rate)
        writer.writeframes(wav[44:])
    assert written.getvalue() == wav
    # The formula, at one sample: channel 2 (microphone 1) of frame 7.
    frame, channel = 7, 2
    offset = 44 + (frame * recipe.channels + channel) * 2
    expected = (
        ((frame * (channel + 3) + recipe.offset) % recipe.modulus - recipe.center)
        * (channel + 1)
        * recipe.scale
    )
    assert int.from_bytes(wav[offset : offset + 2], "little", signed=True) == expected


def test_the_golden_take_is_a_package_the_pc_validates(tmp_path: Path) -> None:
    _write_take(tmp_path / "take")
    take = validate_profile_take_package(tmp_path / "take")
    assert take.manifest.capture_profile.profile == "xvf3800-linear-4"
    assert take.qc_report.status == "pass"
    for name, data in bundle.golden_take().items():
        if name != "original.wav.recipe.json":
            assert (ROOT / "examples/take" / name).read_bytes() == data


def test_a_server_seeded_with_the_fixture_catalog_admits_the_golden_take(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "lab")
    with contextlib.redirect_stdout(io.StringIO()):
        assert (
            cli.main(
                [
                    "catalog",
                    "init",
                    "--subject-id",
                    COLE_SUBJECT,
                    "--from-builtin",
                    "pilot-v2",
                    "--asset-root",
                    str(layout.root),
                ]
            )
            == 0
        )
    record = read_active_catalog(layout, COLE_SUBJECT)
    assert record is not None and record.reference == bundle.fixture_catalog()[0]
    session = json.loads((ROOT / "examples/intake-session-request.json").read_bytes())
    _write_take(layout.directory("incoming") / session["session_id"])
    summary = publish_pending(layout, min_free_bytes=0)
    assert [receipt.session_id for receipt in summary.accepted] == [session["session_id"]]
    state = read_take_state(layout, session["session_id"])
    assert state is not None and state.capture_profile is not None


def test_each_example_is_what_its_manifest_entry_says() -> None:
    models = _models()
    entries = _manifest()["examples"]
    assert {entry["path"] for entry in entries} == {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "examples").rglob("*")
        if path.is_file()
    }
    for entry in entries:
        payload = (ROOT / entry["path"]).read_bytes()
        model = models[entry["schema"]]
        if entry["expected"] == "valid":
            record = model.model_validate_json(payload)
            assert payload == canonical_json_bytes(record) + b"\n", entry["path"]
            continue
        assert entry["expected"] == "invalid"
        with pytest.raises(ValidationError) as refused:
            model.model_validate_json(payload)
        refusal = entry["refusal"]
        if "message_contains" in refusal:
            assert refusal["message_contains"] in str(refused.value), entry["path"]
        else:
            assert refusal["code"] == "invalid_request"


def test_the_vectors_are_the_canonical_form() -> None:
    vectors = bundle.CanonicalJsonVectors.model_validate_json(
        (ROOT / "examples/canonical-json-vectors.json").read_bytes()
    )
    assert len(vectors.vectors) >= 7
    for vector in vectors.vectors:
        assert canonical_json_bytes(vector.value).decode("utf-8") == vector.canonical
    texts = "".join(vector.canonical for vector in vectors.vectors)
    for fragment in ("31.6", "1e-07", "1e+22", "1.2345678901234568e+17", "100.0", "\\u0001"):
        assert fragment in texts


def test_the_manifest_binds_every_other_file() -> None:
    manifest = _manifest()
    assert manifest["contract_id"] == "corpus-client.v1"
    assert manifest["openapi_path"] == "openapi.json"
    assert manifest["required_semantics"] == list(bundle.REQUIRED_SEMANTICS)
    listed = {entry["path"]: entry for entry in manifest["files"]}
    on_disk = {
        path.relative_to(ROOT).as_posix(): path.read_bytes()
        for path in ROOT.rglob("*")
        if path.is_file()
    }
    assert set(listed) == set(on_disk) - {"manifest.json"}
    for path, entry in listed.items():
        assert entry["sha256"] == hashlib.sha256(on_disk[path]).hexdigest()
        assert entry["size_bytes"] == len(on_disk[path])
    # The bundle digest: each sorted path hashed as path NUL contents NUL.
    digest = hashlib.sha256()
    for path in sorted(on_disk):
        digest.update(path.encode() + b"\0" + on_disk[path] + b"\0")
    assert bundle.bundle_digest(on_disk) == digest.hexdigest()


def _concrete(path: str) -> str:
    return (
        path.replace("{prompt_id}", "harvard-001")
        .replace("{session_id}", "take-" + "a" * 64)
        .replace("{path}", "original.wav")
        .replace("{take_id}", "take-" + "a" * 64)
        .replace("{request_id}", "req-" + "0" * 32)
    )


def test_the_openapi_document_is_the_clients_allow_list() -> None:
    document = json.loads((ROOT / "openapi.json").read_bytes())
    assert document["openapi"] == "3.1.0"
    schemas = document["components"]["schemas"]
    for path, operations in document["paths"].items():
        for method, operation in operations.items():
            query = (
                "profile=xvf3800-linear-4"
                if any(parameter["in"] == "query" for parameter in operation.get("parameters", ()))
                else ""
            )
            assert client_may(method.upper(), _concrete(path), query), (method, path)
    references = set(re.findall(r'"\$ref": "#/components/schemas/([^"]+)"', json.dumps(document)))
    assert references and references <= set(schemas)
    contract = document["x-voice-lab-contract"]
    assert contract["client_audience"] == "voice-lab-panel"
    assert contract["bounds"]["tolerance_ns_max"] == 4_000_000


def test_check_reports_drift_and_write_repairs_it(tmp_path: Path) -> None:
    copy = tmp_path / "corpus-client.v1"
    shutil.copytree(ROOT, copy)
    (copy / "examples/take/qc.json").write_bytes(b"{}\n")
    (copy / "examples/extra.json").write_bytes(b"{}\n")
    (copy / "schemas/QcReport.schema.json").unlink()
    assert bundle.drift(copy) == [
        "examples/extra.json",
        "examples/take/qc.json",
        "schemas/QcReport.schema.json",
    ]
    assert bundle.main(["check", "--root", str(copy)]) == 1
    bundle.write(copy)
    assert bundle.drift(copy) == []
    assert bundle.main(["check", "--root", str(copy)]) == 0
    # The authored documents are the bundle's own: a changed one moves the manifest only.
    (copy / "PROTOCOL.md").write_text("Another protocol.\n")
    assert bundle.drift(copy) == ["manifest.json"]
    (copy / "README.md").unlink()
    with pytest.raises(FileNotFoundError, match="README.md"):
        bundle.drift(copy)


def test_the_digest_command_names_the_manifest_and_the_bundle(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert bundle.main(["digest", "--root", str(ROOT)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert (
        printed["manifest_sha256"]
        == hashlib.sha256((ROOT / "manifest.json").read_bytes()).hexdigest()
    )
    assert len(printed["bundle_digest"]) == 64
