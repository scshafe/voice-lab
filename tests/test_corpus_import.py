"""The Mac's takes to the PC (D-053, docs/design/RECORDING-V1.md step 9): the imported-take
package and its validator, the publisher's admission and the Mac's state on the PC, the
exporter's plan, approval and send, and the classification of every new path.

Synthetic only: a Mac library of generated tones made through the take store
(``corpus_import_fixtures``), a PC lab with the same seeded catalog, and an in-process API.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import io
import json
import os
import shutil
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from corpus_import_fixtures import (
    MAC_DEVICE,
    MacLibrary,
    catalog_view,
    json_record,
    manifest_of,
    record_json,
    reseal_import,
    writable,
)
from corpus_take_fixtures import NOW, make_take, seed_catalog
from home_host_fixtures import NOW as APPROVAL_NOW
from home_host_fixtures import bind_approval, jwks, make_approval, mint, policy
from recording_fixtures import synthetic_catalog
from test_pilot_v2_coverage import released_pilot_v2

from cole_voice_lab import cli, cli_corpus_import
from cole_voice_lab.api.auth import TokenVerifier
from cole_voice_lab.api.contracts import ApiMeta
from cole_voice_lab.api.corpus_contracts import CatalogView, CorpusRequestStatus, TakeHistory
from cole_voice_lab.api.corpus_routes import CorpusRoutes
from cole_voice_lab.api.server import ApiServer
from cole_voice_lab.api.staging import IntakeStaging
from cole_voice_lab.canonical import canonical_json_bytes, canonical_sha256, write_canonical_json
from cole_voice_lab.capture import ReadyMarker
from cole_voice_lab.corpus_import import (
    EXPORT_ROOT,
    CorpusImportApproval,
    CorpusImportPlan,
    approval_path,
    approve_plan,
    build_take_import_package,
    catalog_from_view,
    fetch_catalog_view,
    package_path,
    plan_export,
    plan_path,
    read_approval,
    read_catalog_view,
    read_plan,
    send_plan,
)
from cole_voice_lab.corpus_read_model import read_library
from cole_voice_lab.corpus_take_imports import (
    TAKE_IMPORT_FILES,
    CorpusTakeImportManifest,
    MacTakeState,
    ValidatedTakeImport,
    validate_take_import_package,
)
from cole_voice_lab.corpus_take_store import (
    CorpusTakeState,
    TakeDecision,
    TakeOrigin,
    TakeTrim,
    read_take_state,
    record_takes,
    state_path,
)
from cole_voice_lab.corpus_takes import AUDIO_NAME, take_id_for
from cole_voice_lab.diagnostic_roots import DIAGNOSTIC_SUBJECT_PREFIX, initialize_diagnostic_root
from cole_voice_lab.errors import ContractError, SecurityError
from cole_voice_lab.home_host import HomeHostApproval, HomeHostLedger, load_approval
from cole_voice_lab.intake import validate_package
from cole_voice_lab.intake_client import HomeHostClient, UploadError
from cole_voice_lab.intake_publisher import publish_pending, read_refusal
from cole_voice_lab.library_classification import classify
from cole_voice_lab.library_replication import RawPackageSource, ReplicationItem
from cole_voice_lab.models import COLE_SUBJECT
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_contracts import (
    CatalogReference,
    CatalogSnapshot,
    RecordingFrameRange,
    RecordingTake,
)

type Catalog = tuple[CatalogReference, CatalogSnapshot]

REASON_REJECTED = "Mouth click at the start"
REASON_REMOVED = "Recorded twice by mistake"


# -- Two libraries -----------------------------------------------------------------------------


@dataclass
class Libraries:
    """The PC's lab and the Mac's library, seeded with the same catalog version."""

    pc: AssetLayout
    mac: MacLibrary
    catalog: Catalog
    work: Path

    @property
    def view(self) -> CatalogView:
        return catalog_view(self.catalog)


@pytest.fixture
def libraries(tmp_path: Path) -> Libraries:
    pc = AssetLayout.initialize(tmp_path / "srv" / "voice-lab" / "lab")
    mac_layout = AssetLayout.initialize(tmp_path / "mac" / "voice-lab" / "lab")
    catalog = seed_catalog(pc)
    # The Mac seeds the same version from the same files: the same reference, every field.
    assert seed_catalog(mac_layout) == catalog
    bind_approval(mac_layout)
    work = tmp_path / "work"
    work.mkdir()
    return Libraries(pc, MacLibrary(mac_layout), catalog, work)


def spoken_ids(catalog: Catalog) -> list[str]:
    return [prompt.prompt_id for prompt in catalog[1].suite.prompts if prompt.spoken]


@dataclass
class MacTakes:
    kept: RecordingTake
    rejected: RecordingTake
    removed: RecordingTake
    trimmed: RecordingTake
    draft: RecordingTake
    unsealed: str
    pilot_kept: RecordingTake
    room_tone: RecordingTake

    def exported(self) -> dict[str, RecordingTake]:
        """The takes a plan exports, by name."""
        return {
            name: getattr(self, name)
            for name in ("kept", "rejected", "removed", "trimmed", "draft", "pilot_kept")
        }


def record_mac_takes(libraries: Libraries) -> MacTakes:
    """A v2 group against cole-corpus (kept, rejected with a reason, removed, trimmed, draft and
    unsealed takes) and a v1 group embedding pilot-v2 (a kept spoken take and a room tone)."""
    mac = libraries.mac
    ids = spoken_ids(libraries.catalog)
    group = mac.v2_group(libraries.catalog)
    kept = mac.save(mac.sealed(group, ids[0]))
    rejected = mac.decide(mac.sealed(group, ids[1]), "reject", REASON_REJECTED)
    removed = mac.decide(mac.save(mac.sealed(group, ids[2])), "remove", REASON_REMOVED)
    draft_take = mac.sealed(group, ids[3])
    trimmed = mac.save(mac.sealed(group, ids[4]), frames=(1_200, 9_600))
    unsealed = mac.reserve(group, ids[5]).take_id
    pilot = released_pilot_v2()
    pilot_group = mac.v1_group(pilot)
    pilot_spoken = next(p for p in pilot.prompts if p.prompt_id == ids[6])
    room = next(p for p in pilot.prompts if p.category == "room-tone")
    pilot_kept = mac.save(mac.sealed(pilot_group, pilot_spoken.prompt_id))
    room_tone = mac.save(mac.sealed(pilot_group, room.prompt_id))
    return MacTakes(kept, rejected, removed, trimmed, draft_take, unsealed, pilot_kept, room_tone)


def pc_take_id(take: RecordingTake) -> str:
    assert take.seal is not None
    return take_id_for(take.seal.original_sha256)


@contextmanager
def serving(layout: AssetLayout, tmp_path: Path) -> Iterator[int]:
    jwks_path = tmp_path / "jwks.json"
    jwks_path.write_text(json.dumps(jwks()), encoding="utf-8")
    server = ApiServer(
        ("127.0.0.1", 0),
        staging=IntakeStaging(layout, min_free_bytes=0),
        verifier=TokenVerifier(policy(jwks_path)),
        meta=ApiMeta(server_build="test-build", minimum_client_version="0.1.0"),
        log=False,
        routes=(CorpusRoutes(layout, clock=lambda: NOW + timedelta(hours=1)),),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()


def call(
    port: int, method: str, path: str, body: dict[str, Any] | None = None
) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Authorization": f"Bearer {mint()}"}
    payload = None if body is None else json.dumps(body).encode()
    if payload is not None:
        headers["Content-Type"] = "application/json"
    connection.request(method, path, body=payload, headers=headers)
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response.status, data


def send(libraries: Libraries, port: int, plan_sha256: str, **options: Any) -> Any:
    """``send_plan`` over the loopback API, the PC's publisher standing in for the wait."""
    return send_plan(
        libraries.mac.layout,
        plan_sha256,
        client=HomeHostClient.loopback_for_tests(port, mint),
        clock=lambda: APPROVAL_NOW,
        sleep=lambda _seconds: publish_pending(libraries.pc, min_free_bytes=0),
        **options,
    )


