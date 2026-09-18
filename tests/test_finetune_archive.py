"""``voice-lab finetune archive`` and ``restore-run``: a run leaves the Mac and comes back.

Scenario tests for the Step 11 fine-tune run archive. One synthetic library with one finished
stub run is bound to a local-directory vault that copies the opt-in ``training`` class, and
the whole round trip runs through the commands: archive (push, verify, ``archived.json``),
remove the local copy (``local-removal.json``, then the unlinks), the run's location in the
app's status, Speak and ``finetune synthesize`` refusing an archived run, and ``restore-run``
bringing the checkpoints back from the vault. Every refusal the two commands make is checked
as the operator sees it, together with the two facts a removal must never break: the next
generation still names the removed files, and restoring that generation reproduces the run.
All audio is synthetic, no training ever runs, and the Keychain is the dictionary in
``tests/vault_binding_fixtures.py``.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from vault_binding_fixtures import (
    ALL_CLASSES,
    LIBRARY_ID,
    NAMESPACE,
    SYNTHETIC_KEY_HEX,
    add_library_records,
    install_vault_seams,
    restore_permissions,
)
from vault_library_fixtures import RUN_FINISHED, add_finished_run

import cole_voice_lab.cli as cli_module
import cole_voice_lab.library_generations as generations_module
from cole_voice_lab.canonical import canonical_sha256
from cole_voice_lab.cli import main
from cole_voice_lab.finetune import (
    FINETUNE_ARCHIVE_NAME,
    FINETUNE_REMOVAL_NAME,
    FineTuneArchivedFile,
    FineTuneError,
    FineTuneRunArchive,
    FineTuneRunRemoval,
    FineTuneSummaryCache,
    finished_finetune_runs,
    resolve_speech_model,
    write_run_archive,
    write_run_removal,
)
from cole_voice_lab.library_generations import VaultRecords
from cole_voice_lab.models import utc_now
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_contracts import RecordingError, SynthesizeCommand
from cole_voice_lab.recording_finetune_jobs import (
    FineTuneJob,
    FineTuneJobHead,
    SpeechActiveMarker,
    SpeechWorkerRequest,
    run_speech_worker_request,
)
from cole_voice_lab.recording_takes import write_recording_evidence

RUN_ROOT = f"derived/finetune/{RUN_FINISHED}"
CHECKPOINT = f"{RUN_ROOT}/checkpoints/model_last.pt"
PRETRAINED = f"{RUN_ROOT}/checkpoints/pretrained_model.safetensors"
COPIED_CLASSES = tuple(name for name in ALL_CLASSES if name != "training")


@pytest.fixture(autouse=True)
def _no_ambient_asset_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COLE_VOICE_LAB_ASSET_ROOT", raising=False)


@pytest.fixture(autouse=True)
def _writable_tmp_path(tmp_path: Path) -> Iterator[None]:
    yield
    restore_permissions(tmp_path)


@dataclass
class Cli:
    """Runs ``voice-lab`` in this process and keeps every byte it printed."""

    capsys: pytest.CaptureFixture[str]

    def __call__(self, *arguments: str) -> tuple[int, str, str]:
        status = main(list(arguments))
        captured = self.capsys.readouterr()
        return status, captured.out, captured.err

    def json(self, *arguments: str) -> Any:
        status, out, err = self(*arguments)
        assert status == 0, err
        return json.loads(out)

    def refused(self, *arguments: str, code: str | None = None) -> str:
        status, out, err = self(*arguments)
        assert status == 2, out
        if code is not None:
            assert err.startswith(f"voice-lab: {code}: "), err
        return err


@dataclass
class Lab:
    """A library with one finished run, bound to a local-directory vault."""

    cli: Cli
    layout: AssetLayout
    runtime: Path
    vault_directory: Path
    tmp_path: Path

    @property
    def root(self) -> Path:
        return self.layout.root

    def archive(self, *extra: str) -> Any:
        return self.cli.json(
            "finetune", "archive", "--asset-root", str(self.root), "--run", RUN_FINISHED, *extra
        )

    def refuse_archive(self, *extra: str, code: str | None = None) -> str:
        return self.cli.refused(
            "finetune",
            "archive",
            "--asset-root",
            str(self.root),
            "--run",
            RUN_FINISHED,
            *extra,
            code=code,
        )

    def restore_run(self, *extra: str) -> Any:
        return self.cli.json(
            "finetune",
            "restore-run",
            "--asset-root",
            str(self.root),
            "--run",
            RUN_FINISHED,
            *extra,
        )

    def refuse_restore_run(self, *extra: str, code: str | None = None) -> str:
        return self.cli.refused(
            "finetune",
            "restore-run",
            "--asset-root",
            str(self.root),
            "--run",
            RUN_FINISHED,
            *extra,
            code=code,
        )

    def records(self) -> VaultRecords:
        return VaultRecords(self.layout)

    def manifest(self, generation: int) -> Any:
        manifest = self.records().read_manifest_copy(generation)
        assert manifest is not None, generation
        return manifest

    def entries(self, generation: int) -> dict[str, Any]:
        return {
            entry.path: entry
            for entry in self.manifest(generation).entries
            if entry.path.startswith(f"{RUN_ROOT}/")
        }

    def blob_file(self, digest: str) -> Path:
        return self.vault_directory / "blobs" / digest[:2] / digest

    def summary(self) -> Any:
        runs = finished_finetune_runs(self.layout)
        return next(run for run in runs if run.run_id == RUN_FINISHED)

    def run_file(self, relative: str) -> Path:
        return self.root / relative


def bind(lab: Lab, *classes: str) -> None:
    root = str(lab.root)
    lab.cli.json("vault", "init", "--asset-root", root, "--library-id", LIBRARY_ID)
    sheets = lab.tmp_path.resolve() / "sheets"
    sheets.mkdir(mode=0o700, exist_ok=True)
    lab.cli.json(
        "vault", "key", "create", "--asset-root", root, "--sheet-out", str(sheets / "sheet.txt")
    )
    lab.cli.json(
        "vault",
        "bind",
        "--asset-root",
        root,
        "--backend",
        "local-directory",
        "--endpoint",
        str(lab.vault_directory),
        "--namespace",
        NAMESPACE,
        "--classes",
        *(classes or ALL_CLASSES),
    )


@pytest.fixture
def unbound(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> Lab:
    """A library with one finished run and no vault binding yet."""
    install_vault_seams(monkeypatch)
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    add_library_records(layout)
    runtime = tmp_path.resolve() / "runtime"
    add_finished_run(layout, runtime)
    vault_directory = tmp_path.resolve() / "vault"
    vault_directory.mkdir(mode=0o700)
    return Lab(Cli(capsys), layout, runtime, vault_directory, tmp_path)


@pytest.fixture
def lab(unbound: Lab) -> Lab:
    """The same library, bound to a vault that takes the opt-in ``training`` class."""
    bind(unbound)
    return unbound


# -- the round trip ----------------------------------------------------------------------------


def test_a_run_is_archived_verified_removed_and_brought_back(lab: Lab) -> None:
    checkpoint_bytes = lab.run_file(CHECKPOINT).read_bytes()
    assert lab.summary().location == "local" and lab.summary().in_vault is False

    archived = lab.archive()

    assert archived["run_id"] == RUN_FINISHED and archived["removed"] is None
    assert archived["archive_record"] == str(lab.run_file(f"{RUN_ROOT}/archived.json"))
    record = FineTuneRunArchive.model_validate_json(
        lab.run_file(f"{RUN_ROOT}/{FINETUNE_ARCHIVE_NAME}").read_bytes()
    )
    assert record.library_id == LIBRARY_ID and record.run_id == RUN_FINISHED
    assert {file.path for file in record.files} >= {
        "run.json",
        "checkpoints/model_last.pt",
        "checkpoints/pretrained_model.safetensors",
    }
    assert next(f for f in record.files if f.path.startswith("checkpoints/pretrained")).kind == (
        "pin"
    )
    assert os.stat(lab.run_file(f"{RUN_ROOT}/{FINETUNE_ARCHIVE_NAME}")).st_mode & 0o777 == 0o600
    assert lab.summary().location == "local" and lab.summary().in_vault is True

    removed = lab.archive("--remove-local-copy")["removed"]

    assert removed["files"] == [
        "checkpoints/model_last.pt",
        "checkpoints/pretrained_model.safetensors",
    ]
    # The pretrained file is a hard link to the runtime's weights: unlinking it frees nothing,
    # so only the checkpoint's own bytes count.
    assert removed["freed_bytes"] == len(checkpoint_bytes)
    assert not lab.run_file(CHECKPOINT).exists()
    assert not lab.run_file(PRETRAINED).exists()
    assert lab.run_file(f"{RUN_ROOT}/run.json").exists()
    assert lab.run_file(f"{RUN_ROOT}/dataset/dataset.json").exists()
    assert lab.run_file(f"{RUN_ROOT}/samples/base/samples.json").exists()
    assert lab.run_file(f"{RUN_ROOT}/checkpoints").is_dir()

    summary = lab.summary()
    assert (summary.location, summary.in_vault) == ("archived", True)

    with pytest.raises(FineTuneError) as speak:
        resolve_speech_model(lab.layout, "latest")
    assert speak.value.code == "FINETUNE.RUN_ARCHIVED"
    assert "restore-run --run" in str(speak.value)
    # Base speaks with the pinned weights and only borrows this run's reference clip.
    assert resolve_speech_model(lab.layout, "base").run is None

    assert "is archived" in lab.cli.refused(
        "finetune",
        "synthesize",
        "--asset-root",
        str(lab.root),
        "--run-id",
        RUN_FINISHED,
        "--runtime-root",
        str(lab.runtime),
        code="FINETUNE.RUN_ARCHIVED",
    )

    restored = lab.restore_run("--runtime-root", str(lab.runtime))

    assert [file["state"] for file in restored["restored"]] == ["written"]
    assert restored["restored_bytes"] == len(checkpoint_bytes)
    assert [pin["state"] for pin in restored["pins"]] == ["linked"]
    assert lab.run_file(CHECKPOINT).read_bytes() == checkpoint_bytes
    assert lab.run_file(PRETRAINED).exists()
    assert not lab.run_file(f"{RUN_ROOT}/{FINETUNE_REMOVAL_NAME}").exists()
    assert lab.run_file(f"{RUN_ROOT}/local-removal-restored-1.json").exists()

    back = lab.summary()
    assert (back.location, back.in_vault) == ("local", True)
    assert resolve_speech_model(lab.layout, "latest").run_id == RUN_FINISHED


def test_the_next_generation_still_names_the_removed_files_and_restores_the_run(
    lab: Lab, tmp_path: Path
) -> None:
    checkpoint_bytes = lab.run_file(CHECKPOINT).read_bytes()
    archived = lab.archive("--remove-local-copy")
    removed_generation = archived["removed"]["generation"]
    before = lab.entries(removed_generation)

    # A later push reads a library the run's checkpoints are gone from.
    later = lab.run_file("provenance/consents/later-consent.json")
    later.write_bytes(b'{"consent":"later"}\n')
    later.chmod(0o600)
    pushed = lab.cli.json("vault", "push", "--asset-root", str(lab.root))

    assert pushed["outcome"] == "pushed" and pushed["generation"] == removed_generation + 1
    assert pushed["waiting_paths"] == []
    after = lab.entries(pushed["generation"])
    # The removal record joins the run; nothing the removal took is dropped.
    assert set(after) == set(before) | {f"{RUN_ROOT}/{FINETUNE_REMOVAL_NAME}"}
    assert after[CHECKPOINT].chunks == before[CHECKPOINT].chunks
    assert after[PRETRAINED].kind == "pin"

    report = lab.cli.json("vault", "verify", "--asset-root", str(lab.root), "--path", RUN_ROOT)
    assert report["ok"] is True and report["pinned"] == 1
    assert report["checked"] == len(after)

    target = tmp_path.resolve() / "restored" / "assets"
    target.parent.mkdir(mode=0o700)
    lab.cli.json(
        "vault",
        "restore",
        "--asset-root",
        str(lab.root),
        "--target",
        str(target),
        "--accept-latest",
        "--runtime-root",
        str(lab.runtime),
    )

    assert (target / CHECKPOINT).read_bytes() == checkpoint_bytes
    assert (target / PRETRAINED).exists()
    assert (target / f"{RUN_ROOT}/{FINETUNE_ARCHIVE_NAME}").exists()
    assert (target / f"{RUN_ROOT}/{FINETUNE_REMOVAL_NAME}").exists()
    # The removal record travels with the run, so the restored run carries one although every
    # file it names is back: it is the files that say where a run is, and the restored run is
    # local, listed and able to speak. Nothing on a restored Mac could open a vault.
    restored_layout = AssetLayout.validate(target)
    summary = finished_finetune_runs(restored_layout)[0]
    assert (summary.run_id, summary.location, summary.in_vault) == (RUN_FINISHED, "local", True)
    assert resolve_speech_model(restored_layout, "latest").run_id == RUN_FINISHED

    # `restore-run` there finishes without a vault and keeps the record as history.
    finished = lab.cli.json(
        "finetune", "restore-run", "--asset-root", str(target), "--run", RUN_FINISHED
    )

    assert [file["state"] for file in finished["restored"]] == ["kept"]
    assert [pin["state"] for pin in finished["pins"]] == ["linked"]
    assert not (target / f"{RUN_ROOT}/{FINETUNE_REMOVAL_NAME}").exists()
    assert (target / f"{RUN_ROOT}/local-removal-restored-1.json").exists()
    assert finished_finetune_runs(restored_layout)[0].location == "local"


def test_a_removal_a_crash_interrupted_is_finished_by_either_command(lab: Lab) -> None:
    archived = lab.archive()
    generation = archived["generation"]
    entries = lab.entries(generation)
    checkpoint_bytes = lab.run_file(CHECKPOINT).read_bytes()

    # The record is written before the first unlink; a crash here leaves every file in place.
    removal = FineTuneRunRemoval(
        run_id=RUN_FINISHED,
        generation=generation,
        manifest_sha256=archived["manifest_sha256"],
        removed_at=utc_now(),
        files=(
            FineTuneArchivedFile(
                path="checkpoints/model_last.pt",
                sha256=entries[CHECKPOINT].sha256,
                size=entries[CHECKPOINT].size,
                kind="file",
            ),
        ),
    )
    write_run_removal(lab.layout, RUN_ROOT, removal)

    # Every file the record names is still there, so the run is still local and still speaks.
    assert lab.summary().location == "local"
    assert lab.run_file(CHECKPOINT).exists()
    assert resolve_speech_model(lab.layout, "latest").run_id == RUN_FINISHED

    # A restore of a run whose files never left is the files it finds, kept as they are.
    restored = lab.restore_run()

    assert [file["state"] for file in restored["restored"]] == ["kept"]
    assert lab.run_file(CHECKPOINT).read_bytes() == checkpoint_bytes
    assert lab.run_file(f"{RUN_ROOT}/local-removal-restored-1.json").exists()
    assert lab.summary().location == "local"


def test_a_second_removal_finishes_what_the_first_started(lab: Lab) -> None:
    lab.archive("--remove-local-copy")
    generation = lab.archive("--remove-local-copy")["removed"]["generation"]

    again = lab.archive("--remove-local-copy")["removed"]

    assert again["removal_record"] is None and again["generation"] == generation
    assert again["files"] == []
    assert lab.summary().location == "archived"


# -- refusals ----------------------------------------------------------------------------------


def test_a_library_with_no_vault_refuses_to_archive_anything(unbound: Lab) -> None:
    assert "vault bind" in unbound.refuse_archive(code="STORAGE.NOT_BOUND")
    assert not unbound.run_file(f"{RUN_ROOT}/{FINETUNE_ARCHIVE_NAME}").exists()


def test_a_vault_that_does_not_take_training_refuses_before_anything_is_read(
    unbound: Lab,
) -> None:
    bind(unbound, *COPIED_CLASSES)

    refusal = unbound.refuse_archive(code="STORAGE.NOT_APPROVED")

    assert "does not take fine-tune runs" in refusal and "training" in refusal
    assert not unbound.run_file(f"{RUN_ROOT}/{FINETUNE_ARCHIVE_NAME}").exists()
    assert list((unbound.vault_directory / "blobs").glob("*/*")) == []


def test_an_unfinished_run_is_never_archived(lab: Lab) -> None:
    run_path = lab.run_file(f"{RUN_ROOT}/run.json")
    run = json.loads(run_path.read_bytes())
    run["status"] = "running"
    del run["finished_at"]
    run_path.chmod(0o600)
    run_path.write_text(json.dumps(run))

    refusal = lab.refuse_archive(code="FINETUNE.NO_FINISHED_RUN")

    assert "is running, not finished" in refusal
    assert not lab.run_file(f"{RUN_ROOT}/{FINETUNE_ARCHIVE_NAME}").exists()


def test_an_unknown_run_is_named_rather_than_traced(lab: Lab) -> None:
    refusal = lab.cli.refused(
        "finetune",
        "archive",
        "--asset-root",
        str(lab.root),
        "--run",
        "run-that-is-not-there",
        code="FINETUNE.SOURCE_CHANGED",
    )

    assert "run.json" in refusal


@pytest.mark.parametrize("activity", ["job", "speak"])
def test_the_apps_gpu_work_holds_back_both_commands(lab: Lab, activity: str) -> None:
    if activity == "job":
        write_recording_evidence(
            lab.root / "provenance/finetune-jobs/head.json",
            FineTuneJobHead(
                job=FineTuneJob(
                    operation_id="finetune-2", request_sha256="1" * 64, queued_at=utc_now()
                )
            ),
            immutable=False,
        )
    else:
        write_recording_evidence(
            lab.root / "provenance/finetune-jobs/speech-active.json",
            SpeechActiveMarker(operation_id="speak-1", pid=os.getpid(), started_at=utc_now()),
            immutable=False,
        )

    assert lab.refuse_archive(code="FINETUNE.JOB_ACTIVE")
    assert lab.refuse_restore_run(code="FINETUNE.JOB_ACTIVE")
    assert not lab.run_file(f"{RUN_ROOT}/{FINETUNE_ARCHIVE_NAME}").exists()


def test_a_vault_copy_that_does_not_verify_archives_and_removes_nothing(lab: Lab) -> None:
    lab.archive()
    generation = lab.records().read_state(LIBRARY_ID).generation  # type: ignore[union-attr]
    entry = lab.entries(generation)[CHECKPOINT]
    lost = lab.blob_file(entry.chunks[0].sha256)
    lost.unlink()

    refusal = lab.refuse_archive("--remove-local-copy")

    assert "did not verify" in refusal and CHECKPOINT in refusal
    assert lab.run_file(CHECKPOINT).exists()
    assert not lab.run_file(f"{RUN_ROOT}/{FINETUNE_REMOVAL_NAME}").exists()


def test_a_missing_manifest_copy_blocks_the_removal_but_not_the_archive(lab: Lab) -> None:
    lab.archive()
    generation = lab.records().read_state(LIBRARY_ID).generation  # type: ignore[union-attr]
    copy = lab.records().manifest_copy_path(generation)
    copy.chmod(0o600)
    copy.unlink()

    refusal = lab.refuse_archive("--remove-local-copy")

    assert "keeps no manifest copy" in refusal
    assert lab.run_file(CHECKPOINT).exists()
    assert not lab.run_file(f"{RUN_ROOT}/{FINETUNE_REMOVAL_NAME}").exists()


def test_a_restore_refuses_a_run_nothing_was_removed_from(lab: Lab) -> None:
    lab.archive()

    refusal = lab.refuse_restore_run()

    assert "local-removal.json" in refusal


def test_a_restore_refuses_when_the_disk_cannot_hold_the_run(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab.archive("--remove-local-copy")
    monkeypatch.setattr(generations_module, "_free_bytes", lambda _path: 1)

    refusal = lab.refuse_restore_run(code="REPLICATION.RESTORE_REFUSED")

    assert "bytes free" in refusal
    assert not lab.run_file(CHECKPOINT).exists()
    assert lab.run_file(f"{RUN_ROOT}/{FINETUNE_REMOVAL_NAME}").exists()


def test_a_runtime_whose_weights_are_not_the_pin_is_refused_by_name(
    lab: Lab, tmp_path: Path
) -> None:
    lab.archive("--remove-local-copy")
    other = tmp_path.resolve() / "other-runtime"
    (other / "pretrained").mkdir(mode=0o700, parents=True)
    (other / "pretrained/model.safetensors").write_bytes(b"other weights entirely")

    refusal = lab.refuse_restore_run(
        "--runtime-root", str(other), code="FINETUNE.PRETRAINED_MISMATCH"
    )

    assert PRETRAINED in refusal
    assert lab.run_file(CHECKPOINT).exists(), "the file entries are restored before the pins"
    assert not lab.run_file(PRETRAINED).exists()


def test_a_restore_without_a_runtime_says_the_pinned_weights_are_still_missing(lab: Lab) -> None:
    lab.archive("--remove-local-copy")

    restored = lab.restore_run()

    assert [pin["state"] for pin in restored["pins"]] == ["missing"]
    assert "no runtime root" in restored["pins"][0]["reason"]
    assert lab.run_file(CHECKPOINT).exists() and not lab.run_file(PRETRAINED).exists()
    # The pin is reported, not refused: only F5's trainer reads it, and the runtime is not
    # part of the library, so a Mac rebuilt from the vault has none yet. Everything Speak
    # needs is back, so the run is local again and speaks.
    assert lab.summary().location == "local"
    assert resolve_speech_model(lab.layout, "latest").run_id == RUN_FINISHED


@pytest.mark.parametrize("command", ["train", "run", "dataset"])
def test_every_command_that_writes_into_a_run_refuses_an_archived_one(
    lab: Lab, command: str
) -> None:
    # `run` and `dataset` rewrite dataset/, run.json and checkpoints/ of an existing --run-id,
    # so each of them does what `train` is refused for, and `run` does all of it.
    lab.archive("--remove-local-copy")
    checkpoints = sorted(path.name for path in lab.run_file(f"{RUN_ROOT}/checkpoints").iterdir())
    dataset = lab.run_file(f"{RUN_ROOT}/dataset/dataset.json").read_bytes()
    extra = {
        "train": ("--runtime-root", str(lab.runtime)),
        "run": ("--catalog", "pilot-v1", "--runtime-root", str(lab.runtime)),
        "dataset": ("--catalog", "pilot-v1"),
    }[command]

    refusal = lab.cli.refused(
        "finetune",
        command,
        "--asset-root",
        str(lab.root),
        "--run-id",
        RUN_FINISHED,
        *extra,
        code="FINETUNE.RUN_ARCHIVED",
    )

    assert "restore-run" in refusal
    assert sorted(path.name for path in lab.run_file(f"{RUN_ROOT}/checkpoints").iterdir()) == (
        checkpoints
    )
    assert lab.run_file(f"{RUN_ROOT}/dataset/dataset.json").read_bytes() == dataset


def test_the_archived_run_code_reaches_the_app_as_a_wire_error(lab: Lab) -> None:
    lab.archive("--remove-local-copy")
    spoken = SynthesizeCommand(operation_id="speak-1", text="Hello from the vault.", model="latest")

    reply = run_speech_worker_request(
        lab.layout,
        SpeechWorkerRequest(
            generation="speech-generation",
            operation_id=spoken.operation_id,
            request_sha256=canonical_sha256(spoken),
            command=spoken,
            runtime_root=str(lab.runtime),
        ),
    )

    assert reply.clip is None and reply.resolution is None
    assert reply.error is not None and reply.error.code == "FINETUNE.RUN_ARCHIVED"
    assert reply.error.retryable is False
    # The restore command has to survive the worker's 1,024-character bound.
    assert f"restore-run --run {RUN_FINISHED}" in reply.error.message
    assert isinstance(reply.error, RecordingError)


def test_the_archive_records_are_private_and_never_replaced(lab: Lab) -> None:
    lab.archive("--remove-local-copy")
    for name in (FINETUNE_ARCHIVE_NAME, FINETUNE_REMOVAL_NAME):
        path = lab.run_file(f"{RUN_ROOT}/{name}")
        assert os.stat(path).st_mode & 0o777 == 0o600
    existing = FineTuneRunArchive.model_validate_json(
        lab.run_file(f"{RUN_ROOT}/{FINETUNE_ARCHIVE_NAME}").read_bytes()
    )
    with pytest.raises(FineTuneError) as refused:
        write_run_archive(lab.layout, RUN_ROOT, existing)
    assert refused.value.code == "FINETUNE.RUN_EXISTS"


def test_nothing_the_commands_print_repeats_the_library_key(lab: Lab) -> None:
    archived = json.dumps(lab.archive("--remove-local-copy"))
    restored = json.dumps(lab.restore_run("--runtime-root", str(lab.runtime)))

    assert SYNTHETIC_KEY_HEX not in archived + restored


# -- records and their rules ---------------------------------------------------------------------


def test_an_archive_or_removal_record_names_each_file_once() -> None:
    twice = (
        FineTuneArchivedFile(
            path="checkpoints/model_last.pt", sha256="1" * 64, size=8, kind="file"
        ),
        FineTuneArchivedFile(
            path="checkpoints/model_last.pt", sha256="2" * 64, size=9, kind="file"
        ),
    )
    with pytest.raises(ValidationError, match="names each file once"):
        FineTuneRunArchive(
            run_id=RUN_FINISHED,
            library_id=LIBRARY_ID,
            destination_sha256="0" * 64,
            generation=1,
            manifest_sha256="0" * 64,
            archived_at=utc_now(),
            files=twice,
        )
    with pytest.raises(ValidationError, match="names each file once"):
        FineTuneRunRemoval(
            run_id=RUN_FINISHED,
            generation=1,
            manifest_sha256="0" * 64,
            removed_at=utc_now(),
            files=twice,
        )


def test_a_run_record_that_names_another_run_is_refused(lab: Lab) -> None:
    run_path = lab.run_file(f"{RUN_ROOT}/run.json")
    run = json.loads(run_path.read_bytes())
    run["run_id"] = "another-run"
    run_path.chmod(0o600)
    run_path.write_text(json.dumps(run))

    refusal = lab.refuse_archive(code="FINETUNE.SOURCE_CHANGED")

    assert "names run another-run" in refusal


def test_each_restore_keeps_its_own_removal_history(lab: Lab) -> None:
    lab.archive("--remove-local-copy")
    lab.restore_run()
    lab.archive("--remove-local-copy")

    lab.restore_run()

    assert lab.run_file(f"{RUN_ROOT}/local-removal-restored-1.json").exists()
    assert lab.run_file(f"{RUN_ROOT}/local-removal-restored-2.json").exists()
    assert lab.summary().location == "local"


def test_a_file_that_is_back_with_other_bytes_is_refused_rather_than_replaced(lab: Lab) -> None:
    size = lab.run_file(CHECKPOINT).stat().st_size
    lab.archive("--remove-local-copy")
    lab.run_file(CHECKPOINT).write_bytes(b"?" * (size + 1))
    lab.run_file(CHECKPOINT).chmod(0o600)

    assert "is not the file it names" in lab.refuse_restore_run(code="REPLICATION.RESTORE_REFUSED")

    lab.run_file(CHECKPOINT).write_bytes(b"?" * size)
    lab.run_file(CHECKPOINT).chmod(0o600)

    assert "other bytes" in lab.refuse_restore_run(code="REPLICATION.RESTORE_REFUSED")
    assert lab.run_file(CHECKPOINT).read_bytes() == b"?" * size


def test_a_removal_record_that_went_missing_and_came_back_still_recovers_its_entries(
    lab: Lab,
) -> None:
    removed = lab.archive("--remove-local-copy")["removed"]
    generation = removed["generation"]
    before = lab.entries(generation)
    record = lab.run_file(f"{RUN_ROOT}/{FINETUNE_REMOVAL_NAME}")
    kept = record.read_bytes()

    # Without the record the removed files are simply gone, so the next generation drops them.
    record.chmod(0o600)
    record.unlink()
    dropped = lab.cli.json("vault", "push", "--asset-root", str(lab.root))
    assert CHECKPOINT not in lab.entries(dropped["generation"])

    # With it back, the generation it names is read from this Mac's manifest copies.
    record.write_bytes(kept)
    record.chmod(0o600)
    recovered = lab.cli.json("vault", "push", "--asset-root", str(lab.root))
    after = lab.entries(recovered["generation"])

    assert after[CHECKPOINT].chunks == before[CHECKPOINT].chunks
    assert after[PRETRAINED].kind == "pin"
    assert recovered["waiting_paths"] == []


def test_a_removal_record_no_generation_holds_leaves_its_files_waiting(lab: Lab) -> None:
    lab.archive("--remove-local-copy")
    record = lab.run_file(f"{RUN_ROOT}/{FINETUNE_REMOVAL_NAME}")
    removal = FineTuneRunRemoval.model_validate_json(record.read_bytes())
    record.chmod(0o600)
    record.write_bytes(
        removal.model_copy(
            update={
                "files": tuple(
                    file.model_copy(update={"sha256": "e" * 64}) for file in removal.files
                )
            }
        )
        .model_dump_json()
        .encode()
        + b"\n"
    )

    pushed = lab.cli.json("vault", "push", "--asset-root", str(lab.root))

    assert CHECKPOINT in pushed["waiting_paths"]
    assert CHECKPOINT not in lab.entries(pushed["generation"])


def test_a_push_that_does_not_carry_this_macs_view_archives_nothing(lab: Lab) -> None:
    # A consent record the whole world could read makes its owner refused, so the push does
    # not stand for what is on this Mac.
    lab.run_file("provenance/consents/cole-self-v1.json").chmod(0o644)

    refusal = lab.refuse_archive()

    assert "did not finish" in refusal and "vault push" in refusal
    assert not lab.run_file(f"{RUN_ROOT}/{FINETUNE_ARCHIVE_NAME}").exists()


def test_a_run_without_checkpoints_has_no_local_copy_to_remove(lab: Lab) -> None:
    for name in ("model_last.pt", "pretrained_model.safetensors"):
        lab.run_file(f"{RUN_ROOT}/checkpoints/{name}").unlink()

    refusal = lab.refuse_archive("--remove-local-copy")

    assert "holds no checkpoint" in refusal
    assert lab.run_file(f"{RUN_ROOT}/{FINETUNE_ARCHIVE_NAME}").exists()
    assert not lab.run_file(f"{RUN_ROOT}/{FINETUNE_REMOVAL_NAME}").exists()


def test_a_removal_record_naming_a_file_the_generation_lacks_refuses_the_restore(
    lab: Lab,
) -> None:
    lab.archive("--remove-local-copy")
    record = lab.run_file(f"{RUN_ROOT}/{FINETUNE_REMOVAL_NAME}")
    removal = FineTuneRunRemoval.model_validate_json(record.read_bytes())
    record.chmod(0o600)
    record.write_bytes(
        removal.model_copy(
            update={
                "files": (
                    FineTuneArchivedFile(
                        path="checkpoints/model_9.pt", sha256="f" * 64, size=16, kind="file"
                    ),
                )
            }
        )
        .model_dump_json()
        .encode()
        + b"\n"
    )

    refusal = lab.refuse_restore_run()

    assert "does not hold" in refusal and "model_9.pt" in refusal


def test_a_removal_record_naming_a_generation_this_mac_forgot_leaves_its_files_waiting(
    lab: Lab,
) -> None:
    lab.archive("--remove-local-copy")
    record = lab.run_file(f"{RUN_ROOT}/{FINETUNE_REMOVAL_NAME}")
    removal = FineTuneRunRemoval.model_validate_json(record.read_bytes())
    # Without the record the newest generation drops the files, so only the generation the
    # record names can bring their entries back -- and this Mac keeps no copy of it.
    record.chmod(0o600)
    record.unlink()
    lab.cli.json("vault", "push", "--asset-root", str(lab.root))
    record.write_bytes(
        removal.model_copy(update={"generation": 99}).model_dump_json().encode() + b"\n"
    )
    record.chmod(0o600)

    pushed = lab.cli.json("vault", "push", "--asset-root", str(lab.root))

    assert CHECKPOINT in pushed["waiting_paths"]
    assert CHECKPOINT not in lab.entries(pushed["generation"])


def test_a_removal_a_read_only_directory_blocks_is_named_not_traced(lab: Lab) -> None:
    lab.archive()
    lab.run_file(f"{RUN_ROOT}/checkpoints").chmod(0o500)
    try:
        refusal = lab.refuse_archive("--remove-local-copy", code="FINETUNE.SOURCE_CHANGED")
    finally:
        lab.run_file(f"{RUN_ROOT}/checkpoints").chmod(0o700)

    assert "cannot be removed" in refusal
    # The record is written first, so the removal can be finished once the mode is fixed.
    assert lab.run_file(f"{RUN_ROOT}/{FINETUNE_REMOVAL_NAME}").exists()
    assert lab.run_file(CHECKPOINT).exists()


def test_a_restore_that_cannot_read_a_chunk_leaves_no_half_written_file(lab: Lab) -> None:
    removed = lab.archive("--remove-local-copy")["removed"]
    entry = lab.entries(removed["generation"])[CHECKPOINT]
    blob = lab.blob_file(entry.chunks[0].sha256)
    blob.chmod(0o600)
    blob.write_bytes(b"\x00" * blob.stat().st_size)

    lab.cli.refused(
        "finetune",
        "restore-run",
        "--asset-root",
        str(lab.root),
        "--run",
        RUN_FINISHED,
        code="STORAGE.INTEGRITY",
    )

    assert not lab.run_file(CHECKPOINT).exists()
    assert list(lab.run_file(f"{RUN_ROOT}/checkpoints").glob(".*")) == []
    assert lab.run_file(f"{RUN_ROOT}/{FINETUNE_REMOVAL_NAME}").exists()


def test_a_manifest_copy_that_is_not_the_one_the_record_names_blocks_the_restore(
    lab: Lab,
) -> None:
    lab.archive("--remove-local-copy")
    record = lab.run_file(f"{RUN_ROOT}/{FINETUNE_REMOVAL_NAME}")
    removal = FineTuneRunRemoval.model_validate_json(record.read_bytes())
    record.chmod(0o600)
    record.write_bytes(
        removal.model_copy(update={"manifest_sha256": "a" * 64}).model_dump_json().encode() + b"\n"
    )

    refusal = lab.refuse_restore_run()

    assert "is not the one the run's record names" in refusal
    assert not lab.run_file(CHECKPOINT).exists()


def test_a_pin_a_crash_left_in_place_is_kept_rather_than_replaced(lab: Lab) -> None:
    archived = lab.archive()
    entries = lab.entries(archived["generation"])
    removal = FineTuneRunRemoval(
        run_id=RUN_FINISHED,
        generation=archived["generation"],
        manifest_sha256=archived["manifest_sha256"],
        removed_at=utc_now(),
        files=tuple(
            FineTuneArchivedFile(
                path=path.removeprefix(f"{RUN_ROOT}/"),
                sha256=entries[path].sha256,
                size=entries[path].size,
                kind=entries[path].kind,
            )
            for path in (CHECKPOINT, PRETRAINED)
        ),
    )
    write_run_removal(lab.layout, RUN_ROOT, removal)
    # The crash landed between the two unlinks: the pin is still hard-linked to the runtime.
    lab.run_file(CHECKPOINT).unlink()

    restored = lab.restore_run("--runtime-root", str(lab.runtime))

    assert [file["state"] for file in restored["restored"]] == ["written"]
    assert [pin["state"] for pin in restored["pins"]] == ["linked"]
    assert lab.run_file(PRETRAINED).exists() and lab.run_file(CHECKPOINT).exists()

    # A pin whose bytes are not the ones it names is refused, never replaced.
    pinned = lab.run_file(PRETRAINED).stat().st_size
    lab.archive("--remove-local-copy")
    lab.run_file(PRETRAINED).write_bytes(b"?" * pinned)
    lab.run_file(PRETRAINED).chmod(0o600)

    assert "is not the file it pins" in lab.refuse_restore_run(
        "--runtime-root", str(lab.runtime), code="REPLICATION.RESTORE_REFUSED"
    )


# -- what binds the unlink to the bytes the vault holds ------------------------------------------


def _retrain(lab: Lab, bytes_written: bytes) -> None:
    """Rewrite the run's checkpoint and the digest ``run.json`` binds it to, together."""
    checkpoint = lab.run_file(CHECKPOINT)
    checkpoint.chmod(0o600)
    checkpoint.write_bytes(bytes_written)
    run_path = lab.run_file(f"{RUN_ROOT}/run.json")
    run = json.loads(run_path.read_bytes())
    run["checkpoints"] = [
        {
            **entry,
            "sha256": hashlib.sha256(bytes_written).hexdigest(),
            "size_bytes": len(bytes_written),
        }
        if entry["relative_path"] == CHECKPOINT
        else entry
        for entry in run["checkpoints"]
    ]
    run_path.chmod(0o600)
    run_path.write_text(json.dumps(run))


