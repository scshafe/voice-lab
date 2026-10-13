"""The PC's catalog, kept on this machine and pinned for a session (D-052, RECORDING-V1.md §3).

``<data dir>/corpus/catalog/``:

- ``views/<snapshot_sha256>.json``: a ``CatalogView`` the PC served, by its snapshot's digest.
  It is checked before it is kept and every time it is read: the suite's canonical digest is
  the reference's ``catalog_sha256``, and the snapshot the view describes (its suite, digest
  and aliases, canonical) hashes to the reference's ``snapshot_sha256``, the PC's own file
  digest. So a cached view is exactly the version the PC published.
- ``state.json`` (``CatalogCacheState``): the PC's active version as last seen, and when.

``refresh`` asks the PC with ``If-None-Match`` naming the version last seen: a ``304`` changes
nothing; a new version is kept and becomes ``latest``. A session (``open_session``: the app's
start, or the UI's explicit switch) pins ``latest``; every take of the session is recorded
against the pinned version, so a take never mixes versions. When the PC's active version
changes during a session, ``switch_pending`` says the next session records against it.

With no cached view nothing can be recorded until the PC answers; with one, recording works
offline.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from cole_voice_lab.api.corpus_contracts import CatalogView
from cole_voice_lab.canonical import (
    canonical_json_bytes,
    read_canonical_model,
    write_canonical_json,
)
from cole_voice_lab.errors import ContractError, SecurityError
from cole_voice_lab.models import COLE_SUBJECT, Prompt, utc_now
from cole_voice_lab.recording_contracts import CatalogReference, CatalogSnapshot

from ..api import ApiClient, ApiUnavailable
from .files import private_dir
from .records import CatalogCacheState

STATE_FILE: Final = "state.json"
VIEWS: Final = "views"
#: A 1,886-prompt view is about a megabyte; the PC's snapshot bound is 16 MiB.
MAX_VIEW_BYTES: Final = 32 * 1024 * 1024
MAX_STATE_BYTES: Final = 64 * 1024
_DIGEST: Final = re.compile(r"[0-9a-f]{64}\Z")


class CatalogError(Exception):
    """A catalog view that is not the version it names, or a cache that cannot be read."""


@dataclass(frozen=True)
class PinnedCatalog:
    """One catalog version, checked: its view and the snapshot it describes."""

    view: CatalogView
    snapshot: CatalogSnapshot

    @property
    def reference(self) -> CatalogReference:
        return self.view.reference

    @property
    def snapshot_sha256(self) -> str:
        return self.view.reference.snapshot_sha256

    def prompt(self, prompt_id: str) -> Prompt:
        """The version's prompt ``prompt_id``. Raises: ``KeyError``."""
        for prompt in self.snapshot.suite.prompts:
            if prompt.prompt_id == prompt_id:
                return prompt
        raise KeyError(prompt_id)


def check_view(view: CatalogView) -> PinnedCatalog:
    """``view`` once it is shown to be exactly the version its reference names.

    Raises: ``CatalogError``."""
    reference = view.reference
    if view.subject_id != COLE_SUBJECT:
        raise CatalogError(f"the catalog is not {COLE_SUBJECT}'s (D-050)")
    try:
        snapshot = CatalogSnapshot(
            suite=view.suite,
            catalog_sha256=reference.catalog_sha256,
            prompt_aliases=view.prompt_aliases,
        )
    except ValidationError as exc:
        raise CatalogError(f"the catalog view does not describe its version: {exc}") from exc
    digest = hashlib.sha256(canonical_json_bytes(snapshot) + b"\n").hexdigest()
    if digest != reference.snapshot_sha256:
        raise CatalogError("the catalog view is not the snapshot its reference names")
    if (view.suite.suite_id, view.suite.purpose) != (reference.suite_id, reference.purpose):
        raise CatalogError("the catalog view's suite is not the one its reference names")
    return PinnedCatalog(view, snapshot)


@dataclass(frozen=True)
class RefreshResult:
    """What one ``refresh`` learned: whether the PC's active version changed since it was
    last seen (``changed``) and whether the PC has none (``unavailable``)."""

    changed: bool
    unavailable: bool = False