def standing_approval(layout: AssetLayout) -> HomeHostApproval:
    return load_approval(layout, now=APPROVAL_NOW)[0]


def manifest_digest(package: Path) -> str:
    return hashlib.sha256((package / "manifest.json").read_bytes()).hexdigest()


# -- End to end --------------------------------------------------------------------------------


def test_the_macs_takes_reach_the_pc_with_their_state(libraries: Libraries, tmp_path: Path) -> None:
    takes = record_mac_takes(libraries)
    mac = libraries.mac.layout
    with serving(libraries.pc, tmp_path) as port:
        client = HomeHostClient.loopback_for_tests(port, mint)
        view = fetch_catalog_view(client, standing_approval(mac))
        assert (view.reference, view.suite, view.prompt_aliases) == (
            libraries.catalog[0],
            libraries.catalog[1].suite,
            libraries.catalog[1].prompt_aliases,
        )

        planning = plan_export(mac, view, clock=lambda: NOW)
        exported = takes.exported()
        expected_ids = sorted(pc_take_id(take) for take in exported.values())
        assert planning.plan is not None and planning.plan_sha256 is not None
        assert [entry.take_id for entry in planning.plan.packages] == expected_ids
        assert sorted(planning.built) == expected_ids and planning.reused == ()
        assert planning.plan.catalog == libraries.catalog[0]
        reasons = {item.source: item.reason for item in planning.skipped}
        assert reasons == {
            takes.unsealed: "unsealed",
            takes.room_tone.reservation.take_id: "room-tone",
        }
        report = planning.report(mac)
        assert report["packages"] == 6 and report["skipped_counts"] == {
            "room-tone": 1,
            "unsealed": 1,
        }
        plan_file = plan_path(mac, planning.plan_sha256)
        assert stat.S_IMODE(plan_file.stat().st_mode) == 0o400
        assert read_plan(mac, planning.plan_sha256) == planning.plan
        for entry in planning.plan.packages:
            assert entry.manifest_sha256 == manifest_digest(package_path(mac, entry.take_id))

        # Planning again builds nothing and names the same payload.
        again = plan_export(mac, view, clock=lambda: NOW + timedelta(days=1))
        assert again.plan_sha256 == planning.plan_sha256 and again.built == ()
        assert sorted(again.reused) == expected_ids and again.state_changed == ()

        # Not sent before it is approved.
        with pytest.raises(ContractError, match="is not approved"):
            send(libraries, port, planning.plan_sha256)
        assert HomeHostLedger(mac).entries() == []
        approval, written = approve_plan(
            mac,
            planning.plan_sha256,
            approved_by="Cole",
            statement="My Mac's takes, to my own PC.",
            now=APPROVAL_NOW,
        )
        assert written and approval.packages == 6
        assert approval.destination.endpoint == "voice-lab.example.ts.net"
        assert stat.S_IMODE(approval_path(mac, planning.plan_sha256).stat().st_mode) == 0o400

        results = send(libraries, port, planning.plan_sha256)
        assert [(result.take_id, result.status) for result in results] == [
            (take_id, "accepted") for take_id in expected_ids
        ]
        events = [entry.event for entry, _ in HomeHostLedger(mac).entries()]
        assert events == ["upload-started", "upload-completed"] * 6

        # On the PC, each take holds the Mac's state and says where it came from.
        pc = libraries.pc
        states = {name: read_take_state(pc, pc_take_id(take)) for name, take in exported.items()}
        for name, state in states.items():
            assert state is not None, name
            assert state.origin == TakeOrigin(
                kind="imported", source_take_id=exported[name].reservation.take_id
            )
            assert state.device == MAC_DEVICE.display_name
            assert state.catalog == libraries.catalog[0]
        kept, rejected, removed = states["kept"], states["rejected"], states["removed"]
        trimmed, draft, pilot = states["trimmed"], states["draft"], states["pilot_kept"]
        assert kept is not None and rejected is not None and removed is not None
        assert trimmed is not None and draft is not None and pilot is not None
        accepted_at = kept.accepted_at
        # A Save records no time on a Swift take: decided when the PC accepted it.
        assert kept.decision == TakeDecision(
            decision="kept", revision=1, reason=None, decided_at=accepted_at
        )
        assert rejected.decision.decision == "rejected"
        assert rejected.decision.reason == REASON_REJECTED
        evidence = takes.rejected.decision_evidence
        assert evidence is not None and rejected.decision.decided_at == evidence.committed_at
        assert (removed.decision.decision, removed.decision.reason) == ("removed", None)
        assert draft.decision == TakeDecision(
            decision="draft", revision=0, reason=None, decided_at=None
        )
        assert trimmed.trim == TakeTrim(revision=1, start_frame=1_200, end_frame=9_600)
        assert kept.trim == TakeTrim(revision=0, start_frame=None, end_frame=None)
        assert pilot.decision.decision == "kept"
        # The removal's reason stays in the raw package.
        raw = pc.root / removed.raw_path
        mac_state = MacTakeState.model_validate_json((raw / "mac-state.json").read_bytes())
        assert (mac_state.reason, mac_state.restore_decision) == (REASON_REMOVED, "kept")

        # The library counts them, and the prompt's history says they were imported.
        found = read_library(pc, COLE_SUBJECT)
        assert found is not None
        library = found[0]
        rows = {row.prompt_id: row for row in library.rows}
        ids = spoken_ids(libraries.catalog)
        # Rejected and removed takes cover nothing; the unsealed take was never sent.
        assert [rows[prompt].status for prompt in ids[:7]] == [
            "kept",
            "missing",
            "missing",
            "draft",
            "kept",
            "missing",
            "kept",
        ]
        assert (library.coverage.takes, library.coverage.kept, library.coverage.draft) == (6, 3, 1)
        status, body = call(port, "GET", f"/api/v1/corpus/prompts/{ids[0]}/takes")
        assert status == 200
        [listed] = TakeHistory.model_validate_json(body).takes
        assert listed.origin == kept.origin and listed.decision_revision == 1

        # Step 4's decisions apply to an imported take like any other.
        request = {
            "request_id": f"req-{1:032x}",
            "decision": "rejected",
            "reason": "Too quiet after all",
            "expected_revision": 1,
        }
        status, body = call(port, "POST", f"/api/v1/corpus/takes/{kept.take_id}/decision", request)
        assert status == 202, body
        publish_pending(pc, min_free_bytes=0, clock=lambda: NOW + timedelta(hours=2))
        status, body = call(port, "GET", f"/api/v1/corpus/requests/{request['request_id']}")
        assert CorpusRequestStatus.model_validate_json(body).status == "applied"
        changed = read_take_state(pc, kept.take_id)
        assert changed is not None and changed.decision.revision == 2
        assert changed.decision.decision == "rejected" and changed.origin == kept.origin

        # A second send is a no-op: nothing is uploaded, nothing is written to the ledger.
        again_results = send(libraries, port, planning.plan_sha256)
        assert {(result.status, result.before) for result in again_results} == {("accepted", True)}
        assert len(HomeHostLedger(mac).entries()) == 12