@pytest.mark.parametrize("same_size", [False, True])
def test_a_checkpoint_the_removal_record_does_not_name_is_never_unlinked(
    lab: Lab, same_size: bool
) -> None:
    archived = lab.archive()
    entries = lab.entries(archived["generation"])
    # The record names the bytes that generation holds ...
    write_run_removal(
        lab.layout,
        RUN_ROOT,
        FineTuneRunRemoval(
            run_id=RUN_FINISHED,
            generation=archived["generation"],
            manifest_sha256=archived["manifest_sha256"],
            removed_at=utc_now(),
            files=(
                FineTuneArchivedFile(
                    path="checkpoints/model_last.pt",
                    sha256=entries[CHECKPOINT].sha256,
                    size=entries[CHECKPOINT].size,
                    kind="file",
                ),
            ),
        ),
    )
    # ... and then the run is trained again: run.json and its checkpoint move together, so
    # every later copy is consistent and only the removal record is behind. Bytes of the same
    # length are caught by the digest, not by the size.
    size = entries[CHECKPOINT].size
    trained = b"?" * size if same_size else b"trained again, and longer than the first time"
    assert (len(trained) == size) is same_size
    _retrain(lab, trained)

    refusal = lab.refuse_archive("--remove-local-copy", code="FINETUNE.SOURCE_CHANGED")

    assert f"{CHECKPOINT} is not the file {RUN_ROOT}/{FINETUNE_REMOVAL_NAME} names" in refusal
    assert lab.run_file(CHECKPOINT).read_bytes() == trained


