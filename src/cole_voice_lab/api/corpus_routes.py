"""The corpus routes (D-052, docs/design/RECORDING-V1.md): the catalog, the library, the takes.

@canonical api.corpus_routes -- the API's lab mount is read-only, so these routes read only
what the PC's one lab writer (the intake publisher) keeps for them: the read model
(``corpus_read_model``), the content-addressed catalog snapshots, which
``corpus_store.read_catalog_snapshot`` checks byte for byte against the reference, the take
records (``corpus_take_store``) and the published raw take packages. They never open the
corpus store. The subject is the lab's one subject (``COLE_SUBJECT``, D-050).

``GET|HEAD /api/v1/catalog``:

- 200 ``CatalogView``, with ``ETag: "<snapshot_sha256>"``;
- 304 when ``If-None-Match`` names that tag (the client already holds this version);
- 503 ``catalog_unavailable`` until a catalog is active on the PC (the owner seeds it,
  RECORDING-V1.md §7);
- 500 ``catalog_invalid`` when the read model or the snapshot it names fails its checks.

``GET|HEAD /api/v1/corpus/library`` and ``GET|HEAD /api/v1/corpus/prompts/{prompt_id}/takes``:

- 200 ``CorpusLibrary`` / ``TakeHistory``, with ``ETag: "<read model digest>"`` (the
  library file's SHA-256, which changes exactly when its content does), or 304 for that tag;
- 404 ``prompt_not_found`` for a prompt the active version does not hold;
- 503 ``catalog_unavailable`` as above, and ``library_unavailable`` while the library of the
  active version is not built yet (the next intake pass, or ``voice-lab catalog activate``,
  builds it);
- 500 ``catalog_invalid``/``library_invalid`` when the read model fails its checks.

``GET|HEAD /api/v1/corpus/takes/{take_id}/audio``: the take's ``original.wav`` from its raw
package, with ``Accept-Ranges: bytes``, ``ETag: "<audio sha256>"`` and a single byte range
answered 206 (416 ``range_not_satisfiable`` when it is outside the file). Playback needs the
recorded operator approval (``serve --playback-approval``, the same one journey playback
needs): without it every take's audio is refused, 403 ``playback_not_approved``. 404
``take_not_found`` for a take the PC has no record of; 500 ``take_invalid`` when the record or
its raw file fails its checks.

Like every ``/api/`` route, these are reached only with a verified bearer token (``auth.py``).
The library is parsed once per change of its file and kept, so a 1,886-prompt library with
thousands of takes is not re-read for every request.
"""

from __future__ import annotations

import os
import re
import stat
import threading
from typing import BinaryIO, Final

from ..canonical import canonical_json_bytes, open_regular_no_follow
from ..catalog_ports import CorpusStoreError
from ..corpus_read_model import (
    CorpusLibraryRecord,
    LibraryTake,
    library_path,
    read_active_catalog,
    read_library,
)
from ..corpus_store import read_catalog_snapshot
from ..corpus_take_store import CorpusTakeState, read_take_state
from ..corpus_takes import AUDIO_NAME, TAKE_ID_PATTERN
from ..errors import ContractError, SecurityError
from ..models import COLE_SUBJECT, ContractModel
from ..paths import AssetLayout, confined_path
from .corpus_contracts import CatalogView, CorpusLibrary, TakeHistory
from .routing import RouteRefusal, RouteRequest, RouteResponse

CATALOG_PATH: Final = "/api/v1/catalog"
LIBRARY_PATH: Final = "/api/v1/corpus/library"
_TAKES_PATH: Final = re.compile(r"^/api/v1/corpus/prompts/(?P<prompt_id>[^/]+)/takes$")
_AUDIO_PATH: Final = re.compile(r"^/api/v1/corpus/takes/(?P<take_id>[^/]+)/audio$")
_PROMPT_ID: Final = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
_RANGE: Final = re.compile(r"^bytes=(?P<start>[0-9]{0,15})-(?P<end>[0-9]{0,15})$")
_METHODS: Final = ("GET", "HEAD")


def _matches(header: str | None, tag: str) -> bool:
    """Whether an ``If-None-Match`` header names ``tag`` (weak or strong, or ``*``)."""
    if header is None:
        return False
    candidates = {candidate.strip().removeprefix("W/") for candidate in header.split(",")}
    return "*" in candidates or tag in candidates


