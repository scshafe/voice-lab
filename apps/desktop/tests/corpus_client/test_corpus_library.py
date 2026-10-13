"""The PC's library on this machine (``corpus/library.py``): each pass that reached the PC
reads it last, with ``If-None-Match`` (a ``304`` rewrites nothing), also while sending is
held; it is kept on disk, private and canonical, so a later run offline still has it; a PC
without a library leaves the last one; a library that is not Cole's is not kept."""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Any

from corpus_rig import PC, Scripted, client, record

from cole_voice_lab.canonical import canonical_json_bytes


def test_each_pass_reads_the_pcs_library_and_keeps_it(pc: PC, data_dir: Path, library: Any) -> None:
    corpus = client(data_dir, pc, enabled=False)
    assert corpus.library.latest is None and corpus.library.checked_at is None
    result = corpus.run_pass()
    assert result.gated and result.library_changed and result.library_error == ""
    cached = corpus.library.latest
    assert cached is not None and cached.library == pc.library()
    assert cached.library.catalog == pc.catalog[0] and cached.library.coverage.prompts == 34
    path = data_dir / "corpus" / "library" / "library.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert path.read_bytes() == canonical_json_bytes(cached) + b"\n"
    # Unchanged on the PC: a 304; nothing is rewritten.
    written = path.stat().st_mtime_ns, path.stat().st_ino
    checked = corpus.library.checked_at
    again = corpus.run_pass()
    assert not again.library_changed and corpus.library.latest == cached
    assert (path.stat().st_mtime_ns, path.stat().st_ino) == written
    assert corpus.library.checked_at is not None and checked is not None
    assert corpus.library.checked_at >= checked

    # A take reaches the PC and is kept: the next pass reads the changed library.
    sending = client(data_dir, pc, enabled=True)
    take = record(sending, pc.catalog[1].suite.prompts[0].prompt_id, seed=31)
    sending.decide(take.take_id, "kept")
    result = sending.run_pass()
    assert result.uploaded == 1 and result.applied == 1 and result.library_changed
    latest = sending.library.latest
    assert latest is not None and latest.library.coverage.kept == 1
    assert latest.library.rows[0].latest_take_id == take.take_id

    # Offline, a new run still has the library as last read.
    pc.stop()
    later = client(data_dir, pc)
    assert later.library.latest == latest
    assert later.run_pass().offline and later.library.latest == latest


def test_a_pc_without_a_library_keeps_the_last_one(tmp_path: Path, data_dir: Path, pc: PC) -> None:
    corpus = client(data_dir, pc, enabled=False)
    corpus.run_pass()
    cached = corpus.library.latest
    assert cached is not None and not corpus.library.unavailable
    bare = PC.create(tmp_path / "bare", seeded=False).start()
    try:
        moved = client(data_dir, bare, enabled=False)
        result = moved.run_pass()
        assert result.reached and result.library_error == ""
        assert moved.library.unavailable and moved.library.latest == cached
    finally:
        bare.stop()


def test_a_library_that_is_not_coles_is_not_kept(pc: PC, data_dir: Path) -> None:
    corpus = client(data_dir, pc, enabled=False)
    real = corpus.engine.api

    def someone_elses(tag: str | None = None) -> Any:
        answer = real().library_if_changed(tag)
        assert answer.library is not None
        library = answer.library.model_copy(update={"subject_id": "someone-else"})
        return type(answer)(answer.tag, library)

    corpus.engine.api = lambda: Scripted(real(), library_if_changed=someone_elses)
    result = corpus.run_pass()
    assert result.reached and "not cole-self's" in result.library_error
    assert corpus.library.latest is None
    assert not (data_dir / "corpus" / "library" / "library.json").exists()
