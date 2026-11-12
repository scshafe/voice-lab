"""The suite drops the coverage data files a killed process left without a schema before
pytest-cov combines them (``coverage_files``); a file with data is never touched."""

from __future__ import annotations

import sqlite3
import warnings
from contextlib import closing
from pathlib import Path

import coverage_files
from coverage import Coverage, CoverageData
from coverage.exceptions import CoverageWarning


def test_only_the_files_left_without_a_schema_are_dropped(tmp_path: Path) -> None:
    data_file = tmp_path / ".coverage"
    good = CoverageData(str(tmp_path / ".coverage.host.pid1.good"))
    good.add_lines({str(tmp_path / "module.py"): [1, 2, 3]})
    good.write()
    # As in the failed runs: created, never written, and read-only.
    empty = tmp_path / ".coverage.host.pid2.empty"
    empty.write_bytes(b"")
    empty.chmod(0o444)
    schemaless = tmp_path / ".coverage.host.pid3.schemaless"
    with closing(sqlite3.connect(schemaless)) as db:
        db.execute("create table other (x)")
        db.commit()
    garbage = tmp_path / ".coverage.host.pid4.garbage"
    garbage.write_bytes(b"not sqlite at all, but sixteen-plus bytes long")
    unrelated = tmp_path / "coverage.host.pid5"
    unrelated.write_bytes(b"")
    # Left there, the read-only empty file stopped the combine up to coverage 7.17.2 (pytest-cov's
    # INTERNALERROR); 7.17.3 skips it with a warning.
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", CoverageWarning)
            Coverage(data_file=str(data_file)).combine([str(empty)], keep=True)
    except sqlite3.OperationalError as error:
        assert "readonly database" in str(error)
    assert [p.name for p in coverage_files.drop_unreadable(data_file)] == [
        empty.name,
        schemaless.name,
        garbage.name,
    ]
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        ".coverage.host.pid1.good",
        "coverage.host.pid5",
    ]
    assert not coverage_files.unreadable(tmp_path / ".coverage.host.pid1.good")
    # What is left combines, as pytest-cov then does.
    combined = Coverage(data_file=str(data_file))
    combined.combine([str(tmp_path)], strict=True)
    combined.save()
    reread = CoverageData(str(data_file))
    reread.read()
    assert reread.lines(str(tmp_path / "module.py")) == [1, 2, 3]