def test_a_job_that_starts_during_the_push_archives_nothing(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    pushing = generations_module.push_generation

    def queued_meanwhile(*arguments: Any, **options: Any) -> Any:
        # The app queues a fine-tune after the command's activity check: the pass then reports
        # the whole fine-tune owner `changed` and carries the runs of an earlier generation,
        # which is a fine copy but not something an archive may rest on.
        write_recording_evidence(
            lab.root / "provenance/finetune-jobs/head.json",
            FineTuneJobHead(
                job=FineTuneJob(
                    operation_id="finetune-9", request_sha256="2" * 64, queued_at=utc_now()
                )
            ),
            immutable=False,
        )
        return pushing(*arguments, **options)

    monkeypatch.setattr(generations_module, "push_generation", queued_meanwhile)

    refusal = lab.refuse_archive("--remove-local-copy")

    assert "did not carry this Mac's fine-tune runs" in refusal
    assert not lab.run_file(f"{RUN_ROOT}/{FINETUNE_ARCHIVE_NAME}").exists()
    assert not lab.run_file(f"{RUN_ROOT}/{FINETUNE_REMOVAL_NAME}").exists()
    assert lab.run_file(CHECKPOINT).exists()


@pytest.mark.parametrize("resumed", [False, True])
def test_a_speak_request_that_starts_during_the_archive_stops_the_removal(
    lab: Lab, monkeypatch: pytest.MonkeyPatch, resumed: bool
) -> None:
    archived = lab.archive()
    if resumed:
        # A removal a crash interrupted takes the other route: no record is written, the
        # record it finds is the one it unlinks by.
        entries = lab.entries(archived["generation"])
        write_run_removal(
            lab.layout,
            RUN_ROOT,
            FineTuneRunRemoval(
                run_id=RUN_FINISHED,
                generation=archived["generation"],
                manifest_sha256=archived["manifest_sha256"],
                removed_at=utc_now(),
                files=(
                    FineTuneArchivedFile(
                        path="checkpoints/model_last.pt",
                        sha256=entries[CHECKPOINT].sha256,
                        size=entries[CHECKPOINT].size,
                        kind="file",
                    ),
                ),
            ),
        )
    verifying = cli_module._finetune_verify_run

    def then_cole_speaks(*arguments: Any) -> Any:
        report = verifying(*arguments)
        # Minutes of push and verify separate the command's opening check from its unlinks;
        # a Speak request that starts in that window resolved a checkpoint that is about to go.
        write_recording_evidence(
            lab.root / "provenance/finetune-jobs/speech-active.json",
            SpeechActiveMarker(operation_id="speak-7", pid=os.getpid(), started_at=utc_now()),
            immutable=False,
        )
        return report

    monkeypatch.setattr(cli_module, "_finetune_verify_run", then_cole_speaks)

    refusal = lab.refuse_archive("--remove-local-copy", code="FINETUNE.JOB_ACTIVE")

    assert "Speak request speak-7 is running" in refusal
    assert lab.run_file(CHECKPOINT).exists()
    assert lab.run_file(f"{RUN_ROOT}/{FINETUNE_REMOVAL_NAME}").exists() is resumed


def test_the_verify_checks_the_generation_the_records_name_not_whatever_is_the_head(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    archived = lab.archive()
    chunks = lab.entries(archived["generation"])[CHECKPOINT].chunks
    pushing = cli_module._finetune_push
    interrupted: list[int] = []

    def then_another_writer_pushes(*arguments: Any) -> Any:
        pushed = pushing(*arguments)
        if interrupted:
            return pushed
        interrupted.append(pushed.number)
        # The service's own vault supervisor pushes while this command downloads. `--full`
        # re-seals every chunk, so the new head names blobs this command never proved, and
        # the blobs the run's records name are no longer enough.
        later = lab.run_file("provenance/consents/later-consent.json")
        later.write_bytes(b'{"consent":"later"}\n')
        later.chmod(0o600)
        lab.cli.json("vault", "push", "--full", "--asset-root", str(lab.root))
        for ref in chunks:
            lab.blob_file(ref.sha256).unlink()
        return pushed

    monkeypatch.setattr(cli_module, "_finetune_push", then_another_writer_pushes)

    refusal = lab.refuse_archive("--remove-local-copy")

    assert "did not verify" in refusal and CHECKPOINT in refusal
    assert lab.run_file(CHECKPOINT).exists()
    assert not lab.run_file(f"{RUN_ROOT}/{FINETUNE_REMOVAL_NAME}").exists()


def test_a_removal_record_may_name_only_a_runs_checkpoints(lab: Lab) -> None:
    summary_record = FineTuneArchivedFile(path="run.json", sha256="1" * 64, size=4, kind="file")
    with pytest.raises(ValidationError, match="names only checkpoints/ files"):
        FineTuneRunRemoval(
            run_id=RUN_FINISHED,
            generation=1,
            manifest_sha256="0" * 64,
            removed_at=utc_now(),
            files=(summary_record,),
        )

    # A record written by hand is refused where it is read, so no removal can take a record
    # every status reply needs.
    archived = lab.archive()
    forged = {
        "schema_version": "cole-voice-lab.finetune-run-removal/v1",
        "run_id": RUN_FINISHED,
        "generation": archived["generation"],
        "manifest_sha256": archived["manifest_sha256"],
        "removed_at": utc_now().isoformat(),
        "files": [summary_record.model_dump(mode="json")],
    }
    record = lab.run_file(f"{RUN_ROOT}/{FINETUNE_REMOVAL_NAME}")
    record.write_text(json.dumps(forged))
    record.chmod(0o600)

    assert lab.refuse_archive("--remove-local-copy")
    assert lab.run_file(f"{RUN_ROOT}/run.json").exists()
    assert lab.run_file(CHECKPOINT).exists()


def test_the_summary_cache_sees_a_run_leave_the_mac_and_come_back(lab: Lab) -> None:
    # Archiving and removing changes none of the five records a summary is read from, so the
    # cache would keep reporting a removed run as local without the two archive records.
    cache = FineTuneSummaryCache()
    before = finished_finetune_runs(lab.layout, summaries=cache)[0]
    assert (before.location, before.in_vault) == ("local", False)

    lab.archive("--remove-local-copy")
    gone = finished_finetune_runs(lab.layout, summaries=cache)[0]

    assert (gone.location, gone.in_vault) == ("archived", True)

    lab.restore_run("--runtime-root", str(lab.runtime))
    back = finished_finetune_runs(lab.layout, summaries=cache)[0]

    assert (back.location, back.in_vault) == ("local", True)


def test_the_summary_cache_sees_a_removal_that_was_resumed_from_its_record(lab: Lab) -> None:
    # A removal resumed from a record an interrupted one left writes no record at all: it only
    # unlinks. None of the seven records a summary is identified by changes, so an identity
    # check alone would keep the run `local` until the service restarts, and the app would go
    # on offering a run whose checkpoints are gone.
    archived = lab.archive()
    entry = lab.entries(archived["generation"])[CHECKPOINT]
    write_run_removal(
        lab.layout,
        RUN_ROOT,
        FineTuneRunRemoval(
            run_id=RUN_FINISHED,
            generation=archived["generation"],
            manifest_sha256=archived["manifest_sha256"],
            removed_at=utc_now(),
            files=(
                FineTuneArchivedFile(
                    path="checkpoints/model_last.pt",
                    sha256=entry.sha256,
                    size=entry.size,
                    kind="file",
                ),
            ),
        ),
    )
    cache = FineTuneSummaryCache()
    assert finished_finetune_runs(lab.layout, summaries=cache)[0].location == "local"

    removed = lab.archive("--remove-local-copy")["removed"]

    assert removed["removal_record"] is None and removed["files"] == ["checkpoints/model_last.pt"]
    assert not lab.run_file(CHECKPOINT).exists()
    assert finished_finetune_runs(lab.layout, summaries=cache)[0].location == "archived"


def test_a_removal_record_that_cannot_be_read_keeps_the_run_out_of_speak(lab: Lab) -> None:
    lab.archive("--remove-local-copy")
    record = lab.run_file(f"{RUN_ROOT}/{FINETUNE_REMOVAL_NAME}")
    record.chmod(0o600)
    record.write_bytes(b"{ not a record at all")

    # The checkpoints are gone; a record nobody can read must not put the run back in the
    # list Speak resolves from, so an unreadable record reads as archived.
    assert lab.summary().location == "archived"
    with pytest.raises(FineTuneError) as refused:
        resolve_speech_model(lab.layout, "latest")

    assert refused.value.code == "FINETUNE.RUN_ARCHIVED"


def test_a_verify_that_reports_another_generation_removes_nothing(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab.archive()
    verifying = generations_module.verify_generation

    def reports_another_manifest(*arguments: Any, **options: Any) -> Any:
        return replace(verifying(*arguments, **options), manifest_sha256="b" * 64)

    monkeypatch.setattr(generations_module, "verify_generation", reports_another_manifest)

    refusal = lab.refuse_archive("--remove-local-copy")

    assert "is not the one this push left" in refusal
    assert lab.run_file(CHECKPOINT).exists()
    assert not lab.run_file(f"{RUN_ROOT}/{FINETUNE_REMOVAL_NAME}").exists()
