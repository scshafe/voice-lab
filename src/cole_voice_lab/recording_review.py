"""Selected-source review authorization; no historical-library audio scan.

@canonical recording_review -- index aliases locate candidates; canonical take or
legacy metadata authorizes one expected digest, verified through a private
nofollow source before native playback independently opens its exact descriptor.
"""

from __future__ import annotations

import os
import stat
import threading
import wave
from collections import OrderedDict
from pathlib import Path

from .canonical import canonical_sha256, open_regular_no_follow, sha256_file
from .capture import ReadyMarker, RejectedTakeLog, expected_recording_paths
from .errors import ContractError, SecurityError
from .models import CaptureManifest
from .paths import AssetLayout, confined_path
from .recording_contracts import (
    RecordingFileIdentity,
    RecordingFrameRange,
    RecordingHistoryEntry,
    RecordingReviewSource,
    RecordingTake,
)
from .recording_edits import (
    current_native_edit,
    legacy_edit_intent,
    native_edit_identity,
    read_recording_edit,
    require_current_legacy_edit,
)
from .recording_index import RecordingIndex, _AliasKind
from .recording_library import DesktopSessionEvidence
from .recording_takes import RecordingTakeStore
from .recording_takes import read_recording_evidence as _read

MAX_REVIEW_CACHE_ENTRIES = 8
MAX_SELECTED_METADATA_BYTES = 1_048_576


def _identity(layout: AssetLayout, relative: str) -> RecordingFileIdentity:
    path = confined_path(layout.root, relative, must_exist=True)
    with open_regular_no_follow(path) as stream:
        metadata = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise SecurityError("selected original must remain one private regular file")
        return RecordingFileIdentity(
            relative_path=relative,
            device=metadata.st_dev,
            inode=metadata.st_ino,
            size_bytes=metadata.st_size,
            mode=metadata.st_mode,
            mtime_ns=metadata.st_mtime_ns,
            ctime_ns=metadata.st_ctime_ns,
        )


