"""The local take store (D-010, D-012): private, atomic, recoverable, never rewriting an
original."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from voice_lab_desktop.capture.types import EVIDENCE_NAME, ORIGINAL_NAME, PARTIAL_NAME
from voice_lab_desktop.prompted.store import (
    FAILURE_FILE,
    SESSION_FILE,
    TAKE_FILE,
    PromptedStore,
    StoreError,
)

from cole_voice_lab.canonical import sha256_file
from cole_voice_lab.models import utc_now

from .conftest import add_take, new_session


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_a_session_and_its_takes_are_private_and_atomic(store: PromptedStore) -> None:
    session = new_session(store)
    assert session.session_id.startswith("native-desktop-")
    assert session.prompt_ids == ("room-tone-start", "reference-neutral-01", "room-tone-end")
    directory = store.session_dir(session.session_id)
    assert mode(store.root / "sessions") == 0o700 and mode(directory) == 0o700
    assert mode(directory / SESSION_FILE) == 0o600
    assert store.load(session.session_id) == session
    assert store.open_session() == session

    first = add_take(store, session, "reference-neutral-01", keep=False)
    second = add_take(store, session, "reference-neutral-01", keep=False)
    assert (first.take_number, second.take_number) == (1, 2)
    take_dir = directory / "takes" / "reference-neutral-01" / "1"
    assert mode(take_dir) == 0o700 and mode(take_dir / TAKE_FILE) == 0o600
    assert first.decision == "draft" and [e.decision for e in first.history] == ["draft"]
    # Records are written whole: no temporary file is left behind.
    assert not [p for p in directory.rglob(".*tmp*")]
    with pytest.raises(StoreError, match="not in session"):
        store.new_take_dir(session, "expressive-02")
    with pytest.raises(StoreError, match="not a prompted session id"):
        store.session_dir("../elsewhere")


def test_decisions_are_kept_with_their_history_and_removal_is_reversible(
    store: PromptedStore,
) -> None:
    session = new_session(store)
    one = add_take(store, session, "reference-neutral-01", keep=False)
    two = add_take(store, session, "reference-neutral-01", keep=False)
    assert store.statuses(session)["reference-neutral-01"] == "draft"
    assert store.kept(session, "reference-neutral-01") is None
    store.decide(session, "reference-neutral-01", two.take_number, "kept")
    store.decide(session, "reference-neutral-01", one.take_number, "kept")
    # The take kept most recently is the prompt's take (D-016).
    assert store.kept(session, "reference-neutral-01").take_number == one.take_number  # type: ignore[union-attr]
    removed = store.decide(session, "reference-neutral-01", one.take_number, "removed")
    assert removed.decision == "removed"
    assert store.kept(session, "reference-neutral-01").take_number == two.take_number  # type: ignore[union-attr]
    restored = store.restore(session, "reference-neutral-01", one.take_number)
    assert restored.decision == "kept"
    assert [e.decision for e in restored.history] == ["draft", "kept", "removed", "kept"]
    assert store.restore(session, "reference-neutral-01", one.take_number) == restored
    store.decide(session, "reference-neutral-01", one.take_number, "removed")
    store.decide(session, "reference-neutral-01", two.take_number, "removed")
    assert store.statuses(session)["reference-neutral-01"] == "missing"


def test_a_trim_is_a_recipe_bound_to_the_original(store: PromptedStore) -> None:
    session = new_session(store)
    take = add_take(store, session, "reference-neutral-01", frames=4800)
    original = (
        store.session_dir(session.session_id) / "takes/reference-neutral-01/1" / ORIGINAL_NAME
    )
    before = original.read_bytes()
    trimmed = store.set_trim(session, "reference-neutral-01", 1, (480, 4000))
    assert trimmed.trim is not None
    assert (trimmed.trim.start_frame, trimmed.trim.end_frame) == (480, 4000)
    assert trimmed.trim.source_sha256 == take.sha256
    assert original.read_bytes() == before  # never rewritten (D-012)
    whole = store.set_trim(session, "reference-neutral-01", 1, (0, 4800))
    assert whole.trim is None
    for bad in ((0, 0), (10, 5), (0, 4801), (-1, 10)):
        with pytest.raises(StoreError, match="within the take's 4800 frames"):
            store.set_trim(session, "reference-neutral-01", 1, bad)
    assert store.set_trim(session, "reference-neutral-01", 1, None).trim is None


def test_failed_and_damaged_takes_are_shown_but_never_used(store: PromptedStore) -> None:
    session = new_session(store)
    good = add_take(store, session, "reference-neutral-01", keep=False)
    number, failed_dir = store.new_take_dir(session, "reference-neutral-01")
    (failed_dir / PARTIAL_NAME).write_bytes(b"partial")
    store.record_failure(
        session, "reference-neutral-01", number, requested_at=utc_now(), message="a gap"
    )
    assert mode(failed_dir / FAILURE_FILE) == 0o600
    _, damaged_dir = store.new_take_dir(session, "reference-neutral-01")
    (damaged_dir / ORIGINAL_NAME).write_bytes(b"left over")
    # A take whose original changed size, or lost its evidence, is damaged too.
    _, changed_dir = store.new_take_dir(session, "reference-neutral-01")
    changed = add_take(store, session, "reference-neutral-01", keep=False)
    changed_path = store.session_dir(session.session_id) / "takes/reference-neutral-01"
    (changed_path / str(changed.take_number) / ORIGINAL_NAME).write_bytes(b"short")
    lost = add_take(store, session, "reference-neutral-01", keep=False)
    (changed_path / str(lost.take_number) / EVIDENCE_NAME).unlink()
    (changed_dir / TAKE_FILE).write_text("{not json")
    statuses = [(e.number, e.status) for e in store.takes(session, "reference-neutral-01")]
    assert statuses == [
        (good.take_number, "draft"),
        (number, "failed"),
        (3, "damaged"),
        (4, "damaged"),
        (changed.take_number, "damaged"),
        (lost.take_number, "damaged"),
    ]
    failed = store.takes(session, "reference-neutral-01")[1]
    assert failed.failure is not None and failed.failure.message == "a gap"
    with pytest.raises(StoreError, match="take 3 of reference-neutral-01 is damaged"):
        store.decide(session, "reference-neutral-01", 3, "kept")
    # Numbers are never reused.
    assert store.new_take_dir(session, "reference-neutral-01")[0] == lost.take_number + 1


def test_a_damaged_session_is_left_alone(store: PromptedStore) -> None:
    keep = new_session(store)
    broken = new_session(store, ("expressive-02",))
    record = store.session_dir(broken.session_id) / SESSION_FILE
    record.write_text(json.dumps({"session_id": broken.session_id}))
    assert store.sessions() == [keep]
    with pytest.raises(StoreError, match="is damaged"):
        store.load(broken.session_id)
    store.update(keep, state="abandoned")
    assert store.open_session() is None
    (store.sessions_root / "not-a-session").mkdir()
    assert [s.session_id for s in store.sessions()] == [keep.session_id]
    elsewhere = store.session_dir(keep.session_id) / SESSION_FILE
    data = json.loads(elsewhere.read_text())
    other = store.session_dir(broken.session_id) / SESSION_FILE
    other.write_text(json.dumps(data))
    with pytest.raises(StoreError, match="names another session"):
        store.load(broken.session_id)


def test_the_upload_state_lives_beside_the_package(store: PromptedStore) -> None:
    session = new_session(store)
    assert store.upload_state(session.session_id) == {}
    store.write_upload_state(session.session_id, {"state": "pending", "attempts": 1})
    path = store.upload_file(session.session_id)
    assert path.parent == store.packages_root and mode(path) == 0o600
    assert store.upload_state(session.session_id) == {"state": "pending", "attempts": 1}
    path.write_text("[1, 2]")
    assert store.upload_state(session.session_id) == {}
    assert sha256_file(path)[1] > 0
