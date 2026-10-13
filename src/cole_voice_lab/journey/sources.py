"""Where the journey's recordings come from: the recording index's source port (D-051).

A recording is a recording, whatever made it (Recording v1, step 7). The recording index
(``journey/index.py``) and the watcher's scan (``jobs/watch.py``) list recordings through
configured *sources*: adapters behind :class:`RecordingSource`, each with a stable
``type`` name. Every entry a source lists carries ``source`` with that ``type``, so every
index row says where it came from, and ``/api/v1/recordings`` serves it as ``sourceType``.

Two adapters, in this order (the index's order before the port):

- :class:`VoiceMemosSource`, ``voice-memos``: the phone drop, the mirror of Voice Memos.
  It is ``corpus.list_corpus_entries`` with the drop's mirror manifest, unchanged, and
  each entry gains ``source: {"type": "voice-memos"}``. Nothing else is known per memo:
  Voice Memos syncs recordings made on the phone and on Macs, so the drop cannot name a
  chain, and memos have no sessions or segments.
- :class:`MeasurementSource`, ``measurement``: the UMIK-1 measurement sessions,
  ``corpus.list_measurement_entries`` unchanged; its entries already carried
  ``source: {"type": "measurement", "chainId", "sessionId", "segmentKey"}``.

Every adapter keeps the host-access seam's guarantees: it reads directory entries, file
stat metadata and small control files under the roots it was configured with, never audio
bytes and never another path, and it writes nothing. Its entries have the listing's shape
(``recordingId``, ``filename``, ``capturedAt``, ``sizeBytes``, ``modifiedAt``; ``inManifest``
when a mirror manifest vouches for the drop; ``hostPath`` for the index's own use, never
written to a row) and namespace their ``recordingId`` (measurement ids hash
``measurement/<session>/<file>``), so ids never meet across sources.
:func:`list_source_entries` holds the sources to that: each type once, each entry tagged
with its own source's type, no id listed by two sources.

A later source (the singing studio, after v1) is a third adapter added to
:func:`configured_sources`; the index, the watcher and the stages after them do not change.
A stage that treats a kind of recording apart branches on ``source.type`` (the filter and
STT skip ``measurement``); every other type takes the Voice Memos path. Playback is not
part of the port: the browser's seam (``browser.open_recording_audio``) opens only a phone
or a measurement file named by its row, so a new source's rows are listed, filtered and
analysed but not played until that seam learns where its audio lives.

An index written before this port (and the Node pipeline's) gives phone rows no
``source``: Node tagged only measurement rows. :func:`source_type` reads such a row as
``voice-memos``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from . import jscompat as js
from .corpus import Entry, list_corpus_entries, list_measurement_entries, public_entry

VOICE_MEMOS: Final = "voice-memos"
MEASUREMENT: Final = "measurement"
# What a row without `source` is: Node's index tagged measurement rows only.
UNTAGGED_SOURCE_TYPE: Final = VOICE_MEMOS

_SOURCE_TYPE = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")


class SourceError(Exception):
    """A source broke the port's contract (a type twice, a foreign tag, a shared id)."""


class RecordingSource(Protocol):
    """One kind of recording the index lists: a stable ``type`` and a metadata listing."""

    @property
    def type(self) -> str:
        """The ``source.type`` every entry of this source carries (``[a-z][a-z0-9-]*``)."""
        ...

    def list_entries(self) -> list[Entry]:
        """The source's recordings, metadata only, each tagged ``source.type == type``."""
        ...


@dataclass(frozen=True, slots=True)
class VoiceMemosSource:
    """The phone drop (``list_corpus_entries`` with its mirror manifest), tagged."""

    root: str
    limit: int | None = None

    @property
    def type(self) -> str:
        return VOICE_MEMOS

    def list_entries(self) -> list[Entry]:
        listed = list_corpus_entries(self.root, limit=self.limit, mirror_manifest=True)
        # public_entry keeps the listing's key order and puts `source` before `inManifest`.
        return [public_entry({**entry, "source": {"type": self.type}}) for entry in listed]


@dataclass(frozen=True, slots=True)
class MeasurementSource:
    """The accepted measurement sessions (``list_measurement_entries``), as they were."""

    root: str

    @property
    def type(self) -> str:
        return MEASUREMENT

    def list_entries(self) -> list[Entry]:
        return list_measurement_entries(self.root)


def configured_sources(
    corpus_root: str | os.PathLike[str],
    measurement_root: str | os.PathLike[str],
    *,
    limit: int | None = None,
) -> tuple[RecordingSource, ...]:
    """The sources the index and the watcher list, in order. ``limit`` applies to the
    phone drop only, as the index's ``--limit`` always did."""

    return (
        VoiceMemosSource(os.fspath(corpus_root), limit=limit),
        MeasurementSource(os.fspath(measurement_root)),
    )


def list_source_entries(sources: Sequence[RecordingSource]) -> list[Entry]:
    """Every source's entries, source by source, in each source's order."""

    types: set[str] = set()
    listed_by: dict[Any, str] = {}
    entries: list[Entry] = []
    for source in sources:
        kind = source.type
        if not isinstance(kind, str) or not _SOURCE_TYPE.match(kind):
            raise SourceError(f"a source type must match [a-z][a-z0-9-]*: {kind!r}")
        if kind in types:
            raise SourceError(f"two sources have the type {kind}")
        types.add(kind)
        for entry in source.list_entries():
            if js.get(js.get(entry, "source"), "type") != kind:
                raise SourceError(f"an entry of the {kind} source is not tagged {kind}")
            recording = entry.get("recordingId")
            first = listed_by.setdefault(recording, kind)
            if first != kind:
                raise SourceError(f"the {first} and {kind} sources both list {recording}")
            entries.append(entry)
    return entries


def source_type(row: Any) -> Any:
    """A row's ``source.type``; ``voice-memos`` for a row without one (a phone row of an
    index written before the port). A malformed value is returned as it is."""

    return js.coalesce(js.get(js.get(row, "source"), "type"), UNTAGGED_SOURCE_TYPE)


__all__ = [
    "MEASUREMENT",
    "UNTAGGED_SOURCE_TYPE",
    "VOICE_MEMOS",
    "MeasurementSource",
    "RecordingSource",
    "SourceError",
    "VoiceMemosSource",
    "configured_sources",
    "list_source_entries",
    "source_type",
]