# -- The package and its validator ---------------------------------------------------------------


def build(libraries: Libraries, take: RecordingTake, name: str, **changes: Any) -> Path:
    """The imported-take package of ``take`` in ``work/<name>``, for the PC's catalog."""
    directory = libraries.work / name
    build_take_import_package(
        directory,
        layout=libraries.mac.layout,
        take=take,
        catalog=changes.pop("catalog", libraries.catalog[0]),
        clock=changes.pop("clock", lambda: NOW),
        **changes,
    )
    return directory


def to_incoming(layout: AssetLayout, package: Path) -> Path:
    """A copy of ``package`` where the API's staging would finalize it."""
    target = layout.directory("incoming") / manifest_of(package).take_id
    shutil.copytree(package, target)
    return target


@pytest.fixture
def kept(libraries: Libraries) -> RecordingTake:
    mac = libraries.mac
    return mac.save(mac.sealed(mac.v2_group(libraries.catalog), spoken_ids(libraries.catalog)[0]))


@pytest.fixture
def package(libraries: Libraries, kept: RecordingTake) -> Path:
    return build(libraries, kept, "kept")


def test_a_package_carries_the_swift_takes_records_byte_for_byte(
    libraries: Libraries, kept: RecordingTake, package: Path
) -> None:
    root = libraries.mac.layout.root
    source = kept.reservation.take_id
    assert {path.name for path in package.iterdir()} == set(TAKE_IMPORT_FILES)
    records = root / "provenance" / "recording-takes" / source
    assert (package / "seal.json").read_bytes() == (records / "seal.json").read_bytes()
    assert (package / "reservation.json").read_bytes() == (
        records / "reservation.json"
    ).read_bytes()
    group = root / "provenance" / "recording-groups" / f"{kept.reservation.group.group_id}.json"
    assert (package / "group.json").read_bytes() == group.read_bytes()
    assert (package / AUDIO_NAME).read_bytes() == (
        root / kept.reservation.original_path
    ).read_bytes()
    manifest = manifest_of(package)
    assert kept.seal is not None
    assert manifest.take_id == take_id_for(kept.seal.original_sha256)
    assert manifest.source_take_id == source and manifest.subject_id == COLE_SUBJECT
    assert manifest.recorded_against.kind == "catalog-reference"
    assert manifest.recorded_against.model_dump()["reference"] == libraries.catalog[0].model_dump()
    assert (manifest.requested_at, manifest.first_sample_at) == (
        kept.seal.requested_at,
        kept.seal.first_sample_at,
    )
    assert manifest.client.model_dump() == {
        "name": "Cole Voice Recorder",
        "version": kept.seal.recorder_build.build_id,
        "platform": "macOS",
    }
    assert manifest.capture_device == MAC_DEVICE and manifest.exporter.exported_at == NOW
    validated = validate_package(package)
    assert isinstance(validated, ValidatedTakeImport)
    assert validated.session_id == manifest.take_id and validated.mac_state.decision == "kept"
    # The Mac's library is unchanged: the take keeps its audio and records.
    assert (root / kept.reservation.original_path).is_file()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert cli.main(["intake", "validate", str(package)]) == 0
    assert json.loads(out.getvalue())["session_id"] == manifest.take_id


def test_a_pilot_groups_take_names_the_suite_it_embeds(libraries: Libraries) -> None:
    mac = libraries.mac
    pilot = released_pilot_v2()
    take = mac.save(mac.sealed(mac.v1_group(pilot), spoken_ids(libraries.catalog)[0]))
    against = manifest_of(build(libraries, take, "pilot")).recorded_against.model_dump()
    assert against == {
        "kind": "embedded-suite",
        "suite_id": pilot.suite_id,
        "catalog_sha256": take.reservation.group.catalog_sha256,
    }


def flip_audio(package: Path) -> None:
    data = bytearray((package / AUDIO_NAME).read_bytes())
    data[-1] ^= 0x01
    (package / AUDIO_NAME).write_bytes(bytes(data))


def changed_record(package: Path, name: str, change: Any) -> None:
    document = record_json(package, name)
    change(document)
    reseal_import(package, files={name: json_record(document)})


def other_audio_in_seal(document: dict[str, Any]) -> None:
    document["original_sha256"] = "f" * 64
    document["continuity"]["wav_sha256"] = "f" * 64


def other_build(document: dict[str, Any]) -> None:
    document["recorder_build"]["build_id"] = "another-build"


def shorter_reservation(document: dict[str, Any]) -> None:
    document["maximum_frames"] = 28_000_000


def other_device(document: dict[str, Any]) -> None:
    document["capture_device"]["device_id"] = "another-umik-1"


def trim_past_the_take(document: dict[str, Any]) -> None:
    frames = document["frame_range"]["source_frames"]
    document["frame_range"] = {
        "source_frames": frames + 100,
        "start_frame": 0,
        "end_frame": frames + 50,
    }
    document["edit_revision"] = 1


def rejected_without_reason(document: dict[str, Any]) -> None:
    document["decision"] = "rejected"


def another_swift_take(document: dict[str, Any]) -> None:
    document["source_take_id"] = "take-" + "e" * 64


@pytest.mark.parametrize(
    ("tamper", "error", "match"),
    [
        (flip_audio, SecurityError, "recording hash or byte count was tampered"),
        *[
            (
                lambda package, name=name: (package / name).write_bytes(
                    (package / name).read_bytes().replace(b'"', b'"', 1) + b" "
                ),
                SecurityError,
                f"a record's hash or byte count was tampered: {name}",
            )
            for name in ("seal.json", "reservation.json", "group.json", "mac-state.json")
        ],
        (
            lambda package: changed_record(package, "seal.json", other_audio_in_seal),
            SecurityError,
            "the seal describes other audio",
        ),
        (
            lambda package: changed_record(package, "seal.json", other_build),
            ContractError,
            "client version is not the seal's recorder build",
        ),
        (
            lambda package: changed_record(package, "reservation.json", shorter_reservation),
            SecurityError,
            "the seal is not bound to this reservation",
        ),
        (
            lambda package: changed_record(package, "group.json", other_device),
            SecurityError,
            "group.json is not the group the reservation names",
        ),
        (
            lambda package: changed_record(package, "mac-state.json", trim_past_the_take),
            ContractError,
            "the Mac's trim is not within the take's frames",
        ),
        (
            lambda package: changed_record(package, "mac-state.json", rejected_without_reason),
            ContractError,
            "invalid canonical evidence",
        ),
        (
            lambda package: changed_record(package, "mac-state.json", another_swift_take),
            ContractError,
            "name another Swift take",
        ),
    ],
)
def test_a_tampered_package_is_refused(
    package: Path, tamper: Any, error: type[Exception], match: str
) -> None:
    validate_take_import_package(package)
    writable(package)
    tamper(package)
    with pytest.raises(error, match=match):
        validate_take_import_package(package)


