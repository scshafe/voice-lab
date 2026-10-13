"""scripts/journey-parity: the comparison, the report's no-content rule, the fixture run."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from cole_voice_lab.journey import index as journey_index
from cole_voice_lab.journey import jsonio
from cole_voice_lab.journey.corpus import list_corpus_entries, list_measurement_entries

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "journey-parity"
PRIVATE_NAME = "20190101 120000-ABCDEF12.m4a"
PRIVATE_TEXT = "secret lyric line about the harbor"


def _load() -> ModuleType:
    loader = importlib.machinery.SourceFileLoader("journey_parity", str(SCRIPT))
    spec = importlib.util.spec_from_loader("journey_parity", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["journey_parity"] = module
    loader.exec_module(module)
    return module


parity = _load()


def _write(path: Path, payload: Any) -> Path:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def test_identical_documents_have_no_differences() -> None:
    document = {"a": [1, 2.5, None, True, "x"], "b": {"c": {}}}
    result = parity.compare_json(document, json.loads(json.dumps(document)))
    assert result.differences == []
    assert result.tolerated == 0


def test_structural_differences_by_kind() -> None:
    node = {"keep": 1, "gone": 1, "order": {"x": 1, "y": 2}, "list": [1, 2, 3], "t": 1, "s": "a"}
    python = {"keep": 2, "order": {"y": 2, "x": 1}, "list": [1, 2], "t": "1", "s": "b", "new": 0}
    result = parity.compare_json(node, python)
    kinds = {(d.general, d.kind) for d in result.differences}
    assert kinds == {
        ("$.gone", "missingInPython"),
        ("$.new", "extraInPython"),
        ("$.keep", "value"),
        ("$.order", "keyOrder"),
        ("$.list", "length"),
        ("$.t", "type"),
        ("$.s", "value"),
    }


def test_booleans_are_not_numbers() -> None:
    result = parity.compare_json({"v": True}, {"v": 1})
    assert [(d.general, d.kind) for d in result.differences] == [("$.v", "type")]


def test_float_tolerance() -> None:
    node, python = {"v": [0.1, 2]}, {"v": [0.1000004, 2]}
    exact = parity.compare_json(node, python)
    assert [(d.general, d.kind) for d in exact.differences] == [("$.v[*]", "value")]
    loose = parity.compare_json(node, python, tolerance=1e-6)
    assert loose.differences == []
    assert loose.tolerated == 1


def test_general_path_hides_indices_and_non_schema_keys() -> None:
    allowed = frozenset({"results", "features", "perYear"})
    segments = ["results", 3, PRIVATE_NAME, "features", "perYear", "2019", "2019-Q3", "word"]
    assert (
        parity.general_path(segments, allowed)
        == "$.results[*].<key>.features.perYear.2019.2019-Q3.<key>"
    )
    assert parity.general_path(["results", 3, "word"], None) == "$.results[*].word"
    assert parity.concrete_path(["results", 3, PRIVATE_NAME]) == (
        f"$.results[3][{json.dumps(PRIVATE_NAME)}]"
    )


def test_schema_keys_reads_key_positions_only(tmp_path: Path) -> None:
    source = tmp_path / "x.mjs"
    source.write_text(
        "// harbor: a comment\n/* lantern: block */\n"
        'const row = { recordingId: 1, "quoted": 2 };\nrow.features.voicing;\n',
        encoding="utf-8",
    )
    keys = parity.schema_keys([source])
    assert {"recordingId", "quoted", "features", "voicing"} <= keys
    assert "harbor" not in keys
    assert "lantern" not in keys


def test_report_entry_never_carries_content(tmp_path: Path) -> None:
    node = _write(
        tmp_path / "node.json",
        {
            "results": [{"filename": PRIVATE_NAME, "text": PRIVATE_TEXT}],
            "byName": {PRIVATE_TEXT: 1},
        },
    )
    python = _write(
        tmp_path / "python.json",
        {"results": [{"filename": PRIVATE_NAME, "text": "other"}], "byName": {PRIVATE_TEXT: 2}},
    )
    allowed = frozenset({"results", "filename", "text", "byName"})
    entry = parity.compare_file("manifests/x.json", node, python, tolerance=0.0, allowed=allowed)
    text = json.dumps(entry)
    for secret in ("secret", "harbor", "ABCDEF12", "20190101", "other"):
        assert secret not in text
    assert entry["nodeSha256"] == parity._sha256(node)
    assert entry["pythonSha256"] == parity._sha256(python)
    assert entry["byteIdentical"] is False
    assert entry["differences"] == 2
    assert entry["byPath"] == {
        "$.byName.<key>": {"count": 1, "kinds": {"value": 1}},
        "$.results[*].text": {"count": 1, "kinds": {"value": 1}},
    }
    assert "examples" not in entry
    shown = parity.compare_file(
        "manifests/x.json", node, python, tolerance=0.0, allowed=None, show_values=True
    )
    assert {
        "path": "$.results[0].text",
        "kind": "value",
        "node": PRIVATE_TEXT,
        "python": "other",
    } in (shown["examples"])


def test_ignore_rules_are_counted_apart(tmp_path: Path) -> None:
    node = _write(tmp_path / "node.json", {"updatedAt": "2026-01-01T00:00:00.000Z", "n": 1})
    python = _write(tmp_path / "python.json", {"updatedAt": "2026-01-01T00:00:09.000Z", "n": 1})
    entry = parity.compare_file(
        "manifests/full-corpus-filter-results.json", node, python, tolerance=0.0, allowed=None
    )
    assert entry["differences"] == 0
    assert entry["ignoredDifferences"] == 1
    assert "wall clock" in entry["ignoredByPath"]["$.updatedAt"]["reason"]
    other = parity.compare_file("manifests/song-journeys.json", node, python, tolerance=0.0,
                                allowed=None)  # fmt: skip
    assert other["differences"] == 1


INDEX_LABEL = "manifests/recording-index.json"


def _index_entry(node: Any, python: Any, tmp_path: Path, label: str = INDEX_LABEL) -> Any:
    left = _write(tmp_path / "node-index.json", node)
    right = _write(tmp_path / "python-index.json", python)
    return parity.compare_file(label, left, right, tolerance=0.0, allowed=None)


def test_the_index_source_rule_excuses_only_the_voice_memos_tag(tmp_path: Path) -> None:
    """D-051: Voice Memos rows gain {type: voice-memos}; nothing else on the path is excused."""

    measured = {"type": "measurement", "chainId": "umik1", "sessionId": "s", "segmentKey": None}
    node = {
        "recordings": [{"recordingId": "p", "file": {}}, {"recordingId": "m", "source": measured}]
    }
    tagged = {"type": "voice-memos"}
    python = {
        "recordings": [
            {"recordingId": "p", "file": {}, "source": tagged},
            {"recordingId": "m", "source": measured},
        ]
    }
    entry = _index_entry(node, python, tmp_path)
    assert (entry["differences"], entry["ignoredDifferences"]) == (0, 1)
    excused = entry["ignoredByPath"]["$.recordings[*].source"]
    assert excused["kinds"] == {"extraInPython": 1}
    assert "D-051" in excused["reason"]
    rule = next(rule for rule in parity.IGNORE_RULES if rule.file == INDEX_LABEL)
    assert (rule.kind, rule.python_value) == ("extraInPython", tagged)

    def unexpected(python_rows: list[Any], label: str = INDEX_LABEL) -> dict[str, Any]:
        found = _index_entry(node, {"recordings": python_rows}, tmp_path, label)
        assert found["differences"] == 1 and found["ignoredDifferences"] == 0
        return dict(found["byPath"])

    phone, measurement = python["recordings"]
    other_tag = {**phone, "source": {"type": "voice-memos", "chainId": "iphone-voicememos"}}
    assert unexpected([other_tag, measurement]) == {
        "$.recordings[*].source": {"count": 1, "kinds": {"extraInPython": 1}}
    }
    changed = {**measurement, "source": {**measured, "type": "voice-memos"}}
    assert unexpected([{"recordingId": "p", "file": {}}, changed]) == {
        "$.recordings[*].source.type": {"count": 1, "kinds": {"value": 1}}
    }
    assert unexpected([{"recordingId": "p", "file": {}}, {"recordingId": "m"}]) == {
        "$.recordings[*].source": {"count": 1, "kinds": {"missingInPython": 1}}
    }
    assert unexpected([phone, measurement], "manifests/full-corpus-filter-results.json") == {
        "$.recordings[*].source": {"count": 1, "kinds": {"extraInPython": 1}}
    }


def test_the_index_through_the_source_port_has_parity_with_the_listers(tmp_path: Path) -> None:
    """Offline stand-in for the Node side of the index stage: the index the listers built
    before the port (the bytes phase 3 found identical to Node's) against the port's."""

    phone = tmp_path / "phone"
    phone.mkdir()
    for name in ("20190301 090000-AAAA1111.m4a", "20200415 181500-BBBB2222.m4a", "memo.m4a"):
        (phone / name).write_text(name)
    session = tmp_path / "measurement" / "sessionA1"
    session.mkdir(parents=True)
    (session / "seg-1.wav").write_text("w")
    started = "2024-01-01T00:00:00.000Z"
    (session / "session.json").write_text(
        json.dumps({"chainId": "umik1", "startedAt": started, "files": [{"name": "seg-1.wav"}]})
    )
    stamp = "2026-01-01T00:00:00.000Z"
    roots = ["--corpus-root", str(phone), "--measurement-root", str(session.parent)]
    python = tmp_path / "python" / "recording-index.json"
    assert journey_index.main([*roots, "--out", str(python), "--generated-at", stamp]) == 0
    before = journey_index.build_manifest(
        [*list_corpus_entries(str(phone)), *list_measurement_entries(str(session.parent))],
        {"generatedAt": stamp},
    )
    node = tmp_path / "node" / "recording-index.json"
    node.parent.mkdir()
    node.write_text(jsonio.stringify(before) + "\n", encoding="utf-8")
    allowed = parity.schema_keys(sorted((REPO_ROOT / "src/cole_voice_lab/journey").glob("*.py")))
    entry = parity.compare_file(INDEX_LABEL, node, python, tolerance=0.0, allowed=allowed)
    assert entry["byteIdentical"] is False
    assert (entry["differences"], entry["ignoredDifferences"]) == (0, 3)
    assert entry["byPath"] == {}
    assert list(entry["ignoredByPath"]) == ["$.recordings[*].source"]


def test_byte_differences_without_structural_ones(tmp_path: Path) -> None:
    node = tmp_path / "node.json"
    python = tmp_path / "python.json"
    node.write_text('{"v": 1}\n', encoding="utf-8")
    python.write_text('{"v": 1.0}\n', encoding="utf-8")
    entry = parity.compare_file("manifests/x.json", node, python, tolerance=0.0, allowed=None)
    assert entry["byPath"] == {"$": {"count": 1, "kinds": {"formatting": 1}}}
    node.write_text("plain text\n", encoding="utf-8")
    python.write_text("plain text \n", encoding="utf-8")
    entry = parity.compare_file("artifacts/x.txt", node, python, tolerance=0.0, allowed=None)
    assert entry["byPath"] == {"$": {"count": 1, "kinds": {"bytes": 1}}}
    python.write_text("plain text\n", encoding="utf-8")
    entry = parity.compare_file("artifacts/x.txt", node, python, tolerance=0.0, allowed=None)
    assert entry["byteIdentical"] is True
    assert entry["differences"] == 0


def test_missing_files_are_differences(tmp_path: Path) -> None:
    present = _write(tmp_path / "a.json", {})
    entry = parity.compare_file("manifests/a.json", present, None, tolerance=0.0, allowed=None)
    assert entry["pythonSha256"] is None
    assert entry["byPath"] == {"$": {"count": 1, "kinds": {"missingInPython": 1}}}
    entry = parity.compare_file("manifests/a.json", None, present, tolerance=0.0, allowed=None)
    assert entry["byPath"] == {"$": {"count": 1, "kinds": {"missingInNode": 1}}}


def test_file_labels_hide_recording_names() -> None:
    label = parity.artifact_label(Path("stt-transcripts/0123456789abcdef.txt"), reveal=False)
    assert label.startswith("artifacts/stt-transcripts/#")
    assert label.endswith(".txt")
    assert "0123456789abcdef" not in label
    odd = parity.artifact_label(Path(f"elsewhere/{PRIVATE_NAME}"), reveal=False)
    assert odd.startswith("artifacts/#")
    assert "ABCDEF12" not in odd
    assert parity.artifact_label(Path("voice-features/a.json"), reveal=True) == (
        "artifacts/voice-features/a.json"
    )
    assert parity.manifest_label("song-journeys.json") == "manifests/song-journeys.json"
    assert parity.manifest_label(PRIVATE_NAME).startswith("manifests/#")


def test_data_root_inside_the_repository_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    code = parity.main(["--data-root", str(REPO_ROOT / "docs"), "--approval", "test"])
    assert code == 2
    assert "inside the repository" in capsys.readouterr().err


def test_data_root_requires_approval(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert parity.main(["--data-root", str(tmp_path)]) == 2
    assert "--approval" in capsys.readouterr().err


def test_fixture_only_flags_are_refused_with_a_data_root(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as refused:
        parity.main(["--data-root", str(tmp_path), "--approval", "x", "--show-values"])
    assert refused.value.code == 2
    with pytest.raises(SystemExit):
        parity.main(["--fixture", "--approval", "x"])


def _fixture_names() -> list[str]:
    names = []
    takes = [*parity.FIXTURE_TAKES, parity.FIXTURE_HELD_BACK]
    for index, take in enumerate(takes):
        stamp = take[0]
        names.append(f"{stamp}-{parity._suffix(stamp, index)}.m4a")
    return names


def _skip_reason() -> str | None:
    for tool in ("node", "ffmpeg", "git"):
        if shutil.which(tool) is None:
            return f"{tool} is not installed"
    if not parity.commit_available():
        return f"commit {parity.PINNED_COMMIT} is not in this checkout (shallow clone)"
    return None


@pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")
def test_fixture_pipeline_has_parity(tmp_path: Path) -> None:
    out = tmp_path / "report.json"
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--fixture", "--out", str(out)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
    text = out.read_text(encoding="utf-8")
    report = json.loads(text)
    summary = report["summary"]
    assert report["ok"] is True
    assert summary["differences"] == 0
    assert summary["failedStages"] == []
    assert set(summary["stages"].values()) == {"identical"}
    assert len(summary["stages"]) == 14
    assert summary["artifacts"] > 0
    assert report["tools"]["whisperCli"].startswith("fake")
    assert report["tools"]["analysisWorker"].startswith("fake")
    for name in _fixture_names():
        assert name not in text
        assert name.split("-")[1][:8] not in text
        assert parity._recording_id(name) not in text
    for song in parity.SONGS.values():
        for line in song.lines:
            assert line not in text