class RecordingReviewResolver:
    """A bounded metadata-only verification cache; it never stores decoded audio."""

    def __init__(
        self, layout: AssetLayout, store: RecordingTakeStore, index: RecordingIndex
    ) -> None:
        self.layout, self.store, self.index = layout, store, index
        self._verified: OrderedDict[str, None] = OrderedDict()
        self._owned: OrderedDict[str, RecordingFileIdentity] = OrderedDict()
        self._cache_lock = threading.Lock()

    def _verify(
        self, relative: str, digest: str, frames: int, *, cached_only: bool = False
    ) -> RecordingFileIdentity:
        before = _identity(self.layout, relative)
        key = canonical_sha256([before.model_dump(mode="json"), digest, frames])
        with self._cache_lock:
            verified = key in self._verified
        if not verified:
            if cached_only:
                raise ContractError(
                    "review the selected source before editing its changed identity"
                )
            path = confined_path(self.layout.root, relative, must_exist=True)
            with open_regular_no_follow(path) as stream, wave.open(stream, "rb") as reader:
                if (
                    reader.getnchannels(),
                    reader.getsampwidth(),
                    reader.getframerate(),
                    reader.getnframes(),
                    reader.getcomptype(),
                ) != (1, 3, 48_000, frames, "NONE"):
                    raise SecurityError("selected original format or frame count changed")
            actual, size = sha256_file(path)
            if (
                actual != digest
                or size != before.size_bytes
                or _identity(self.layout, relative) != before
            ):
                raise SecurityError("selected original differs from its authorized digest/identity")
        with self._cache_lock:
            self._verified[key] = None
            self._verified.move_to_end(key)
            while len(self._verified) > MAX_REVIEW_CACHE_ENTRIES:
                self._verified.popitem(last=False)
        return before

    def resolve(
        self, catalog_sha256: str, take_id: str, *, require_verified: bool = False
    ) -> RecordingReviewSource:
        if take_id.startswith("take-"):
            take = self.store.get_take(take_id)
            group = self.store.get_cursor(self.index.subject_id)
            if group is None:
                raise ContractError("review requires an open recording context")
            current = self.store.get_group(group.group_id)
            if (
                current.catalog_sha256 != catalog_sha256
                or take.reservation.group.subject_id != self.index.subject_id
                or take.reservation.prompt not in current.catalog.prompts
            ):
                raise SecurityError(
                    "selected native take differs from the current exact catalog/subject"
                )
            return self._native(
                take, catalog_sha256, owned_selection=group.selection.take_id == take_id
            )
        entry, aliases = self.index.review_aliases(catalog_sha256, take_id)
        if (
            entry.legacy_session_id is None
            or entry.source_sha256 is None
            or entry.source_frames is None
        ):
            raise ContractError("selected legacy recording has no complete source identity")
        failures = []
        for relative, digest, kind in aliases:
            try:
                if digest != entry.source_sha256:
                    raise SecurityError("selected alias digest differs from its indexed identity")
                self._authorize_legacy(entry, relative, kind)
                identity = self._verify(
                    relative, digest, entry.source_frames, cached_only=require_verified
                )
                edit = read_recording_edit(
                    self.layout,
                    subject_id=entry.subject_id,
                    session_id=entry.legacy_session_id,
                    prompt_id=entry.prompt.prompt_id,
                    source_sha256=digest,
                )
                frame_range = RecordingFrameRange(
                    source_frames=entry.source_frames,
                    start_frame=0 if edit is None else edit.start_frame,
                    end_frame=entry.source_frames if edit is None else edit.end_frame,
                )
                intent = legacy_edit_intent(
                    self.layout,
                    subject_id=entry.subject_id,
                    session_id=entry.legacy_session_id,
                    prompt_id=entry.prompt.prompt_id,
                    source_sha256=digest,
                )
                if intent is not None:
                    require_current_legacy_edit(self.layout, intent)
                    if (
                        intent.source.take_id != take_id
                        or intent.source.source_identity != identity
                    ):
                        raise SecurityError("legacy intent source identity changed")
                    return RecordingReviewSource.model_validate(
                        intent.source.model_dump() | {"catalog_sha256": catalog_sha256}
                    )
                return RecordingReviewSource(
                    take_id=take_id,
                    subject_id=entry.subject_id,
                    catalog_sha256=catalog_sha256,
                    prompt=entry.prompt,
                    source_path=relative,
                    source_sha256=digest,
                    source_frames=entry.source_frames,
                    source_identity=identity,
                    frame_range=frame_range,
                    decision="removed" if edit is not None and edit.removed else entry.decision,
                    decision_revision=entry.state_revision,
                    edit_revision=0,
                    analysis=entry.analysis,
                    export=entry.export,
                    recorded_at=entry.recorded_at,
                    date_precision=entry.date_precision,
                    original_kind="accepted-raw" if kind == "legacy-raw" else "legacy-staged",
                    edit_recipe_sha256=None if edit is None else canonical_sha256(edit),
                    eligibility_messages=("Removed from use",)
                    if edit is not None and edit.removed
                    else (),
                )
            except (ContractError, SecurityError, OSError, wave.Error) as error:
                failures.append(type(error).__name__)
        raise SecurityError("selected recording has no usable canonical source alias")

    def _native(
        self, take: RecordingTake, catalog_sha256: str, *, owned_selection: bool = False
    ) -> RecordingReviewSource:
        if take.seal is None or take.frame_range is None:
            raise ContractError("review requires a stopped sealed take")
        seal = take.seal
        identity = (
            _identity(self.layout, seal.original_path)
            if owned_selection
            else self._verify(seal.original_path, seal.original_sha256, seal.audio.frames)
        )
        if identity.size_bytes != seal.original_size_bytes:
            raise SecurityError("registered original size differs from its bound seal")
        if owned_selection:
            with self._cache_lock:
                previous = self._owned.get(take.reservation.take_id)
                if previous is not None and previous != identity:
                    raise SecurityError(
                        "registered original identity changed since selected authorization"
                    )
                self._owned[take.reservation.take_id] = identity
                self._owned.move_to_end(take.reservation.take_id)
                while len(self._owned) > MAX_REVIEW_CACHE_ENTRIES:
                    self._owned.popitem(last=False)
        recipe = current_native_edit(self.layout, take.reservation.take_id)
        messages = []
        if take.decision != "kept":
            messages.append(
                "Draft is not saved" if take.decision == "draft" else "Excluded from use"
            )
        if take.analysis != "verified":
            messages.append(
                "Independent verification is not complete"
                if take.analysis in {"pending", "checking"}
                else "Verification requires review"
            )
        return RecordingReviewSource(
            take_id=take.reservation.take_id,
            subject_id=take.reservation.group.subject_id,
            catalog_sha256=catalog_sha256,
            prompt=take.reservation.prompt,
            source_path=seal.original_path,
            source_sha256=seal.original_sha256,
            source_frames=seal.audio.frames,
            source_identity=identity,
            frame_range=take.frame_range,
            decision=take.decision,
            decision_revision=take.decision_revision,
            edit_revision=take.edit_revision,
            analysis=take.analysis,
            export=take.export,
            recorded_at=seal.first_sample_at,
            date_precision="take",
            original_kind="native-original",
            edit_recipe_sha256=None
            if recipe is None
            else native_edit_identity(self.layout, take.reservation.take_id, take=take),
            eligibility_messages=tuple(messages),
        )

    def _authorize_legacy(
        self, entry: RecordingHistoryEntry, relative: str, kind: _AliasKind
    ) -> None:
        assert entry.legacy_session_id is not None
        source = confined_path(self.layout.root, relative, must_exist=True)
        parts = Path(relative).parts
        if kind == "legacy-raw":
            if len(parts) < 3 or parts[0] != "raw":
                raise SecurityError("accepted alias is outside its content-addressed root")
            package = confined_path(self.layout.root, "/".join(parts[:2]), must_exist=True)
        elif kind in {"legacy-tmp", "rejected"}:
            expected = f"capture-{entry.legacy_session_id}" + (
                ".rejected" if kind == "rejected" else ""
            )
            if len(parts) < 3 or parts[:2] != ("tmp", expected):
                raise SecurityError("legacy alias differs from its canonical capture session")
            package = confined_path(self.layout.root, "/".join(parts[:2]), must_exist=True)
        else:
            raise ContractError("native package alias requires its canonical package-to-take map")
        if kind == "rejected":
            ledger = _read(package / "rejected-takes.json", RejectedTakeLog)
            if ledger.session_id != entry.legacy_session_id or not any(
                item.relative == source.name
                and item.sha256 == entry.source_sha256
                and item.size_bytes == source.stat().st_size
                for item in ledger.entries
            ):
                raise SecurityError("rejected source differs from its forensic ledger")
        elif (package / "READY").exists():
            marker = _read(package / "READY", ReadyMarker)
            manifest_path = confined_path(package, marker.manifest_path, must_exist=True)
            manifest = _read(manifest_path, CaptureManifest)
            if (
                sha256_file(manifest_path)[0] != marker.manifest_sha256
                or marker.session_id != entry.legacy_session_id
                or manifest.session_id != entry.legacy_session_id
                or manifest.subject_id != entry.subject_id
                or kind == "legacy-raw"
                and package.name != marker.manifest_sha256
            ):
                raise SecurityError("selected package manifest differs from its canonical identity")
            evidence = next(
                (
                    item
                    for item in manifest.files
                    if item.path == source.relative_to(package).as_posix()
                ),
                None,
            )
            if (
                evidence is None
                or evidence.sha256 != entry.source_sha256
                or evidence.audio is None
                or evidence.audio.frames != entry.source_frames
                or evidence.size_bytes != source.stat().st_size
                or evidence.kind == "utterance"
                and (
                    evidence.prompt_id != entry.prompt.prompt_id
                    or evidence.transcript != entry.prompt.text
                )
                or evidence.kind == "room-tone"
                and entry.prompt.category != "room-tone"
            ):
                raise SecurityError("selected source is not authorized by its capture manifest")
        else:
            if kind == "legacy-raw":
                raise SecurityError("accepted source has lost its READY evidence")
        # Desktop files and rejected forensic files need precise local provenance;
        # permanent foreign v1 packages retain their explicit session precision.
        sidecar_path = confined_path(
            self.layout.root, f"provenance/capture-desktop/{entry.legacy_session_id}.json"
        )
        if sidecar_path.exists():
            sidecar = _read(sidecar_path, DesktopSessionEvidence)
            if (
                sidecar.subject_id != entry.subject_id
                or entry.prompt not in sidecar.selected_suite.prompts
            ):
                raise SecurityError(
                    "selected desktop source differs from its exact prompt evidence"
                )
            selected = [
                take
                for take in sidecar.takes
                if take.prompt_id == entry.prompt.prompt_id
                and take.audio_sha256 == entry.source_sha256
                and take.take_started_at == entry.recorded_at
            ]
            if not selected:
                raise SecurityError("selected desktop take date/digest lacks provenance")
            if (
                kind != "rejected"
                and source.relative_to(package).as_posix()
                != dict(
                    zip(
                        (prompt.prompt_id for prompt in sidecar.selected_suite.prompts),
                        expected_recording_paths(sidecar.selected_suite),
                        strict=True,
                    )
                )[entry.prompt.prompt_id]
            ):
                raise SecurityError("selected desktop path differs from its canonical prompt path")
        elif entry.legacy_session_id.startswith("desktop-") or not (package / "READY").exists():
            raise SecurityError("selected desktop provenance disappeared")