def byte_range(header: str | None, size: int) -> tuple[int, int] | None:
    """The inclusive ``(start, end)`` of a single ``Range: bytes=…``, or ``None`` for all.

    Several ranges, another unit, a header that is not a byte range or an invalid one (its last
    byte before its first) are answered with the whole file (RFC 9110 §14.2: a server may
    ignore ``Range``, and ignores an invalid one); a single byte range the file cannot satisfy
    is refused.
    Raises: ``RouteRefusal`` 416 ``range_not_satisfiable`` (with ``Content-Range: bytes
    */<size>``).
    """
    if header is None:
        return None
    match = _RANGE.fullmatch(header.strip())
    if match is None or (not match["start"] and not match["end"]):
        return None
    unsatisfiable = RouteRefusal(
        416,
        "range_not_satisfiable",
        f"the audio is {size} bytes",
        (("Content-Range", f"bytes */{size}"),),
    )
    if not match["start"]:
        suffix = int(match["end"])
        if suffix == 0:
            raise unsatisfiable
        return max(0, size - suffix), size - 1
    start = int(match["start"])
    if match["end"] and int(match["end"]) < start:
        return None  # an invalid range, ignored
    if start >= size:
        raise unsatisfiable
    end = int(match["end"]) if match["end"] else size - 1
    return start, min(end, size - 1)


class _LibraryView:
    """One parsed library file: the record, its digest and each prompt's takes."""

    def __init__(self, library: CorpusLibraryRecord, digest: str) -> None:
        self.library = library
        self.tag = f'"{digest}"'
        self.rows = {row.prompt_id: row for row in library.rows}
        self.takes: dict[str, tuple[LibraryTake, ...]] = {
            history.prompt_id: history.takes for history in library.histories
        }


