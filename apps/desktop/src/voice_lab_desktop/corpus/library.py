"""The PC's library, kept on this machine (D-052; RECORDING-V1.md step 6, the studio's coverage).

``<data dir>/corpus/library/library.json`` (``CachedLibrary``): the PC's ``CorpusLibrary`` of
its active catalog version as last read (each prompt's status and take count, and the
coverage totals), its ``ETag`` (the PC's read-model digest) and when it was read. It carries
no audio and no prompt text.

The sync engine refreshes it at the end of every pass that reached the PC, with
``If-None-Match`` naming the tag last read: a ``304`` changes nothing, and nothing is written.
Reading it sends nothing of the corpus, so it is read while sending is held too, like the
catalog. The prompt studio counts coverage from it and the local spool, so a studio opened
offline still knows what the PC had. A cache that cannot be read is set aside as empty: the
next pass reads the PC's again.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Final

from cole_voice_lab.canonical import read_canonical_model, write_canonical_json
from cole_voice_lab.errors import ContractError, SecurityError
from cole_voice_lab.models import COLE_SUBJECT, utc_now

from ..api import ApiClient, ApiUnavailable
from .files import private_dir
from .records import CachedLibrary

LIBRARY_FILE: Final = "library.json"
#: A 1,886-prompt library is under a megabyte.
MAX_LIBRARY_BYTES: Final = 32 * 1024 * 1024
#: The PC's answers while it has no library to serve (yet).
UNAVAILABLE_CODES: Final = frozenset({"catalog_unavailable", "library_unavailable"})


class LibraryError(Exception):
    """A library the PC served that is not Cole's."""


class LibraryCache:
    """The PC's library under ``root`` (``<data dir>/corpus/library``). Safe to call from the
    GUI thread and the sync worker."""

    def __init__(self, root: Path, *, clock: Callable[[], datetime] = utc_now) -> None:
        self.root = Path(root)
        self.clock = clock
        self._lock = threading.RLock()
        self._latest: CachedLibrary | None = None
        self._loaded = False
        self._checked_at: datetime | None = None
        self._unavailable = False

    def _load(self) -> None:
        if self._loaded:
            return
        path = self.root / LIBRARY_FILE
        try:
            if path.exists():
                self._latest = read_canonical_model(
                    path, CachedLibrary, max_bytes=MAX_LIBRARY_BYTES
                )
        except (OSError, ContractError, SecurityError):
            self._latest = None
        self._loaded = True  # last: peek() never sees it loaded before it is

    @property
    def loaded(self) -> bool:
        """The cache was read (``latest`` no longer parses the file)."""
        return self._loaded

    def peek(self) -> CachedLibrary | None:
        """``latest`` when the cache was read, else ``None`` without reading it (the GUI
        thread; ``CorpusClient.prepare`` reads it on a worker)."""
        return self._latest if self._loaded else None

    @property
    def latest(self) -> CachedLibrary | None:
        """The PC's library as last read (``None`` until it was read once)."""
        with self._lock:
            self._load()
            return self._latest

    @property
    def checked_at(self) -> datetime | None:
        """When this run last heard the PC's library (read or unchanged)."""
        with self._lock:
            return self._checked_at

    @property
    def unavailable(self) -> bool:
        """The PC answered that it has no library to serve right now."""
        with self._lock:
            return self._unavailable

    def refresh(self, api: ApiClient) -> bool:
        """Ask the PC for its library; ``True`` when it changed since it was last read.

        Raises: what ``ApiClient.library_if_changed`` raises, but ``ApiUnavailable`` for a PC
        that has no library yet (``unavailable`` says so, and the last one is kept);
        ``LibraryError`` for a library that is not Cole's (nothing is kept)."""
        with self._lock:
            self._load()
            known = self._latest.tag if self._latest is not None else None
        try:
            answer = api.library_if_changed(known)
        except ApiUnavailable as exc:
            if exc.code not in UNAVAILABLE_CODES:
                raise
            with self._lock:
                self._unavailable = True
                self._checked_at = self.clock()
            return False
        now = self.clock()
        if answer.library is None:
            with self._lock:
                self._unavailable = False
                self._checked_at = now
            return False
        if answer.library.subject_id != COLE_SUBJECT:
            raise LibraryError(f"the PC's library is not {COLE_SUBJECT}'s (D-050)")
        cached = CachedLibrary(tag=answer.tag, fetched_at=now, library=answer.library)
        with self._lock:
            write_canonical_json(private_dir(self.root) / LIBRARY_FILE, cached)
            self._latest = cached
            self._unavailable = False
            self._checked_at = now
        return known != answer.tag