class CatalogCache:
    """The catalog views under ``root`` (``<data dir>/corpus/catalog``) and the session's
    pinned version. Safe to call from the GUI thread and the sync worker."""

    def __init__(self, root: Path, *, clock: Callable[[], datetime] = utc_now) -> None:
        self.root = Path(root)
        self.clock = clock
        self._lock = threading.RLock()
        self._pinned: PinnedCatalog | None = None
        self._latest: PinnedCatalog | None = None
        self._state = CatalogCacheState()
        self._loaded = False

    # -- files ------------------------------------------------------------------------------

    def _view_path(self, digest: str) -> Path:
        if not _DIGEST.match(digest):
            raise CatalogError(f"not a snapshot digest: {digest!r}")
        return self.root / VIEWS / f"{digest}.json"

    def _read_view(self, digest: str) -> PinnedCatalog:
        path = self._view_path(digest)
        try:
            view = read_canonical_model(path, CatalogView, max_bytes=MAX_VIEW_BYTES)
        except (OSError, ContractError, SecurityError) as exc:
            raise CatalogError(f"the cached catalog {digest[:12]}… cannot be read: {exc}") from exc
        pinned = check_view(view)
        if pinned.snapshot_sha256 != digest:
            raise CatalogError(f"the cached catalog {digest[:12]}… names another version")
        return pinned

    def _write_state(self, state: CatalogCacheState) -> None:
        write_canonical_json(private_dir(self.root) / STATE_FILE, state)
        self._state = state

    def _load(self) -> None:
        """The state and the latest view, once; a damaged cache is set aside as empty."""
        if self._loaded:
            return
        self._loaded = True
        path = self.root / STATE_FILE
        if not os.path.lexists(path):
            return
        try:
            self._state = read_canonical_model(path, CatalogCacheState, max_bytes=MAX_STATE_BYTES)
        except (OSError, ContractError, SecurityError):
            self._state = CatalogCacheState()
            return
        if self._state.latest is not None:
            try:
                self._latest = self._read_view(self._state.latest)
            except CatalogError:
                self._latest = None
                self._state = self._state.model_copy(update={"latest": None})

    # -- the session --------------------------------------------------------------------------

    def open_session(self) -> PinnedCatalog | None:
        """Pin the PC's active version as last seen for a new session (``None`` while none
        was ever seen) and forget older views."""
        with self._lock:
            self._load()
            self._pinned = self._latest
            self._prune()
            return self._pinned

    def _prune(self) -> None:
        keep = {p.snapshot_sha256 for p in (self._pinned, self._latest) if p is not None}
        views = self.root / VIEWS
        if not views.is_dir():
            return
        for path in views.iterdir():
            if path.suffix == ".json" and path.stem not in keep and _DIGEST.match(path.stem):
                path.unlink(missing_ok=True)

    @property
    def pinned(self) -> PinnedCatalog | None:
        """The version this session records against."""
        with self._lock:
            return self._pinned

    @property
    def latest(self) -> PinnedCatalog | None:
        """The PC's active version, as last seen."""
        with self._lock:
            self._load()
            return self._latest

    @property
    def state(self) -> CatalogCacheState:
        with self._lock:
            self._load()
            return self._state

    @property
    def switch_pending(self) -> bool:
        """The PC's active version is not the pinned one: the next session switches."""
        with self._lock:
            self._load()
            latest, pinned = self._latest, self._pinned
            return (
                latest is not None
                and pinned is not None
                and latest.snapshot_sha256 != pinned.snapshot_sha256
            )

    # -- the PC ---------------------------------------------------------------------------------

    def refresh(self, api: ApiClient) -> RefreshResult:
        """Ask the PC for its active version (``If-None-Match``: the one last seen).

        A new version is kept and becomes ``latest``; it is also pinned at once when this
        session had none (no take could have been recorded yet).
        Raises: ``ApiUnavailable``/``SignInRequired``/``ApiError`` as the API's;
        ``CatalogError`` for a view that is not the version it names (nothing is kept)."""
        with self._lock:
            self._load()
            known = self._state.latest
        try:
            answer = api.catalog(known)
        except ApiUnavailable as exc:
            if exc.code != "catalog_unavailable":
                raise
            with self._lock:
                self._write_state(
                    self._state.model_copy(
                        update={"checked_at": self.clock(), "pc_unavailable": True}
                    )
                )
            return RefreshResult(changed=False, unavailable=True)
        now = self.clock()
        if answer.view is None:
            with self._lock:
                self._write_state(
                    self._state.model_copy(update={"checked_at": now, "pc_unavailable": False})
                )
            return RefreshResult(changed=False)
        pinned = check_view(answer.view)
        with self._lock:
            path = self._view_path(pinned.snapshot_sha256)
            private_dir(path.parent)
            write_canonical_json(path, answer.view)
            changed = known != pinned.snapshot_sha256
            self._latest = pinned
            if self._pinned is None:
                self._pinned = pinned
            self._write_state(
                CatalogCacheState(
                    latest=pinned.snapshot_sha256, checked_at=now, pc_unavailable=False
                )
            )
            return RefreshResult(changed=changed)