class CorpusRoutes:
    """The corpus routes over the lab at ``layout``, for ``subject_id``.

    ``playback_approval`` is the operator's recorded approval for playback; without it no
    take's audio is served."""

    def __init__(
        self,
        layout: AssetLayout,
        subject_id: str = COLE_SUBJECT,
        *,
        playback_approval: str | None = None,
    ) -> None:
        self.layout = layout
        self.subject_id = subject_id
        self.playback_approval = playback_approval or None
        self._lock = threading.Lock()
        self._cached: tuple[tuple[int, ...], _LibraryView] | None = None

    def __call__(self, request: RouteRequest) -> RouteResponse | None:
        if request.path == CATALOG_PATH:
            self._methods(request)
            return self._catalog(request)
        if request.path == LIBRARY_PATH:
            self._methods(request)
            return self._library(request)
        if (match := _TAKES_PATH.fullmatch(request.path)) is not None:
            self._methods(request)
            return self._history(request, match["prompt_id"])
        if (match := _AUDIO_PATH.fullmatch(request.path)) is not None:
            self._methods(request)
            return self._audio(request, match["take_id"])
        return None

    @staticmethod
    def _methods(request: RouteRequest) -> None:
        if request.method not in _METHODS:
            allowed = ", ".join(_METHODS)
            raise RouteRefusal(405, "method_not_allowed", f"this route answers {allowed}")

    def _catalog(self, request: RouteRequest) -> RouteResponse:
        try:
            record = read_active_catalog(self.layout, self.subject_id)
        except (ContractError, SecurityError, OSError) as error:
            raise RouteRefusal(500, "catalog_invalid", f"the corpus read model: {error}") from error
        if record is None:
            raise RouteRefusal(
                503,
                "catalog_unavailable",
                "no catalog is active on this server yet (docs/design/RECORDING-V1.md §7)",
            )
        tag = f'"{record.reference.snapshot_sha256}"'
        headers = (("ETag", tag),)
        if _matches(request.header("If-None-Match"), tag):
            return RouteResponse(status=304, route="corpus.catalog", headers=headers)
        try:
            snapshot = read_catalog_snapshot(self.layout, record.reference)
        except (CorpusStoreError, SecurityError, OSError) as error:
            raise RouteRefusal(500, "catalog_invalid", f"the catalog snapshot: {error}") from error
        view = CatalogView(
            subject_id=record.subject_id,
            reference=record.reference,
            suite=snapshot.suite,
            prompt_aliases=snapshot.prompt_aliases,
            refreshed_at=record.refreshed_at,
        )
        return self._json("corpus.catalog", view, headers)

    @staticmethod
    def _json(
        route: str, model: ContractModel, headers: tuple[tuple[str, str], ...]
    ) -> RouteResponse:
        return RouteResponse(
            status=200, route=route, body=canonical_json_bytes(model) + b"\n", headers=headers
        )

    def _current_library(self) -> _LibraryView:
        """The library of the active version, parsed once per change of its file."""
        try:
            record = read_active_catalog(self.layout, self.subject_id)
        except (ContractError, SecurityError, OSError) as error:
            raise RouteRefusal(500, "catalog_invalid", f"the corpus read model: {error}") from error
        if record is None:
            raise RouteRefusal(
                503,
                "catalog_unavailable",
                "no catalog is active on this server yet (docs/design/RECORDING-V1.md §7)",
            )
        path = library_path(self.layout, self.subject_id)
        try:
            metadata = os.lstat(path)
        except FileNotFoundError:
            metadata = None
        except OSError as error:
            raise RouteRefusal(500, "library_invalid", f"the corpus library: {error}") from error
        key = (
            None
            if metadata is None
            else (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
            )
        )
        with self._lock:
            if key is not None and self._cached is not None and self._cached[0] == key:
                view = self._cached[1]
            else:
                try:
                    found = read_library(self.layout, self.subject_id)
                except (ContractError, SecurityError, OSError) as error:
                    raise RouteRefusal(
                        500, "library_invalid", f"the corpus library: {error}"
                    ) from error
                if found is None or key is None:
                    raise RouteRefusal(
                        503,
                        "library_unavailable",
                        "the library is built by the next intake pass on this server",
                    )
                view = _LibraryView(*found)
                self._cached = (key, view)
        if view.library.catalog != record.reference:
            raise RouteRefusal(
                503,
                "library_unavailable",
                f"the library is being rebuilt for catalog version "
                f"{record.reference.catalog_version}",
            )
        return view

    def _library(self, request: RouteRequest) -> RouteResponse:
        view = self._current_library()
        headers = (("ETag", view.tag),)
        if _matches(request.header("If-None-Match"), view.tag):
            return RouteResponse(status=304, route="corpus.library", headers=headers)
        library = view.library
        answer = CorpusLibrary(
            subject_id=library.subject_id,
            catalog=library.catalog,
            coverage=library.coverage,
            rows=library.rows,
        )
        return self._json("corpus.library", answer, headers)

    def _history(self, request: RouteRequest, prompt_id: str) -> RouteResponse:
        view = self._current_library()
        row = view.rows.get(prompt_id) if _PROMPT_ID.fullmatch(prompt_id) else None
        if row is None:
            raise RouteRefusal(
                404, "prompt_not_found", "the active catalog version has no such prompt"
            )
        headers = (("ETag", view.tag),)
        if _matches(request.header("If-None-Match"), view.tag):
            return RouteResponse(status=304, route="corpus.takes", headers=headers)
        answer = TakeHistory(
            subject_id=view.library.subject_id,
            catalog=view.library.catalog,
            row=row,
            takes=view.takes.get(prompt_id, ()),
        )
        return self._json("corpus.takes", answer, headers)

    def _audio(self, request: RouteRequest, take_id: str) -> RouteResponse:
        if self.playback_approval is None:
            raise RouteRefusal(
                403, "playback_not_approved", "playback needs the recorded playback approval"
            )
        if not re.fullmatch(TAKE_ID_PATTERN, take_id):
            raise RouteRefusal(404, "take_not_found", "no take has this id")
        try:
            state = read_take_state(self.layout, take_id)
        except (ContractError, SecurityError, OSError) as error:
            raise RouteRefusal(500, "take_invalid", f"the take's record: {error}") from error
        if state is None or state.subject_id != self.subject_id:
            raise RouteRefusal(404, "take_not_found", "this server has no record of the take")
        stream, size = self._open_audio(state)
        try:
            chosen = byte_range(request.header("Range"), size)
        except RouteRefusal:
            stream.close()
            raise
        headers: list[tuple[str, str]] = [
            ("Accept-Ranges", "bytes"),
            ("ETag", f'"{state.audio.sha256}"'),
        ]
        status, length = 200, size
        if chosen is not None:
            start, end = chosen
            stream.seek(start, os.SEEK_SET)
            headers.append(("Content-Range", f"bytes {start}-{end}/{size}"))
            status, length = 206, end - start + 1
        return RouteResponse(
            status=status,
            route="corpus.audio",
            content_type="audio/wav",
            headers=tuple(headers),
            stream=stream,
            stream_length=length,
        )

    def _open_audio(self, state: CorpusTakeState) -> tuple[BinaryIO, int]:
        """The take's raw WAV, open, once it is a regular file of the recorded size.

        Raw packages are content-addressed and write-protected, so the WAV is not hashed
        again for each request: its size and file type are checked against the record."""
        try:
            path = confined_path(self.layout.root, f"{state.raw_path}/{AUDIO_NAME}")
            stream = open_regular_no_follow(path)
        except (SecurityError, OSError, ValueError) as error:
            raise RouteRefusal(500, "take_invalid", f"the take's raw audio: {error}") from error
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != state.audio.size_bytes:
            stream.close()
            raise RouteRefusal(
                500, "take_invalid", "the take's raw audio is not the size its record names"
            )
        return stream, metadata.st_size
