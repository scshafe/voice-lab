"""Coverage data files that a process left unreadable.

Once the tests have run, pytest-cov combines every ``.coverage.*`` file beside its data file:
the xdist workers' and those of the subprocesses the tests start. A file a process created and
never wrote has no ``coverage_schema`` table; coverage then writes the schema into it, and
when the file is also read-only (its process ran under a restrictive file mode), sqlite's
"attempt to write a readonly database" is not an error coverage's combine catches (up to
7.17.2; 7.17.3 skips such a file with a warning): the whole run ends in an INTERNALERROR
although every test passed. It did so three times on 2026-11-03 (two CI runs and a deploy's
verify); which test's process leaves it is not known.
Such a file holds no data, so the suite's controller drops it before pytest-cov combines, and
says which (``tests/conftest.py``).
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path


def unreadable(path: Path) -> bool:
    """``path`` has no coverage schema (empty, or created and never written), or is no
    database at all. A file another process holds (locked) is not counted as unreadable."""
    try:
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
            db.execute("select version from coverage_schema").fetchone()
    except sqlite3.Error as exc:
        message = str(exc)
        return "no such table" in message or "not a database" in message
    return False


def drop_unreadable(data_file: Path) -> list[Path]:
    """Remove the parallel data files beside ``data_file`` (``<data_file>.*``) that are
    unreadable; return them."""
    dropped = []
    for path in sorted(data_file.parent.glob(data_file.name + ".*")):
        if path.is_file() and not path.is_symlink() and unreadable(path):
            path.unlink(missing_ok=True)
            dropped.append(path)
    return dropped