def test_audio_rebound_into_the_manifest_still_disagrees_with_the_seal(package: Path) -> None:
    writable(package)
    flip_audio(package)
    inspection_sha = hashlib.sha256((package / AUDIO_NAME).read_bytes()).hexdigest()
    document = record_json(package, "manifest.json")
    audio = {**document["audio"], "sha256": inspection_sha}
    reseal_import(package, manifest={"audio": audio, "take_id": f"take-{inspection_sha}"})
    with pytest.raises(SecurityError, match="the seal describes other audio"):
        validate_take_import_package(package)


@pytest.mark.parametrize(
    ("fields", "match"),
    [
        ({"client": {"name": "Cole Voice Recorder", "version": "x", "platform": "macOS"}},
         "client version"),
        ({"capture_host": "another-host"}, "device or host is not the group's"),
        ({"recorded_against": {"kind": "embedded-suite", "suite_id": "pilot-v2",
                               "catalog_sha256": "a" * 64}}, "recorded_against"),
        ({"first_sample_at": "2026-09-12T12:00:10Z"}, "times are not the seal's"),
        ({"exporter": {"name": "voice-lab corpus-import", "version": "0",
                       "exported_at": "2026-09-01T00:00:00Z"}}, "request, the first sample"),
        ({"capture_device": {**MAC_DEVICE.model_dump(), "backend": "portaudio"}}, "Core Audio"),
        ({"subject_id": "synthetic-subject"}, "a take is of cole-self"),
    ],
)  # fmt: skip
def test_each_manifest_rule_is_checked(package: Path, fields: dict[str, Any], match: str) -> None:
    reseal_import(package, manifest=fields)
    with pytest.raises((ContractError, SecurityError), match=match):
        validate_take_import_package(package)


def test_a_manifests_prompt_must_be_the_reservations(libraries: Libraries, package: Path) -> None:
    other = libraries.catalog[1].suite.prompts[5]
    reseal_import(
        package,
        manifest={
            "prompt": other.model_dump(mode="json"),
            "prompt_sha256": canonical_sha256(other),
        },
    )
    with pytest.raises(ContractError, match="not the reservation's"):
        validate_take_import_package(package)


def test_links_extra_files_and_partial_packages_are_refused(package: Path, tmp_path: Path) -> None:
    writable(package)
    (package / "notes.txt").write_bytes(b"undeclared\n")
    with pytest.raises(SecurityError, match="undeclared=\\['notes.txt'\\]"):
        validate_take_import_package(package)
    (package / "notes.txt").unlink()
    (package / "intake-receipt.json").write_bytes(b"{}\n")
    with pytest.raises(SecurityError, match="intake-receipt.json"):
        validate_take_import_package(package)
    (package / "intake-receipt.json").unlink()
    elsewhere = tmp_path / "seal.json"
    os.rename(package / "seal.json", elsewhere)
    (package / "seal.json").symlink_to(elsewhere)
    with pytest.raises(SecurityError):
        validate_take_import_package(package)
    (package / "seal.json").unlink()
    os.link(elsewhere, package / "seal.json")
    with pytest.raises(SecurityError, match="hard-linked"):
        validate_take_import_package(package)
    (package / "seal.json").unlink()
    os.rename(elsewhere, package / "seal.json")
    (package / "READY").unlink()
    with pytest.raises(ContractError, match="no READY marker"):
        validate_take_import_package(package)


def test_the_take_must_be_from_a_umik_1(libraries: Libraries) -> None:
    mac = libraries.mac
    reference, snapshot = libraries.catalog
    group = mac.store.open_group(
        reference=reference,
        suite=snapshot.suite,
        subject_id=COLE_SUBJECT,
        capture_device=MAC_DEVICE.model_copy(update={"display_name": "MacBook Air Microphone"}),
        capture_host="coles-mac-synthetic",
    ).group_id
    take = mac.save(mac.sealed(group, spoken_ids(libraries.catalog)[0]))
    with pytest.raises(ContractError, match="UMIK-1"):
        build(libraries, take, "built-in")
    assert not any(libraries.work.iterdir())  # nothing is left behind


def test_the_mac_state_holds_its_own_rules() -> None:
    frames = RecordingFrameRange(source_frames=100, start_frame=0, end_frame=100)
    base: dict[str, Any] = {
        "source_take_id": "take-" + "1" * 64,
        "decision": "rejected",
        "decision_revision": 1,
        "reason": "Noise",
        "decided_at": NOW,
        "restore_decision": "draft",
        "frame_range": frames,
        "edit_revision": 0,
    }
    assert MacTakeState.model_validate(base).trimmed is False
    for change in (
        {"reason": None},
        {"restore_decision": None},
        {"decision": "kept"},
        {"decision": "draft", "decision_revision": 0, "reason": None, "restore_decision": None},
        {"frame_range": RecordingFrameRange(source_frames=100, start_frame=1, end_frame=100)},
    ):
        with pytest.raises(ValueError):
            MacTakeState.model_validate(base | change)


# -- On the PC ---------------------------------------------------------------------------------


def test_an_imported_take_is_published_and_its_record_rebuilt_from_raw(
    libraries: Libraries, package: Path
) -> None:
    pc = libraries.pc
    to_incoming(pc, package)
    summary = publish_pending(pc, min_free_bytes=0, clock=lambda: NOW)
    manifest = manifest_of(package)
    assert [receipt.session_id for receipt in summary.accepted] == [manifest.take_id]
    assert summary.takes_recorded == [manifest.take_id]
    raw = pc.directory("raw") / summary.accepted[0].manifest_sha256
    assert {stat.S_IMODE(child.stat().st_mode) for child in raw.iterdir()} == {0o400}
    published = validate_package(raw, allow_intake_receipt=True)
    assert isinstance(published, ValidatedTakeImport) and published.manifest == manifest
    path = state_path(pc, manifest.take_id)
    before = path.read_bytes()
    assert b'"origin":{"kind":"imported"' in before
    path.unlink()
    states, recorded = record_takes(pc)
    assert recorded == (manifest.take_id,) and path.read_bytes() == before
    assert states[0].decision.decided_at == NOW  # the PC's acceptance: a Save has no time
    # A take a client uploaded has no origin, and its record's bytes are as before D-053.
    uploaded = make_take(libraries.work / "uploaded", catalog=libraries.catalog, seed=9)
    shutil.copytree(libraries.work / "uploaded", pc.directory("incoming") / uploaded.session_id)
    publish_pending(pc, min_free_bytes=0, clock=lambda: NOW)
    plain = read_take_state(pc, uploaded.session_id)
    assert plain is not None and plain.origin is None
    assert b'"origin"' not in state_path(pc, uploaded.session_id).read_bytes()
    assert CorpusTakeState.model_validate_json(state_path(pc, uploaded.session_id).read_bytes())


