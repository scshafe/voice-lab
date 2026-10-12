"""Measurement sessions to the home host through /api/v1/intake (phase 4b, D-021, D-024).

A simulated session becomes the capture package journey.measurement_package defines,
goes through the real /api/v1/intake and the real intake publisher into a synthetic raw/
with its v2 receipt, and every upload is in this machine's ledger before its first byte."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from helpers import record_session
from home_rig import SERVER_URL, bearer, home_rig
from voice_lab_desktop.auth import SignInRequired
from voice_lab_desktop.measurement import home_host
from voice_lab_desktop.measurement.package import read_session_json

from cole_voice_lab.api import journey_intake
from cole_voice_lab.canonical import canonical_json_bytes
from cole_voice_lab.home_host import HomeHostLedger
from cole_voice_lab.intake import RECEIPT_NAME, IntakeReceipt, validate_capture_package
from cole_voice_lab.intake_client import HomeHostClient, UploadError
from cole_voice_lab.journey import measurement_package as package
from cole_voice_lab.journey.jsonio import read_json
from cole_voice_lab.journey.paths import packaged_chains
from cole_voice_lab.models import CaptureManifest


@pytest.fixture
def recorded(backend: Any, levels: Any, pinned: Any, sessions: Any) -> Any:
    return record_session(backend, levels, pinned, sessions, skip_free=True)


def manifest_of(path: Path) -> CaptureManifest:
    return CaptureManifest.model_validate_json((path / "manifest.json").read_bytes())


def test_a_session_becomes_the_measurement_package(
    recorded: Any, sessions: Any, tmp_path: Path
) -> None:
    local = sessions.pending()[0]
    built = home_host.build_package(local, tmp_path / "packages")
    validated = validate_capture_package(built)
    capture = validated.manifest
    session_json = read_session_json(local.path)
    assert built.name == capture.session_id == f"vj-{recorded.session_id}"
    assert capture.prompt_suite_id == "voice-journey-measurement"
    assert capture.capture_host == "voice-lab-desktop" and capture.subject_id == "cole-self"
    assert capture.capture_device.device_id == "voice-journey-chain:umik1"
    kinds = {Path(f.path).name: (Path(f.path).parent.name, f.kind) for f in capture.files}
    for segment in session_json["segments"]:
        key = segment["key"]
        expected = "room-tone" if key == "room-silence" else "calibration"
        assert kinds[segment["file"]] == (key, expected)
    # The session's own record travels with its takes: the distance, the pinned level, and
    # each segment with its label and levels.
    measurement = capture.measurement
    assert measurement is not None
    assert measurement.source_session_id == recorded.session_id
    assert measurement.distance_cm == float(session_json["capture"]["distanceCm"])
    assert measurement.input_volume == float(session_json["capture"]["inputVolume"])
    assert measurement.level_pinned_at == session_json["capture"]["levelPinnedAt"]
    assert measurement.controlled is True
    assert measurement.client is not None and measurement.client.startswith("voice-lab-desktop")
    assert [s.key for s in measurement.segments] == [s["key"] for s in session_json["segments"]]
    paths = {f.path for f in capture.files}
    for recorded_segment, kept in zip(session_json["segments"], measurement.segments, strict=True):
        assert kept.path in paths and kept.label == recorded_segment["label"]
        assert kept.rms_dbfs == recorded_segment["rmsDbfs"]
    # Built once: the same package, the same bytes, so a retry names the same digest.
    before = (built / "manifest.json").read_bytes()
    assert home_host.build_package(local, tmp_path / "packages") == built
    assert (built / "manifest.json").read_bytes() == before
    # The server's compatibility path maps the same session to the same package (only
    # who built it differs): it normalizes the manifest first, the app does not.
    normalized = journey_intake.validate_manifest(session_json, read_json(packaged_chains()))
    located = {f["name"]: local.path / f["name"] for f in session_json["files"]}
    (tmp_path / "server").mkdir()
    server_side, _ = package.write_package(
        normalized,
        located,
        tmp_path / "server",
        subject_id="cole-self",
        capture_host="voice-lab-desktop",
        move=False,
    )
    assert canonical_json_bytes(server_side) == canonical_json_bytes(capture)


def test_a_damaged_or_changed_session_is_never_sent(
    recorded: Any, sessions: Any, tmp_path: Path
) -> None:
    local = sessions.pending()[0]
    wav = next(local.path.glob("*.wav"))
    wav.write_bytes(wav.read_bytes()[:-3] + b"\x00\x00\x00")
    with pytest.raises(package.PackageRefusal) as refused:
        home_host.build_package(local, tmp_path / "packages")
    assert refused.value.code == "files_changed" and refused.value.detail["files"] == [wav.name]
    (local.path / "session.json").write_text("[]")
    with pytest.raises(package.PackageRefusal) as damaged:
        home_host.build_package(local, tmp_path / "packages")
    assert damaged.value.code == "session_damaged"
    assert not (tmp_path / "packages").exists() or not any((tmp_path / "packages").iterdir())


def test_a_session_lands_in_raw_with_its_receipt_and_the_ledger(
    recorded: Any, sessions: Any, measurement_dir: Path, tmp_path: Path
) -> None:
    with home_rig(tmp_path / "rig", tmp_path / "library") as rig:
        seen: list[tuple[str, int, int]] = []
        summary = home_host.upload_pending(
            sessions,
            library=rig.library,
            server_url=SERVER_URL,
            bearer=bearer(),
            client_factory=rig.client_factory(),
            progress=lambda name, sent, size: seen.append((name, sent, size)),
            sleep=rig.publish,
        )
        assert (summary.accepted, summary.pending, summary.failed) == (1, 0, 0)
        (raw,) = rig.accepted()
        receipt = IntakeReceipt.model_validate_json((raw / RECEIPT_NAME).read_bytes())
        built = measurement_dir / "packages" / f"vj-{recorded.session_id}"
        digest = validate_capture_package(built).manifest_sha256
        assert (
            receipt.session_id == f"vj-{recorded.session_id}" and receipt.manifest_sha256 == digest
        )
        assert manifest_of(raw) == manifest_of(built)
        published = validate_capture_package(raw, allow_intake_receipt=True).manifest
        assert published.measurement is not None and published.measurement.distance_cm == 35.0
        # The ledger: started before the first byte, completed with the receipt.
        events = [entry.event for entry, _ in HomeHostLedger(rig.library).entries()]
        assert events == ["upload-started", "upload-completed"]
        assert all(e.manifest_sha256 == digest for e, _ in HomeHostLedger(rig.library).entries())
        upload = json.loads((recorded.session_dir / "upload.json").read_text())
        assert upload["state"] == "accepted" and upload["server"] == SERVER_URL
        assert upload["receipt"]["manifestSha256"] == digest
        assert {name for name, _sent, _size in seen} >= {"room-silence/01-room-silence.wav"}
        assert all(
            sent == size for name, sent, size in seen if name.endswith(".json") or sent == size
        )
        # Nothing is pending any more: a second run sends nothing.
        again = home_host.upload_pending(
            sessions,
            library=rig.library,
            server_url=SERVER_URL,
            bearer=bearer(),
            client_factory=rig.client_factory(),
            sleep=rig.publish,
        )
        assert (again.accepted, again.pending, again.failed) == (0, 0, 0)


def test_an_interrupted_upload_resumes_from_the_servers_bytes(
    recorded: Any, sessions: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cole_voice_lab.intake_client as intake_client

    monkeypatch.setattr(intake_client, "CHUNK_BYTES", 256 * 1024)
    puts: list[tuple[str, int, int, int]] = []  # path, start, end, total

    class Dropping(HomeHostClient):
        def request(self, method: str, path: str, **kwargs: Any) -> Any:
            if method == "PUT":
                span, total = dict(kwargs.get("headers") or {})["Content-Range"][6:].split("/")
                start, end = span.split("-")
                puts.append((path, int(start), int(end), int(total)))
                if len(puts) == 3:
                    raise UploadError("the home host is unreachable (ConnectionResetError)")
            return super().request(method, path, **kwargs)

    with home_rig(tmp_path / "rig", tmp_path / "library") as rig:

        def dropping(_approval: Any, token: Any) -> HomeHostClient:
            return Dropping(
                endpoint="127.0.0.1", resolver=None, bearer=token, loopback_port=rig.port
            )

        first = home_host.upload_pending(
            sessions,
            library=rig.library,
            server_url=SERVER_URL,
            bearer=bearer(),
            client_factory=dropping,
            sleep=rig.publish,
        )
        assert first.pending == 1 and "unreachable" in (first.last_error or "")
        assert sessions.pending()[0].upload["attempts"] == 1
        sent_first = list(puts)
        second = home_host.upload_pending(
            sessions,
            library=rig.library,
            server_url=SERVER_URL,
            bearer=bearer(),
            client_factory=dropping,
            sleep=rig.publish,
        )
        assert second.accepted == 1
        sent_second = puts[len(sent_first) :]
        # No file the first attempt completed is sent again ...
        completed = {path for path, _s, end, total in sent_first[:-1] if end + 1 == total}
        assert completed and not completed & {path for path, *_ in sent_second}
        # ... and the interrupted file starts again where the server stopped holding it.
        dropped_path, dropped_start, _end, _total = sent_first[-1]
        resumed = next(start for path, start, _e, _t in sent_second if path == dropped_path)
        assert resumed == dropped_start
        events = [entry.event for entry, _ in HomeHostLedger(rig.library).entries()]
        assert events == ["upload-started", "upload-started", "upload-completed"]


def test_this_machine_must_be_ready(recorded: Any, sessions: Any, tmp_path: Path) -> None:
    library_dir = tmp_path / "library"
    library = home_host.library_layout(library_dir)  # made private, nothing bound
    with pytest.raises(home_host.HomeHostNotReady, match="no home-host approval is bound"):
        home_host.upload_pending(sessions, library=library, server_url=SERVER_URL, bearer=bearer())
    with home_rig(tmp_path / "rig", tmp_path / "capture-only", classes=("capture",)) as rig:
        with pytest.raises(home_host.HomeHostNotReady, match="does not cover measurement"):
            home_host.upload_pending(
                sessions, library=rig.library, server_url=SERVER_URL, bearer=bearer()
            )
    with home_rig(tmp_path / "rig2", tmp_path / "library2") as rig:
        with pytest.raises(home_host.HomeHostNotReady, match="the approved home host is"):
            home_host.upload_pending(
                sessions,
                library=rig.library,
                server_url="https://other.example.ts.net",
                bearer=bearer(),
            )
    assert sessions.pending()[0].upload == {}  # nothing was attempted


def test_a_refused_sign_in_keeps_the_session_waiting(
    recorded: Any, sessions: Any, tmp_path: Path
) -> None:
    with home_rig(tmp_path / "rig", tmp_path / "library") as rig:
        with pytest.raises(SignInRequired):
            home_host.upload_pending(
                sessions,
                library=rig.library,
                server_url=SERVER_URL,
                bearer=bearer({"sub": "someone-else"}),
                client_factory=rig.client_factory(),
                sleep=rig.publish,
            )
        assert sessions.pending()[0].upload["lastError"] == "sign-in required"

        def gone() -> str:
            raise SignInRequired("not signed in")

        with pytest.raises(SignInRequired):
            home_host.upload_pending(
                sessions,
                library=rig.library,
                server_url=SERVER_URL,
                bearer=gone,
                client_factory=rig.client_factory(),
                sleep=rig.publish,
            )
        assert rig.accepted() == []
