"""A finished prompted session as a capture package (D-042): one kept take per prompt, as
recorded (a trim stays local), labelled ``portaudio``, accepted by the strict validator,
rebuilt to the same bytes and never overwritten."""

from __future__ import annotations

import os
import stat

import pytest
from voice_lab_desktop.capture.types import ORIGINAL_NAME
from voice_lab_desktop.prompted import catalog, package
from voice_lab_desktop.prompted.store import PromptedStore, StoreError

from cole_voice_lab.canonical import sha256_file
from cole_voice_lab.intake import validate_capture_package
from cole_voice_lab.models import utc_now

from .conftest import add_take, new_session

PROMPTS = ("room-tone-start", "reference-neutral-01", "room-tone-end")


def test_a_finished_session_becomes_a_valid_capture_package(store: PromptedStore) -> None:
    session = new_session(store)
    takes = {prompt_id: add_take(store, session, prompt_id) for prompt_id in PROMPTS}
    # A second, later-kept take wins; an earlier one, trimmed, is never what is sent.
    add_take(store, session, "reference-neutral-01", keep=False)
    later = add_take(store, session, "reference-neutral-01")
    store.set_trim(session, "reference-neutral-01", later.take_number, (480, 2400))
    takes["reference-neutral-01"] = store.take(session, "reference-neutral-01", later.take_number)

    with pytest.raises(package.PackageNotReady, match="finish the session first"):
        package.build(store, session)
    session = store.update(session, state="finished", finished_at=utc_now())
    path, digest = package.build(store, session)

    assert path == store.packages_root / session.session_id
    validated = validate_capture_package(path)
    manifest = validated.manifest
    assert validated.manifest_sha256 == digest
    assert manifest.session_id == session.session_id
    assert manifest.prompt_suite_id == catalog.batch_suite(session.catalog, PROMPTS).suite_id
    assert manifest.capture_host == "voice-lab-desktop" and manifest.subject_id == "cole-self"
    assert manifest.capture_device.backend == "portaudio"
    assert "umik-1" in manifest.capture_device.display_name.casefold()
    assert manifest.finalized_at == session.finished_at
    assert manifest.captured_at == min(t.requested_at for t in takes.values())
    assert [f.kind for f in manifest.files] == ["room-tone", "utterance", "room-tone"]
    # Each file is the kept take's original, untrimmed (D-012), copied private.
    assert {f.sha256 for f in manifest.files} == {t.sha256 for t in takes.values()}
    for entry in manifest.files:
        copied = path / entry.path
        assert stat.S_IMODE(copied.stat().st_mode) == 0o600
        assert copied.stat().st_nlink == 1
    take_dir = store.session_dir(session.session_id) / "takes/reference-neutral-01"
    original = take_dir / str(later.take_number) / ORIGINAL_NAME
    utterance = next(f for f in manifest.files if f.kind == "utterance")
    assert sha256_file(path / utterance.path)[0] == sha256_file(original)[0]
    assert os.stat(path / utterance.path).st_ino != os.stat(original).st_ino

    # Built again: the same package, nothing new beside it.
    before = sorted(p.name for p in store.packages_root.iterdir())
    assert package.build(store, session) == (path, digest)
    assert sorted(p.name for p in store.packages_root.iterdir()) == before


def test_a_session_without_every_kept_take_is_not_packaged(store: PromptedStore) -> None:
    session = new_session(store, ("reference-neutral-01", "expressive-02"))
    add_take(store, session, "room-tone-start")
    add_take(store, session, "reference-neutral-01", keep=False)
    session = store.update(session, state="finished", finished_at=utc_now())
    with pytest.raises(
        package.PackageNotReady,
        match=r"3 prompt\(s\) have no kept take: reference-neutral-01, expressive-02, room-tone-",
    ):
        package.build(store, session)
    assert not store.packages_root.exists()


def test_a_changed_original_or_mixed_devices_are_refused(store: PromptedStore) -> None:
    session = new_session(store)
    takes = {prompt_id: add_take(store, session, prompt_id) for prompt_id in PROMPTS}
    session = store.update(session, state="finished", finished_at=utc_now())
    take_dir = store.session_dir(session.session_id) / "takes/room-tone-end/1"
    original = take_dir / ORIGINAL_NAME
    data = bytearray(original.read_bytes())
    data[-1] ^= 0xFF
    original.write_bytes(bytes(data))
    with pytest.raises(StoreError, match="changed on disk since it was recorded"):
        package.build(store, session)
    assert not store.packages_root.exists()  # checked before anything is written

    other = new_session(store)
    for prompt_id in PROMPTS[:2]:
        add_take(store, other, prompt_id)
    add_take(store, other, PROMPTS[2], device="MacBook Air Microphone [Core Audio]")
    other = store.update(other, state="finished", finished_at=utc_now())
    with pytest.raises(package.PackageNotReady, match="recorded on different devices"):
        package.build(store, other)
    assert takes  # (the first session's takes were all kept)


def test_an_existing_package_that_is_not_this_sessions_is_refused(store: PromptedStore) -> None:
    session = new_session(store)
    for prompt_id in PROMPTS:
        add_take(store, session, prompt_id)
    session = store.update(session, state="finished", finished_at=utc_now())
    path, _ = package.build(store, session)
    # Another take kept after packaging: the package on disk no longer holds the kept takes.
    add_take(store, session, "reference-neutral-01")
    from cole_voice_lab.errors import SecurityError

    with pytest.raises(SecurityError, match="does not hold this session's kept takes"):
        package.build(store, session)
    assert path.is_dir()


def test_a_prompt_suite_v1_session_packages_too(store: PromptedStore) -> None:
    loaded = catalog.load("builtin:pilot-v1.json", catalog_file=None)
    ids = catalog.batch_ids(loaded.suite, ["expressive-02"])
    session = store.create(
        catalog_source=loaded.source,
        catalog_label=loaded.label,
        catalog=loaded.suite,
        catalog_sha256=loaded.sha256,
        prompt_ids=ids,
        device="Umik-1  Gain: 18dB [ALSA]",
    )
    for prompt_id in ids:
        add_take(store, session, prompt_id, device="Umik-1  Gain: 18dB [ALSA]")
    session = store.update(session, state="finished", finished_at=utc_now())
    path, _ = package.build(store, session)
    manifest = validate_capture_package(path).manifest
    assert manifest.prompt_suite_id.startswith("desktop-subset-")
    utterance = next(f for f in manifest.files if f.kind == "utterance")
    assert utterance.prompt_id == "expressive-02" and utterance.transcript