def test_a_contradicted_or_uncounting_catalog_is_refused_and_a_missing_one_deferred(
    libraries: Libraries,
) -> None:
    mac, pc = libraries.mac, libraries.pc
    reference = libraries.catalog[0]
    group = mac.v2_group(libraries.catalog)
    ids = spoken_ids(libraries.catalog)
    other_sha = "e" * 64
    contradicting = reference.model_copy(
        update={
            "catalog_sha256": other_sha,
            "snapshot_path": reference.snapshot_path.replace(reference.catalog_sha256, other_sha),
        }
    )
    later = reference.model_copy(update={"catalog_version": 7})
    room = next(p for p in released_pilot_v2().prompts if p.category == "room-tone")
    room_tone = mac.save(mac.sealed(mac.v1_group(released_pilot_v2()), room.prompt_id))
    packages = {
        "contradicted": build(
            libraries, mac.save(mac.sealed(group, ids[0])), "contradicted", catalog=contradicting
        ),
        "later": build(libraries, mac.save(mac.sealed(group, ids[1])), "later", catalog=later),
        "room": build(libraries, room_tone, "room"),
    }
    for directory in packages.values():
        to_incoming(pc, directory)
    summary = publish_pending(pc, min_free_bytes=0)
    assert summary.accepted == []
    refused = {record.session_id: record.error.split(":")[0] for record in summary.refused}
    assert refused == {
        manifest_of(packages["contradicted"]).take_id: "take_catalog_invalid",
        manifest_of(packages["room"]).take_id: "take_prompt_not_in_catalog",
    }
    [deferred] = summary.deferred
    assert deferred["session"] == manifest_of(packages["later"]).take_id
    assert deferred["error"].startswith("take_catalog_missing: ")
    later_id = manifest_of(packages["later"]).take_id
    assert (pc.directory("incoming") / later_id).is_dir() and read_refusal(pc, later_id) is None


def test_a_diagnostic_root_refuses_an_imported_take(libraries: Libraries, package: Path) -> None:
    pc, _marker = initialize_diagnostic_root(
        libraries.work.parent / "diagnostic" / "lab", subject_id=f"{DIAGNOSTIC_SUBJECT_PREFIX}one"
    )
    to_incoming(pc, package)
    [refusal] = publish_pending(pc, min_free_bytes=0).refused
    assert refusal.error.startswith("take_diagnostic_root: ")


def test_the_same_audio_as_a_published_pc_take_is_a_collision(libraries: Libraries) -> None:
    pc, mac = libraries.pc, libraries.mac
    # The PC's take: corpus_take_fixtures' default tone (0.25 s, 220 Hz, seed 0).
    uploaded = make_take(libraries.work / "uploaded", catalog=libraries.catalog)
    shutil.copytree(libraries.work / "uploaded", pc.directory("incoming") / uploaded.session_id)
    assert len(publish_pending(pc, min_free_bytes=0).accepted) == 1
    # The Swift take of the very same samples.
    swift = mac.save(
        mac.sealed(mac.v2_group(libraries.catalog), spoken_ids(libraries.catalog)[0],
                   frequency=220.0, seed=0)
    )  # fmt: skip
    package = build(libraries, swift, "same-audio")
    assert manifest_of(package).take_id == uploaded.session_id
    to_incoming(pc, package)
    [refusal] = publish_pending(pc, min_free_bytes=0).refused
    assert "session ID collision" in refusal.error
    state = read_take_state(pc, uploaded.session_id)
    assert state is not None and state.origin is None


def test_the_vault_binds_an_imported_raw_take(libraries: Libraries, package: Path) -> None:
    to_incoming(libraries.pc, package)
    [receipt] = publish_pending(libraries.pc, min_free_bytes=0).accepted
    items = {
        item.path: item
        for item in RawPackageSource().items(libraries.pc)
        if isinstance(item, ReplicationItem)
    }
    raw = f"raw/{receipt.manifest_sha256}"
    manifest = manifest_of(package)
    assert set(items) == {
        f"{raw}/{name}" for name in (*TAKE_IMPORT_FILES, "intake-qc.json", "intake-receipt.json")
    }
    audio = items[f"{raw}/{AUDIO_NAME}"]
    assert (audio.expected_sha256, audio.expected_size) == (
        manifest.audio.sha256,
        manifest.audio.size_bytes,
    )
    for name, record in manifest.records.by_name().items():
        item = items[f"{raw}/{name}"]
        assert (item.expected_sha256, item.expected_size) == (record.sha256, record.size_bytes)


# -- The exporter --------------------------------------------------------------------------------


def test_a_changed_mac_state_keeps_its_package_and_is_reported(libraries: Libraries) -> None:
    mac = libraries.mac
    draft = mac.sealed(mac.v2_group(libraries.catalog), spoken_ids(libraries.catalog)[0])
    first = plan_export(mac.layout, libraries.view, clock=lambda: NOW)
    mac.decide(draft, "reject", "Changed my mind")
    second = plan_export(mac.layout, libraries.view, clock=lambda: NOW)
    assert second.plan_sha256 == first.plan_sha256
    assert second.state_changed == (pc_take_id(draft),)
    assert second.report(mac.layout)["mac_state_changed_since_packaged"] == [pc_take_id(draft)]


def test_damaged_duplicate_foreign_and_legacy_takes_are_skipped(libraries: Libraries) -> None:
    mac = libraries.mac
    ids = spoken_ids(libraries.catalog)
    group = mac.v2_group(libraries.catalog)
    good = mac.save(mac.sealed(group, ids[0]))
    twin = mac.save(mac.sealed(group, ids[1], frequency=300.0 + 7.0, seed=1))
    assert twin.seal is not None and good.seal is not None
    assert twin.seal.original_sha256 == good.seal.original_sha256
    damaged = mac.save(mac.sealed(group, ids[2]))
    audio = mac.layout.root / damaged.reservation.original_path
    data = bytearray(audio.read_bytes())
    data[-1] ^= 0x01
    audio.write_bytes(bytes(data))
    broken = mac.sealed(group, ids[3])
    head = mac.layout.root / "provenance/recording-takes" / broken.reservation.take_id / "head.json"
    head.write_bytes(b"{}\n")
    foreign = mac.save(
        mac.sealed(mac.v1_group(released_pilot_v2(), subject_id="synthetic"), ids[4])
    )
    legacy = mac.layout.directory("tmp") / "capture-desktop-1"
    legacy.mkdir()
    (mac.layout.directory("tmp") / "capture-desktop-1.rejected").mkdir()
    planning = plan_export(mac.layout, libraries.view, clock=lambda: NOW)
    assert planning.plan is not None
    assert [entry.source_take_id for entry in planning.plan.packages] == [good.reservation.take_id]
    reasons = {item.source: item.reason for item in planning.skipped}
    assert reasons == {
        twin.reservation.take_id: "duplicate-audio",
        damaged.reservation.take_id: "damaged",
        broken.reservation.take_id: "damaged",
        foreign.reservation.take_id: "other-subject",
        "tmp/capture-desktop-1": "legacy-capture-session",
    }
    assert not any(path.name.startswith(".") for path in (mac.layout.root / EXPORT_ROOT).iterdir())


def test_a_library_without_takes_writes_no_plan(libraries: Libraries) -> None:
    planning = plan_export(libraries.mac.layout, libraries.view)
    assert planning.plan is None and planning.plan_sha256 is None
    assert not (libraries.mac.layout.root / "provenance/host-transfers").exists()


def test_a_catalog_view_must_be_the_version_it_names(libraries: Libraries) -> None:
    view = libraries.view
    catalog_from_view(view)
    suite = view.suite.model_copy(update={"prompts": view.suite.prompts[:-1]})
    with pytest.raises(ContractError, match="not the version it names"):
        catalog_from_view(view.model_copy(update={"suite": suite}))
    with pytest.raises(ContractError, match="not cole-self"):
        catalog_from_view(view.model_copy(update={"subject_id": "synthetic"}))


