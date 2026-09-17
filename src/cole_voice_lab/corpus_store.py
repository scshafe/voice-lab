"""Catalog logic every prompt and catalog store shares, and the file-only snapshot readers.

@canonical corpus_store -- the rules a catalog applies to prompts and catalog definitions
(prompt-suite/v2 fit, text hygiene, the typo-class bound of a keep-recordings edit, "reads
the same"), and the only readers of published snapshot files: every version is pinned by
the canonical digest of the PromptSuite it materializes and backed by an immutable
content-addressed snapshot file that takes reference instead of embedding the catalog.
Related: catalog_ports.py (the reader and editor ports, records and CORPUS.* errors),
corpus_store_sqlite.py (the store), storage_binding.py (opens it),
recording_contracts.py (CatalogReference, CatalogSnapshot, PromptAlias).

A snapshot that a reference names but that is missing or altered (suite or aliases: the
reference pins the sha256 of the whole file) fails as ``CorpusSnapshotSecurityError``, a
``SecurityError`` subclass, so existing fail-closed handling applies unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import unicodedata
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import BinaryIO, Final, get_args

from pydantic import Field, TypeAdapter, ValidationError

from .canonical import canonical_json_bytes, require_private_metadata
from .catalog_ports import (
    CatalogDefinition,
    CorpusSnapshotSecurityError,
    CorpusStoreCode,
    CorpusStoreError,
)
from .errors import SecurityError
from .models import (
    PHYSICAL_TEST_SUITE_PREFIX,
    PROMPT_SUITE_V2,
    ContractModel,
    Prompt,
    PromptSuite,
    PromptSuitePurpose,
    Sha256,
    StableId,
    has_hidden_control,
)
from .paths import AssetLayout, confined_path
from .recording_contracts import (
    MAX_RECORDING_REVISION,
    CatalogReference,
    CatalogSnapshot,
    RecordingGroup,
    UtcTimestamp,
)

MAX_CATALOG_SNAPSHOT_BYTES: Final = 16 * 1024 * 1024
MAX_CATALOG_DESCRIPTION_CHARS: Final = 1024
# Version and revision numbers are SQLite integers.
MAX_CATALOG_VERSION: Final = MAX_RECORDING_REVISION
# A typo-class text fix (the only rewording an operator's --keep-recordings accepts): at most
# this many word and character edits once case, punctuation and spacing are ignored.
MAX_KEPT_WORD_EDITS: Final = 2
MAX_KEPT_CHARACTER_EDITS: Final = 3
# Resolved group catalogs kept per owner (take store, index); a process binds a handful.
MAX_CACHED_GROUP_CATALOGS: Final = 8
# The refusal when no store exists (CORPUS.STORE_MISSING); it names the SQLite database
# (corpus_store_sqlite.CORPUS_DIRECTORY and CORPUS_DATABASE_NAME).
STORE_MISSING_MESSAGE: Final = (
    "no corpus store at provenance/corpus/corpus.sqlite3; "
    "run `voice-lab catalog init` to create one"
)
_NOT_SPOKEN = re.compile(r"[^\w\s]|_")
# Descriptions are printed to terminals: no control, format or line/paragraph separators.
_DESCRIPTION_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})
_PURPOSES: Final[tuple[PromptSuitePurpose, ...]] = get_args(PromptSuitePurpose)
_PROMPT_FIT_SUITE_ID = "prompt-fit"
_STABLE_ID: TypeAdapter[str] = TypeAdapter(StableId)
_PURPOSE: TypeAdapter[PromptSuitePurpose] = TypeAdapter(PromptSuitePurpose)


class PromptSourceReference(ContractModel):
    """Provenance of an imported sentence set, stored as canonical JSON beside its prompts.

    ``sha256`` is the digest of the exact source bytes. ``unpinned_source_accepted`` is true
    only when those bytes are not the published list the importer pins
    (``standard_prompt_sets.STANDARD_SET_SHA256``) and the operator imported them anyway
    (``--accept-unpinned-source``); it is omitted from the JSON when false, so references
    written before the field existed keep their exact bytes.
    """

    url: str = Field(min_length=1, max_length=2048)
    fetched_at: UtcTimestamp
    sha256: Sha256
    license: str = Field(min_length=1, max_length=65_536)
    unpinned_source_accepted: bool = Field(default=False, exclude_if=lambda value: not value)


@dataclass(frozen=True, slots=True)
class TextChange:
    """How far an edit moves a prompt's spoken text, measured on the words read aloud.

    Both counts ignore case, punctuation and spacing. ``word_edits`` is ``None`` when the
    word sequences differ by more than ``MAX_KEPT_WORD_EDITS`` insertions, deletions or
    substitutions; ``character_edits`` is ``None`` beyond ``MAX_KEPT_CHARACTER_EDITS``.
    """

    before: str
    after: str
    word_edits: int | None
    character_edits: int | None

    @property
    def typo_class(self) -> bool:
        """True when the recorded audio can still read as the new text (a typo fix)."""
        return self.word_edits is not None and self.character_edits is not None

    @property
    def spoken_words_unchanged(self) -> bool:
        """True when only case, punctuation or spacing changed."""
        return self.word_edits == 0


@dataclass(frozen=True, slots=True)
class PromptCatalogFit:
    """Which prompt-suite/v2 catalog purposes can hold one prompt.

    ``suites`` holds, per purpose that accepts the prompt, a one-prompt suite of that
    purpose (a physical-test one carries the physical-test suite id prefix); ``problems``
    says, per other purpose, why its catalogs refuse the prompt.
    """

    prompt_id: str
    suites: Mapping[str, PromptSuite]  # keyed by PromptSuitePurpose
    problems: Mapping[str, str]  # keyed by PromptSuitePurpose

    def holds(self, purpose: str) -> bool:
        """True when a catalog of ``purpose`` can publish the prompt."""
        return purpose in self.suites

    def explanation(self) -> str:
        """Every refusing purpose with its reason, purposes sharing a reason grouped."""
        grouped: dict[str, list[str]] = {}
        for purpose, problem in self.problems.items():
            grouped.setdefault(problem, []).append(purpose)
        return "; ".join(
            f"{', '.join(purposes)}: {problem}" for problem, purposes in grouped.items()
        )


def validation_problems(error: ValidationError) -> str:
    """A pydantic failure as one line: ``location: message`` parts joined by ``; ``.

    Pydantic's ``Value error, `` prefix for a validator's own ``ValueError`` is dropped, so
    the text reads as the contract's sentence.
    Params: ``error`` is any pydantic ``ValidationError``.
    Returns: the one-line text.
    Raises: nothing.
    """
    parts = []
    for item in error.errors():
        location = ".".join(str(part) for part in item["loc"])
        message = item["msg"].removeprefix("Value error, ")
        parts.append(f"{location}: {message}" if location else message)
    return "; ".join(parts)


def spoken_words(text: str) -> tuple[str, ...]:
    """The words a reader says for ``text``: case, punctuation and spacing are ignored.

    The one normalization behind every "reads the same" rule: the keep-recordings bound
    (``text_change``), the store's refusal of a prompt that reads like another live prompt,
    and the standard-set parsers' refusal of two sentences that read alike.
    Params: ``text`` is spoken prompt text.
    Returns: the case-folded words with every character that is neither a letter nor a
    digit removed (so ``"It's"`` is ``"its"``), in order.
    Raises: nothing.
    """
    return tuple(_NOT_SPOKEN.sub("", text.casefold()).split())


def _edits_within(first: Sequence[str], second: Sequence[str], limit: int) -> int | None:
    """Levenshtein distance when it is at most ``limit``, else ``None`` (banded, O(limit·n))."""
    if abs(len(first) - len(second)) > limit:
        return None
    beyond = limit + 1
    previous = [column if column <= limit else beyond for column in range(len(second) + 1)]
    for row in range(1, len(first) + 1):
        current = [beyond] * (len(second) + 1)
        if row <= limit:
            current[0] = row
        for column in range(max(1, row - limit), min(len(second), row + limit) + 1):
            substitution = previous[column - 1] + (first[row - 1] != second[column - 1])
            current[column] = min(
                previous[column] + 1, current[column - 1] + 1, substitution, beyond
            )
        previous = current
    return previous[-1] if previous[-1] <= limit else None


def text_change(before: str, after: str) -> TextChange:
    """Measure a spoken-text edit the way the keep-recordings rule judges it.

    A kept recording keeps its audio and trains with the new text, so the new text must
    still be what the audio says: case, punctuation and spacing are free, and beyond that
    at most ``MAX_KEPT_WORD_EDITS`` word edits and ``MAX_KEPT_CHARACTER_EDITS`` character
    edits (``TextChange.typo_class``). ``CatalogEditor.edit_prompt`` refuses a keep-recordings
    edit beyond that (``CORPUS.REWORDING_REFUSED``) unless its caller passes
    ``allow_rewording`` (``voice-lab catalog edit-prompt --allow-rewording``).
    Params: ``before`` and ``after`` are the current and the new spoken text.
    Returns: the measured change.
    Raises: nothing.
    """
    words_before, words_after = spoken_words(before), spoken_words(after)
    return TextChange(
        before=before,
        after=after,
        word_edits=_edits_within(words_before, words_after, MAX_KEPT_WORD_EDITS),
        character_edits=_edits_within(
            " ".join(words_before), " ".join(words_after), MAX_KEPT_CHARACTER_EDITS
        ),
    )


def _quoted(text: str | None) -> str:
    return "(no spoken text)" if text is None else json.dumps(text, ensure_ascii=False)


def _require_typo_class(prompt_id: str, before: str | None, after: str | None) -> None:
    """A kept recording pairs old audio with the new text, so the words must still match."""
    if before is None or after is None or before == after:
        return  # silent prompts carry no transcript; the identity rule refuses spoken/silent
    if text_change(before, after).typo_class:
        return
    raise CorpusStoreError(
        "CORPUS.REWORDING_REFUSED",
        f"--keep-recordings keeps {prompt_id}'s recordings only for a typo-class fix (at most "
        f"{MAX_KEPT_WORD_EDITS} word edits and {MAX_KEPT_CHARACTER_EDITS} character edits, "
        f"ignoring case, punctuation and spacing), and {_quoted(before)} -> "
        f"{_quoted(after)} is a rewording: the kept audio would train with words it does "
        "not say. Edit without --keep-recordings (the prompt is recorded again), or add "
        "--allow-rewording if the recordings already say the new text; nothing was written",
    )


def prompt_catalog_fit(prompt: Prompt, purposes: Sequence[str] | None = None) -> PromptCatalogFit:
    """Check one prompt against the prompt-suite/v2 rules of every catalog purpose.

    Each purpose's rules are applied to the prompt alone (a one-prompt suite), which is
    exactly the per-prompt part of ``PromptSuite`` validation: the held-out flag must match
    the purpose, only physical-test catalogs take procedures and the ``test`` label (and
    require it), a room tone carries instructions and no text, and text is hygienic.
    Params: ``prompt`` is a validated ``Prompt``; ``purposes`` limits the check to these
    purposes (every purpose when ``None``; unknown names are ignored).
    Returns: the checked purposes that hold it (with their one-prompt suites) and why the
    other checked purposes do not.
    Raises: nothing.
    """
    suites: dict[str, PromptSuite] = {}
    problems: dict[str, str] = {}
    for purpose in _PURPOSES if purposes is None else (p for p in _PURPOSES if p in purposes):
        prefix = PHYSICAL_TEST_SUITE_PREFIX if purpose == "physical-test" else ""
        try:
            suites[purpose] = PromptSuite(
                schema_version=PROMPT_SUITE_V2,
                suite_id=prefix + _PROMPT_FIT_SUITE_ID,
                purpose=purpose,
                prompts=(prompt,),
            )
        except ValidationError as error:
            problems[purpose] = "; ".join(
                dict.fromkeys(item["msg"].removeprefix("Value error, ") for item in error.errors())
            )
    return PromptCatalogFit(prompt_id=prompt.prompt_id, suites=suites, problems=problems)


def catalog_definition(
    catalog_id: str, *, suite_id: str | None = None, purpose: str, description: str = ""
) -> CatalogDefinition:
    """Validate a catalog's identity before anything is stored.

    Params: ``catalog_id``; ``suite_id`` becomes ``PromptSuite.suite_id`` of every version
    (``catalog_id`` when ``None``); ``purpose`` is the suite purpose; ``description`` is free
    text of at most ``MAX_CATALOG_DESCRIPTION_CHARS`` characters on one line.
    Returns: the validated definition.
    Raises: ``CorpusStoreError`` ``CORPUS.CATALOG_INVALID`` for an id or suite id that is
    not a stable id, an unknown purpose, a suite id whose physical-test prefix disagrees
    with the purpose (no version could ever publish), or a description that is too long or
    carries a control, format or line-separator character (it is printed to terminals).
    """
    identifier = _validated(_STABLE_ID, catalog_id, "CORPUS.CATALOG_INVALID", "catalog id")
    suite = (
        identifier
        if suite_id is None
        else _validated(_STABLE_ID, suite_id, "CORPUS.CATALOG_INVALID", "suite id")
    )
    catalog_purpose = _validated(_PURPOSE, purpose, "CORPUS.CATALOG_INVALID", "purpose")
    if (catalog_purpose == "physical-test") != suite.startswith(PHYSICAL_TEST_SUITE_PREFIX):
        raise CorpusStoreError(
            "CORPUS.CATALOG_INVALID",
            f"suite id {suite} cannot name a {catalog_purpose} catalog: exactly the "
            f"physical-test catalogs have suite ids starting with {PHYSICAL_TEST_SUITE_PREFIX}",
        )
    if len(description) > MAX_CATALOG_DESCRIPTION_CHARS:
        raise CorpusStoreError(
            "CORPUS.CATALOG_INVALID",
            f"description exceeds {MAX_CATALOG_DESCRIPTION_CHARS} characters",
        )
    if any(
        unicodedata.category(character) in _DESCRIPTION_FORBIDDEN_CATEGORIES
        for character in description
    ):
        raise CorpusStoreError(
            "CORPUS.CATALOG_INVALID",
            "description cannot contain control, format or line-separator characters",
        )
    return CatalogDefinition(identifier, suite, catalog_purpose, description)


def validated_subject_id(subject_id: str) -> str:
    """Validate a subject id before a catalog is activated for it.

    Params: ``subject_id``.
    Returns: the same id.
    Raises: ``CorpusStoreError`` ``CORPUS.SUBJECT_INVALID`` unless it is a stable id.
    """
    return _validated(_STABLE_ID, subject_id, "CORPUS.SUBJECT_INVALID", "subject id")


def _validated[ValueT](
    adapter: TypeAdapter[ValueT], value: object, code: CorpusStoreCode, label: str
) -> ValueT:
    try:
        return adapter.validate_python(value)
    except ValidationError as error:
        raise CorpusStoreError(code, f"{label}: {validation_problems(error)}") from error


def _storable_prompt(prompt: Prompt) -> Prompt:
    """Apply the prompt-suite/v2 rules now, so every stored prompt can publish somewhere.

    The store publishes prompt-suite/v2 only, so a prompt no v2 catalog of any purpose can
    hold (a room tone that carries text, a procedure without the ``test`` label, ...) would
    stay live forever without ever publishing.
    """
    if any(
        value is not None and has_hidden_control(value)
        for value in (prompt.text, prompt.instructions, prompt.style)
    ):
        raise CorpusStoreError(
            "CORPUS.PROMPT_INVALID",
            f"prompt {prompt.prompt_id} text cannot contain format or control characters",
        )
    fit = prompt_catalog_fit(prompt)
    if not fit.suites:
        raise CorpusStoreError(
            "CORPUS.PROMPT_INVALID",
            f"prompt {prompt.prompt_id} fits no prompt-suite/v2 catalog of any purpose "
            f"({fit.explanation()})",
        )
    return prompt


def _validated_prompt(payload: dict[str, object]) -> Prompt:
    try:
        prompt = Prompt.model_validate(payload)
    except ValidationError as error:
        raise CorpusStoreError(
            "CORPUS.PROMPT_INVALID", f"prompt is invalid: {validation_problems(error)}"
        ) from error
    return _storable_prompt(prompt)


def prompt_from_fields(
    prompt_id: str,
    *,
    text: str | None,
    category: str,
    style: str,
    expected_seconds: float,
    instructions: str | None = None,
    labels: Sequence[str] = (),
    reference_candidate: bool = False,
    holdout: bool = False,
) -> Prompt:
    """The prompt ``CatalogEditor.add_prompt`` would store, checked the same way, unstored.

    Params: as ``CatalogEditor.add_prompt``.
    Returns: the validated ``Prompt``.
    Raises: ``CorpusStoreError`` ``CORPUS.PROMPT_INVALID`` when the fields do not form a
    valid ``Prompt``, carry a format or control character, or fit no prompt-suite/v2
    catalog of any purpose.
    """
    return _validated_prompt(
        {
            "prompt_id": prompt_id,
            "text": text,
            "category": category,
            "style": style,
            "instructions": instructions,
            "labels": tuple(labels),
            "expected_seconds": expected_seconds,
            "reference_candidate": reference_candidate,
            "holdout": holdout,
        }
    )


def _snapshot_payload(stream: BinaryIO, label: str) -> bytes:
    """Read one already opened snapshot after its private-file and size checks."""
    metadata = os.fstat(stream.fileno())
    try:
        require_private_metadata(metadata, label)
    except SecurityError as error:
        raise CorpusSnapshotSecurityError("CORPUS.SNAPSHOT_MISMATCH", str(error)) from error
    if metadata.st_size > MAX_CATALOG_SNAPSHOT_BYTES:
        raise CorpusSnapshotSecurityError(
            "CORPUS.SNAPSHOT_MISMATCH", f"catalog snapshot exceeds its byte limit: {label}"
        )
    return stream.read(MAX_CATALOG_SNAPSHOT_BYTES + 1)


def _decode_snapshot(payload: bytes, reference: CatalogReference) -> CatalogSnapshot:
    """Validate snapshot bytes against the contract and the reference that named them.

    The whole-file digest comes first: it is what binds the alias half of the file, which
    ``catalog_sha256`` (the suite digest) does not cover.
    """
    if hashlib.sha256(payload).hexdigest() != reference.snapshot_sha256:
        raise CorpusSnapshotSecurityError(
            "CORPUS.SNAPSHOT_MISMATCH",
            f"catalog snapshot {reference.snapshot_path} does not have the published bytes",
        )
    try:
        snapshot = CatalogSnapshot.model_validate_json(payload)
    except ValidationError as error:
        raise CorpusSnapshotSecurityError(
            "CORPUS.SNAPSHOT_MISMATCH",
            f"catalog snapshot {reference.snapshot_path} is not a valid snapshot: "
            f"{validation_problems(error)}",
        ) from error
    if payload != canonical_json_bytes(snapshot) + b"\n":
        raise CorpusSnapshotSecurityError(
            "CORPUS.SNAPSHOT_MISMATCH",
            f"catalog snapshot {reference.snapshot_path} is not canonical",
        )
    if snapshot.catalog_sha256 != reference.catalog_sha256:
        raise CorpusSnapshotSecurityError(
            "CORPUS.SNAPSHOT_MISMATCH",
            f"catalog snapshot {reference.snapshot_path} carries digest "
            f"{snapshot.catalog_sha256}, not the referenced one",
        )
    if snapshot.suite.suite_id != reference.suite_id or snapshot.suite.purpose != reference.purpose:
        raise CorpusSnapshotSecurityError(
            "CORPUS.SNAPSHOT_MISMATCH",
            f"catalog snapshot {reference.snapshot_path} declares suite "
            f"{snapshot.suite.suite_id}/{snapshot.suite.purpose}, not "
            f"{reference.suite_id}/{reference.purpose}",
        )
    return snapshot


def read_catalog_snapshot(layout: AssetLayout, reference: CatalogReference) -> CatalogSnapshot:
    """Read and verify the snapshot a reference names, without opening the database.

    Params: ``layout`` is the asset root; ``reference`` names the snapshot by its
    content-addressed path.
    Returns: the validated ``CatalogSnapshot`` whose file bytes, digest, suite id and
    purpose equal the reference's.
    Raises: ``CorpusSnapshotSecurityError`` with ``CORPUS.SNAPSHOT_MISSING`` when no file
    exists at the path, ``CORPUS.SNAPSHOT_MISMATCH`` when the path has a symlink component
    (the file itself included) or cannot be examined, the entry is not a regular file
    (checked by ``lstat`` before any open, so a planted FIFO cannot block the reader), the
    file cannot be opened or is not an owned private regular file with one link, exceeds
    ``MAX_CATALOG_SNAPSHOT_BYTES``, has bytes other than the ``snapshot_sha256`` the
    reference pins, is not a canonical ``CatalogSnapshot``, or disagrees with the reference.
    """
    try:
        # confined_path lstat-walks the components itself, so it can raise OSError too.
        path = confined_path(layout.root, reference.snapshot_path)
        metadata = os.lstat(path)
    except SecurityError as error:
        raise CorpusSnapshotSecurityError("CORPUS.SNAPSHOT_MISMATCH", str(error)) from error
    except FileNotFoundError as error:
        raise CorpusSnapshotSecurityError(
            "CORPUS.SNAPSHOT_MISSING", f"catalog snapshot is missing: {reference.snapshot_path}"
        ) from error
    except (OSError, ValueError) as error:
        # ValueError: a reference built without validation can carry an absolute or
        # escaping snapshot_path, which confined_path refuses before touching the disk.
        raise CorpusSnapshotSecurityError(
            "CORPUS.SNAPSHOT_MISMATCH",
            f"catalog snapshot cannot be examined: {reference.snapshot_path}: {error}",
        ) from error
    if not stat.S_ISREG(metadata.st_mode):
        raise CorpusSnapshotSecurityError(
            "CORPUS.SNAPSHOT_MISMATCH",
            f"catalog snapshot is not a regular file: {reference.snapshot_path}",
        )
    try:
        # O_NONBLOCK: a FIFO swapped in after the lstat above cannot block the open;
        # _snapshot_payload's fstat then refuses anything that is not a private regular file.
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError as error:
        raise CorpusSnapshotSecurityError(
            "CORPUS.SNAPSHOT_MISSING", f"catalog snapshot is missing: {reference.snapshot_path}"
        ) from error
    except OSError as error:
        raise CorpusSnapshotSecurityError(
            "CORPUS.SNAPSHOT_MISMATCH",
            f"catalog snapshot cannot be opened: {reference.snapshot_path}: {error}",
        ) from error
    with os.fdopen(descriptor, "rb") as stream:
        payload = _snapshot_payload(stream, reference.snapshot_path)
    return _decode_snapshot(payload, reference)


def require_referenced_snapshot(snapshot: CatalogSnapshot, reference: CatalogReference) -> None:
    """Check that an in-memory snapshot is exactly the file a reference seals.

    For readers handed a (reference, snapshot) pair, e.g. from ``CatalogReader.materialize``,
    that record the reference next to results derived from the snapshot. The snapshot's
    file bytes (canonical JSON plus a newline, as ``publish_version`` writes them) are
    checked exactly as ``read_catalog_snapshot`` checks a file, so the prompt aliases are
    bound too. A plain suite's ``CatalogSnapshot.embedded`` matches only a published version
    that accepts no aliases.
    Params: ``snapshot`` is the catalog the caller selects through; ``reference`` is the
    published version the caller will name.
    Returns: nothing.
    Raises: ``CorpusSnapshotSecurityError`` (a ``SecurityError``) with
    ``CORPUS.SNAPSHOT_MISMATCH`` when the bytes, suite digest, suite id or purpose disagree.
    """
    _decode_snapshot(canonical_json_bytes(snapshot) + b"\n", reference)


def resolve_group_catalog(layout: AssetLayout, group: RecordingGroup) -> CatalogSnapshot:
    """The exact catalog a recording group binds, as a snapshot; the one group-catalog reader.

    Pure file access, never the database, so the read-only discovery worker and the
    coverage report can use it while another process holds the store.
    Params: ``layout`` is the asset root the group belongs to; ``group`` is a validated
    ``RecordingGroup`` of either version.
    Returns: for recording-group/v1, ``CatalogSnapshot.embedded`` of the embedded suite (no
    aliases); for recording-group/v2, the snapshot file its ``catalog_reference`` names,
    verified by ``read_catalog_snapshot`` (whole-file digest, suite digest, suite id,
    purpose and content-addressed path all equal the sealed reference).
    Raises: ``CorpusSnapshotSecurityError`` (a ``SecurityError``) for a v2 group:
    ``CORPUS.SNAPSHOT_MISSING`` when the referenced snapshot file does not exist,
    ``CORPUS.SNAPSHOT_MISMATCH`` when it is altered, unsafe or disagrees with the
    reference. A v2 group never falls back to anything else.
    """
    if group.catalog is not None:
        return CatalogSnapshot.embedded(group.catalog)
    # RecordingGroup validation guarantees exactly one form and binds the reference's
    # catalog_sha256 to the group's; read_catalog_snapshot binds the file to the reference.
    assert group.catalog_reference is not None
    return read_catalog_snapshot(layout, group.catalog_reference)


type GroupCatalogKey = tuple[str, CatalogReference | None]


class GroupCatalogCache:
    """A small per-owner LRU in front of ``resolve_group_catalog``.

    Entries are keyed by the suite digest plus, for a recording-group/v2, the whole sealed
    ``CatalogReference`` -- every field the file check compares (snapshot digest, suite
    digest, suite id, purpose, path) -- so a cached entry is exactly what a fresh verified
    read of that reference returns, a reference that disagrees with a snapshot another group
    already verified is still read (and refused), and an embedded catalog never shares an
    entry with a snapshot of the same suite. Each reference is therefore verified once per
    owner instance; a file altered after that is detected by the next owner (a restarted
    service, a discovery worker, a new index). Thread-safe; the file is read outside the
    cache lock.
    """

    def __init__(self, layout: AssetLayout, *, capacity: int = MAX_CACHED_GROUP_CATALOGS) -> None:
        """Params: ``layout`` is the asset root every resolved group belongs to;
        ``capacity`` is the most snapshots kept (least recently used leave first).
        Raises: ``ValueError`` for a capacity below one.
        """
        if capacity < 1:
            raise ValueError("a group catalog cache needs room for at least one catalog")
        self.layout = layout
        self.capacity = capacity
        self._lock = threading.Lock()
        self._entries: OrderedDict[GroupCatalogKey, CatalogSnapshot] = OrderedDict()

    @staticmethod
    def key(group: RecordingGroup) -> GroupCatalogKey:
        """The identity two groups share exactly when one verified read serves both.

        Readers that resolve each distinct group catalog once (reconcile, coverage) dedupe
        on this key too, so it binds everything ``read_catalog_snapshot`` checks.
        Params: ``group`` is a validated group of either version.
        Returns: ``(catalog_sha256, None)`` for recording-group/v1 (the digest binds the
        embedded suite) or ``(catalog_sha256, catalog_reference)`` for recording-group/v2
        (the sealed reference, whose snapshot digest also binds the prompt aliases and whose
        suite id and purpose the file must repeat).
        Raises: nothing.
        """
        return group.catalog_sha256, group.catalog_reference

    def resolve(self, group: RecordingGroup) -> CatalogSnapshot:
        """The exact catalog ``group`` binds, from the cache or ``resolve_group_catalog``.

        Params: ``group`` is a validated group of either version.
        Returns: the embedded suite without aliases (v1) or the verified snapshot (v2).
        Raises: ``CorpusSnapshotSecurityError`` (a ``SecurityError``) with
        ``CORPUS.SNAPSHOT_MISSING`` or ``CORPUS.SNAPSHOT_MISMATCH`` when a v2 group's snapshot
        file is missing, altered or disagrees with its reference; nothing is cached then.
        """
        return self._resolve(self.key(group), lambda: resolve_group_catalog(self.layout, group))

    def resolve_reference(self, reference: CatalogReference) -> CatalogSnapshot:
        """The snapshot a recording-group/v2 carrying ``reference`` binds, before one exists.

        Shares the entry such a group resolves through (``key`` is
        ``(reference.catalog_sha256, reference)``), so minting the group afterwards reads the
        file no second time.
        Params: ``reference`` is a validated catalog reference (e.g. an open-context's).
        Returns: the snapshot ``read_catalog_snapshot`` verifies against the reference.
        Raises: ``CorpusSnapshotSecurityError`` (a ``SecurityError``) with
        ``CORPUS.SNAPSHOT_MISSING`` or ``CORPUS.SNAPSHOT_MISMATCH`` as
        ``read_catalog_snapshot``; nothing is cached then.
        """
        return self._resolve(
            (reference.catalog_sha256, reference),
            lambda: read_catalog_snapshot(self.layout, reference),
        )

    def _resolve(
        self, key: GroupCatalogKey, read: Callable[[], CatalogSnapshot]
    ) -> CatalogSnapshot:
        with self._lock:
            cached = self._entries.get(key)
            if cached is not None:
                self._entries.move_to_end(key)
                return cached
        # A concurrent duplicate read resolves the same verified bytes.
        snapshot = read()
        with self._lock:
            self._entries[key] = snapshot
            self._entries.move_to_end(key)
            while len(self._entries) > self.capacity:
                self._entries.popitem(last=False)
        return snapshot