def planned(libraries: Libraries) -> str:
    mac = libraries.mac
    mac.save(mac.sealed(mac.v2_group(libraries.catalog), spoken_ids(libraries.catalog)[0]))
    mac.save(mac.sealed(mac.v2_group(libraries.catalog), spoken_ids(libraries.catalog)[1]))
    planning = plan_export(mac.layout, libraries.view, clock=lambda: NOW)
    assert planning.plan_sha256 is not None
    return planning.plan_sha256


def test_approval_names_a_plan_on_this_mac_once(libraries: Libraries) -> None:
    mac = libraries.mac.layout
    with pytest.raises(ContractError, match="no export plan"):
        approve_plan(mac, "0" * 64, approved_by="Cole", statement="Yes.", now=APPROVAL_NOW)
    digest = planned(libraries)
    first, written = approve_plan(
        mac, digest, approved_by="Cole", statement="Yes.", now=APPROVAL_NOW
    )
    again, rewritten = approve_plan(
        mac, digest, approved_by="Someone", statement="Other.", now=APPROVAL_NOW
    )
    assert written and not rewritten and again == first
    assert classify(str(approval_path(mac, digest).relative_to(mac.root))).rule.name == (
        "host-transfers"
    )


def test_send_uploads_only_the_approved_packages_as_planned(
    libraries: Libraries, tmp_path: Path
) -> None:
    mac = libraries.mac.layout
    digest = planned(libraries)
    approve_plan(mac, digest, approved_by="Cole", statement="Yes.", now=APPROVAL_NOW)
    plan = read_plan(mac, digest)
    first, second = plan.packages
    writable(package_path(mac, first.take_id))
    flip_audio(package_path(mac, first.take_id))
    with serving(libraries.pc, tmp_path) as port:
        results = send(libraries, port, digest)
    assert [(result.take_id, result.status) for result in results] == [
        (first.take_id, "invalid"),
        (second.take_id, "accepted"),
    ]
    sessions = {entry.session_id for entry, _ in HomeHostLedger(mac).entries()}
    assert sessions == {second.take_id}
    assert read_take_state(libraries.pc, first.take_id) is None


def test_a_take_the_pc_defers_is_collected_by_the_next_send(
    libraries: Libraries, tmp_path: Path
) -> None:
    mac = libraries.mac.layout
    digest = planned(libraries)
    approve_plan(mac, digest, approved_by="Cole", statement="Yes.", now=APPROVAL_NOW)
    with serving(libraries.pc, tmp_path) as port:
        # The publisher does not run while this send waits: both are deferred.
        waiting = send_plan(
            mac,
            digest,
            client=HomeHostClient.loopback_for_tests(port, mint),
            clock=lambda: APPROVAL_NOW,
            wait_seconds=0.0,
        )
        assert [result.status for result in waiting] == ["deferred", "deferred"]
        publish_pending(libraries.pc, min_free_bytes=0)
        collected = send(libraries, port, digest)
    assert [(result.status, result.before) for result in collected] == [("accepted", False)] * 2
    events = [entry.event for entry, _ in HomeHostLedger(mac).entries()]
    assert events.count("upload-completed") == 2


def test_send_refuses_an_approval_for_another_destination(
    libraries: Libraries, tmp_path: Path
) -> None:
    mac = libraries.mac.layout
    digest = planned(libraries)
    approve_plan(mac, digest, approved_by="Cole", statement="Yes.", now=APPROVAL_NOW)
    bind_approval(
        mac, make_approval(approval_id="pc-home-host-2", node_stable_id="nOTHERNODE1CNTRL")
    )
    with serving(libraries.pc, tmp_path) as port, pytest.raises(SecurityError, match="another"):
        send(libraries, port, digest)
    assert HomeHostLedger(mac).entries() == []


# -- The CLI ---------------------------------------------------------------------------------------


def run(*argv: str) -> tuple[int, Any]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.main(list(argv))
    printed = out.getvalue()
    return code, json.loads(printed) if printed.strip() else None


def test_the_owners_commands(
    libraries: Libraries, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mac = libraries.mac
    mac.save(mac.sealed(mac.v2_group(libraries.catalog), spoken_ids(libraries.catalog)[0]))
    root = str(mac.layout.root)
    monkeypatch.setattr(cli_corpus_import, "utc_now", lambda: APPROVAL_NOW)
    with serving(libraries.pc, tmp_path) as port:
        monkeypatch.setattr(
            cli_corpus_import,
            "home_host_client",
            lambda _layout, _approval, _bearer: HomeHostClient.loopback_for_tests(port, mint),
        )
        code, report = run("corpus-import", "plan", "--asset-root", root, "--catalog-from-pc")
        assert code == 0 and report["packages"] == 1
        view = tmp_path / "catalog.json"
        view.write_bytes(canonical_json_bytes(libraries.view))
        code, again = run("corpus-import", "plan", "--asset-root", root, "--catalog", str(view))
        assert code == 0 and again["plan_sha256"] == report["plan_sha256"]
        digest = report["plan_sha256"]
        approve = ("corpus-import", "approve", "--asset-root", root, "--plan", digest,
                   "--approved-by", "Cole", "--statement", "My takes, to my PC.")  # fmt: skip
        assert run(*approve)[0] == 2
        assert not approval_path(mac.layout, digest).exists()
        code, approved = run(*approve, "--i-approve-this-transfer")
        assert code == 0 and approved["approved_now"] is True
        send_args = ("corpus-import", "send", "--asset-root", root, "--plan", digest)
        code, sent = run(*send_args, "--wait", "0")
        assert code == 0 and sent["counts"] == {"deferred": 1}
        publish_pending(libraries.pc, min_free_bytes=0)
        code, sent = run(*send_args)
        assert code == 0 and sent["counts"] == {"accepted": 1}


# -- Classification ------------------------------------------------------------------------------


def test_new_paths_are_classified() -> None:
    sha = "a" * 64
    take = f"take-{sha}"
    for path in (
        f"tmp/corpus-import/{take}/original.wav",
        f"tmp/corpus-import/{take}/seal.json",
        f"tmp/corpus-import/.{take}.building-0123456789abcdef/manifest.json",
    ):
        rule = classify(path).rule
        assert (rule.name, rule.disposition) == ("corpus-import-packages", "mac-only"), path
    for path in (
        f"provenance/host-transfers/corpus-import-{sha}.plan.json",
        f"provenance/host-transfers/corpus-import-{sha}.approval.json",
    ):
        rule = classify(path).rule
        assert (rule.name, rule.disposition, rule.mode) == ("host-transfers", "records", 0o400)
    temporary = classify(
        f"provenance/host-transfers/.corpus-import-{sha}.plan.json.tmp-0123456789abcdef"
    )
    assert temporary.rule.disposition == "mac-only"
    for name in TAKE_IMPORT_FILES:
        rule = classify(f"raw/{sha}/{name}").rule
        assert (rule.name, rule.disposition, rule.mode) == ("raw-packages", "recordings", 0o400)


def test_every_file_step_9_writes_is_classified(libraries: Libraries, tmp_path: Path) -> None:
    record_mac_takes(libraries)
    mac = libraries.mac.layout
    planning = plan_export(mac, libraries.view, clock=lambda: NOW)
    assert planning.plan_sha256 is not None
    approve_plan(mac, planning.plan_sha256, approved_by="Cole", statement="Yes.", now=APPROVAL_NOW)
    with serving(libraries.pc, tmp_path) as port:
        assert {result.status for result in send(libraries, port, planning.plan_sha256)} == {
            "accepted"
        }
    for layout in (mac, libraries.pc):
        for path in layout.root.rglob("*"):
            if path.is_file():
                classify(path.relative_to(layout.root).as_posix())  # raises when unclassified


def test_the_plan_and_approval_contracts_hold_their_rules(libraries: Libraries) -> None:
    digest = planned(libraries)
    plan = read_plan(libraries.mac.layout, digest)
    with pytest.raises(ValueError, match="by take id"):
        CorpusImportPlan.model_validate(
            plan.model_dump() | {"packages": tuple(reversed(plan.packages))}
        )
    with pytest.raises(ValueError, match="sum"):
        CorpusImportPlan.model_validate(plan.model_dump() | {"total_bytes": 1})
    with pytest.raises(ValueError, match="cole-self"):
        CorpusImportPlan.model_validate(plan.model_dump() | {"subject_id": "synthetic"})
    approval, _ = approve_plan(
        libraries.mac.layout, digest, approved_by="Cole", statement="Yes.", now=APPROVAL_NOW
    )
    with pytest.raises(ValueError):
        CorpusImportApproval.model_validate(approval.model_dump() | {"statement": "  "})


# -- Every other rule ------------------------------------------------------------------------------


def rechain(package: Path, change_group: Any = None, change_reservation: Any = None) -> None:
    """Change the group and/or the reservation, and re-bind the chain the Mac binds: the
    reservation embeds the group, and the seal names the reservation's digest."""
    group = record_json(package, "group.json")
    if change_group is not None:
        change_group(group)
    reservation = record_json(package, "reservation.json")
    reservation["group"] = group
    if change_reservation is not None:
        change_reservation(reservation)
    seal = record_json(package, "seal.json")
    seal["reservation_sha256"] = canonical_sha256(reservation)
    reseal_import(
        package,
        files={
            "group.json": json_record(group),
            "reservation.json": json_record(reservation),
            "seal.json": json_record(seal),
        },
    )


def another_subject(document: dict[str, Any]) -> None:
    document["subject_id"] = "synthetic-subject"


def fewer_frames_allowed(document: dict[str, Any]) -> None:
    document["maximum_frames"] = 10


def another_generation(document: dict[str, Any]) -> None:
    document["process_generation"] = "another-generation"


def another_device_uid(document: dict[str, Any]) -> None:
    document["continuity"]["device_uid"] = "another-device"


def decided_after_export(document: dict[str, Any]) -> None:
    document["decided_at"] = "2027-01-01T00:00:00Z"


@pytest.mark.parametrize(
    ("tamper", "error", "match"),
    [
        (lambda package: rechain(package, another_subject), ContractError, "another subject"),
        (
            lambda package: rechain(package, change_reservation=fewer_frames_allowed),
            ContractError,
            "maximum duration",
        ),
        (
            lambda package: changed_record(package, "seal.json", another_generation),
            SecurityError,
            "not of this reservation's capture",
        ),
        (
            lambda package: changed_record(package, "seal.json", another_device_uid),
            SecurityError,
            "the seal's device is not the group's",
        ),
        (
            lambda package: changed_record(package, "mac-state.json", decided_after_export),
            ContractError,
            "between the seal and the export",
        ),
    ],
)
def test_records_bound_to_each_other_but_not_to_the_take_are_refused(
    libraries: Libraries, tamper: Any, error: type[Exception], match: str
) -> None:
    mac = libraries.mac
    take = mac.decide(
        mac.sealed(mac.v2_group(libraries.catalog), spoken_ids(libraries.catalog)[0]),
        "reject",
        "Noise",
    )
    package = build(libraries, take, "rejected")
    writable(package)
    tamper(package)
    with pytest.raises(error, match=match):
        validate_take_import_package(package)


def test_an_export_before_the_seal_and_a_format_that_disagrees_are_refused(
    libraries: Libraries, kept: RecordingTake, package: Path
) -> None:
    assert kept.seal is not None
    early = kept.seal.first_sample_at + timedelta(milliseconds=1)
    exporter = {**record_json(package, "manifest.json")["exporter"]}
    exporter["exported_at"] = early.isoformat().replace("+00:00", "Z")
    reseal_import(package, manifest={"exporter": exporter})
    with pytest.raises(ContractError, match="exported before it was sealed"):
        validate_take_import_package(package)
    other = build(libraries, kept, "other")
    audio = record_json(other, "manifest.json")["audio"]
    audio["audio"]["duration_seconds"] += 0.0005
    reseal_import(other, manifest={"audio": audio})
    with pytest.raises(ContractError, match="format evidence disagrees"):
        validate_take_import_package(other)


@pytest.mark.parametrize(
    ("change", "error", "match"),
    [
        ({"ready": {"manifest_sha256": "1" * 64}}, SecurityError, "READY manifest digest"),
        ({"ready": {"qc_report_sha256": "1" * 64}}, SecurityError, "READY QC digest"),
        ({"qc": {"session_id": "take-" + "1" * 64}}, ContractError, "take IDs disagree"),
        ({"qc": {"manifest_sha256": "1" * 64}}, SecurityError, "not bound to the imported"),
        ({"qc": {"metric_path": "other.wav"}}, ContractError, "QC metrics must cover"),
    ],
)
def test_ready_and_qc_bind_the_manifest(
    package: Path, change: dict[str, dict[str, Any]], error: type[Exception], match: str
) -> None:
    writable(package)
    report = record_json(package, "qc.json") | change.get("qc", {})
    if "metric_path" in report:
        report["metrics"][0]["path"] = report.pop("metric_path")
    report_bytes = json_record(report)
    (package / "qc.json").write_bytes(report_bytes)
    marker = ReadyMarker.model_validate_json((package / "READY").read_bytes())
    fields = {"qc_report_sha256": hashlib.sha256(report_bytes).hexdigest()} | change.get(
        "ready", {}
    )
    (package / "READY").unlink()
    write_canonical_json(package / "READY", marker.model_copy(update=fields))
    with pytest.raises(error, match=match):
        validate_take_import_package(package)


def test_the_manifest_holds_its_own_rules(package: Path) -> None:
    document = record_json(package, "manifest.json")
    CorpusTakeImportManifest.model_validate_json(json.dumps(document))
    physical = {**document["catalog"], "purpose": "physical-test"}
    for change, match in (
        ({"take_id": "take-" + "1" * 64}, "the take id"),
        ({"prompt_sha256": "1" * 64}, "prompt_sha256"),
        ({"catalog": physical}, "physical-test"),
        ({"client": {**document["client"], "name": "voice-lab-desktop"}}, "Cole Voice Recorder"),
        ({"requested_at": "2026-09-12T12:00:00"}, "explicit timezone"),
    ):
        with pytest.raises(ValueError, match=match):
            CorpusTakeImportManifest.model_validate_json(json.dumps(document | change))


def test_plans_and_approvals_are_read_as_their_digests_pin_them(libraries: Libraries) -> None:
    mac = libraries.mac.layout
    digest = planned(libraries)
    path = plan_path(mac, digest)
    original = path.read_bytes()
    os.chmod(path, 0o600)
    path.write_bytes(original.replace(b'"total_bytes":', b'"total_bytes": ', 1))
    with pytest.raises(SecurityError, match="not the one its name pins"):
        read_plan(mac, digest)
    path.write_bytes(original)
    # Another plan (one more take) and an approval copied to the wrong name.
    other_take = libraries.mac.save(
        libraries.mac.sealed(
            libraries.mac.v2_group(libraries.catalog), spoken_ids(libraries.catalog)[8]
        )
    )
    other = plan_export(mac, libraries.view, clock=lambda: NOW)
    assert other.plan_sha256 not in {None, digest} and other.built == (pc_take_id(other_take),)
    assert other.plan_sha256 is not None
    approve_plan(mac, digest, approved_by="Cole", statement="Yes.", now=APPROVAL_NOW)
    shutil.copyfile(approval_path(mac, digest), approval_path(mac, other.plan_sha256))
    with pytest.raises(SecurityError, match="names another plan"):
        read_approval(mac, other.plan_sha256)
    # Another record where the plan's name is: the plan is never written over it.
    taken = plan_path(mac, other.plan_sha256)
    os.chmod(taken, 0o600)
    taken.write_bytes(b"{}\n")
    with pytest.raises(SecurityError, match="another record is already at"):
        plan_export(mac, libraries.view, clock=lambda: NOW + timedelta(hours=1))


def test_catalog_views_from_a_file_or_a_pc_without_one(
    libraries: Libraries, tmp_path: Path
) -> None:
    view = libraries.view
    reference = view.reference.model_copy(update={"purpose": "physical-test"})
    with pytest.raises(ContractError, match="physical-test"):
        catalog_from_view(view.model_copy(update={"reference": reference}))
    saved = tmp_path / "view.json"
    saved.write_bytes(b'{"schema_version": "something else"}\n')
    with pytest.raises(ContractError, match="not a catalog view"):
        read_catalog_view(saved)
    bare = AssetLayout.initialize(tmp_path / "bare" / "lab")
    with serving(bare, tmp_path) as port, pytest.raises(UploadError, match="not available"):
        fetch_catalog_view(
            HomeHostClient.loopback_for_tests(port, mint), standing_approval(libraries.mac.layout)
        )


def test_uncounted_prompts_unsealed_builds_and_existing_packages(libraries: Libraries) -> None:
    mac = libraries.mac
    uncounted = mac.save(mac.sealed(mac.v1_group(synthetic_catalog(2)), "synthetic-0001"))
    planning = plan_export(mac.layout, libraries.view, clock=lambda: NOW)
    assert [(item.reason, item.source) for item in planning.skipped] == [
        ("prompt-not-in-catalog", uncounted.reservation.take_id)
    ]
    draft = mac.store.get_take(
        mac.reserve(mac.v2_group(libraries.catalog), spoken_ids(libraries.catalog)[0]).take_id
    )
    with pytest.raises(ContractError, match="unsealed"):
        build(libraries, draft, "unsealed")
    kept_take = mac.save(
        mac.sealed(mac.v2_group(libraries.catalog), spoken_ids(libraries.catalog)[1])
    )
    build(libraries, kept_take, "kept")
    with pytest.raises(FileExistsError):
        build(libraries, kept_take, "kept")


def test_a_group_record_that_is_not_the_takes_is_damaged(libraries: Libraries) -> None:
    mac = libraries.mac
    take = mac.save(mac.sealed(mac.v2_group(libraries.catalog), spoken_ids(libraries.catalog)[0]))
    path = (
        mac.layout.root / "provenance/recording-groups" / f"{take.reservation.group.group_id}.json"
    )
    group = json.loads(path.read_bytes())
    group["setup_description"] = "edited by hand"
    path.write_bytes(canonical_json_bytes(group) + b"\n")
    planning = plan_export(mac.layout, libraries.view, clock=lambda: NOW)
    [skipped] = planning.skipped
    assert skipped.reason == "damaged" and "is not the record the take's head binds" in (
        skipped.detail
    )
    assert not (mac.layout.root / EXPORT_ROOT / pc_take_id(take)).exists()


def test_a_standing_approval_without_captures_approves_and_sends_nothing(
    libraries: Libraries,
) -> None:
    mac = libraries.mac.layout
    digest = planned(libraries)
    bind_approval(mac, make_approval(approval_id="pc-measurements", classes=("measurement",)))
    with pytest.raises(SecurityError, match="does not cover capture packages"):
        approve_plan(mac, digest, approved_by="Cole", statement="Yes.", now=APPROVAL_NOW)


def test_a_package_rebuilt_after_approval_is_not_sent_and_a_collision_is_refused(
    libraries: Libraries, tmp_path: Path
) -> None:
    mac, pc = libraries.mac, libraries.pc
    ids = spoken_ids(libraries.catalog)
    group = mac.v2_group(libraries.catalog)
    rebuilt = mac.save(mac.sealed(group, ids[0]))
    colliding = mac.save(mac.sealed(group, ids[1], frequency=220.0, seed=0))
    digest = plan_export(mac.layout, libraries.view, clock=lambda: NOW).plan_sha256
    assert digest is not None
    approve_plan(mac.layout, digest, approved_by="Cole", statement="Yes.", now=APPROVAL_NOW)
    # The PC already holds the colliding audio, as a take a client uploaded.
    uploaded = make_take(libraries.work / "uploaded", catalog=libraries.catalog)
    assert uploaded.session_id == pc_take_id(colliding)
    shutil.copytree(libraries.work / "uploaded", pc.directory("incoming") / uploaded.session_id)
    assert len(publish_pending(pc, min_free_bytes=0).accepted) == 1
    # The other package is rebuilt (removed and planned again): another manifest.
    package = package_path(mac.layout, pc_take_id(rebuilt))
    writable(package)
    shutil.rmtree(package)
    plan_export(mac.layout, libraries.view, clock=lambda: NOW + timedelta(hours=1))
    with serving(pc, tmp_path) as port:
        results = {result.take_id: result for result in send(libraries, port, digest)}
    assert results[pc_take_id(rebuilt)].status == "invalid"
    assert "not the one the approved plan lists" in str(results[pc_take_id(rebuilt)].detail)
    refused = results[pc_take_id(colliding)]
    assert refused.status == "refused" and "session ID collision" in str(refused.detail)


def test_a_broken_package_on_the_mac_is_reported_until_it_is_removed(
    libraries: Libraries,
) -> None:
    mac = libraries.mac
    take = mac.save(mac.sealed(mac.v2_group(libraries.catalog), spoken_ids(libraries.catalog)[0]))
    first = plan_export(mac.layout, libraries.view, clock=lambda: NOW)
    package = package_path(mac.layout, pc_take_id(take))
    writable(package)
    flip_audio(package)
    broken = plan_export(mac.layout, libraries.view, clock=lambda: NOW)
    [skipped] = broken.skipped
    assert (skipped.reason, broken.plan) == ("damaged", None)
    assert f"remove {EXPORT_ROOT}/{pc_take_id(take)} to package the take again" in skipped.detail
    shutil.rmtree(package)
    rebuilt = plan_export(mac.layout, libraries.view, clock=lambda: NOW)
    assert rebuilt.built == (pc_take_id(take),) and rebuilt.plan_sha256 == first.plan_sha256
