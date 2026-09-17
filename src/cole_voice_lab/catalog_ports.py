"""The prompt catalog as two ports: what readers may ask and what editors may change.

@canonical catalog_ports -- every caller of the prompt and catalog store depends on
CatalogReader or CatalogEditor, the records they return and the CORPUS.* errors they raise;
CORPUS_ERROR_CLASSES names the one error class of every code.
Related: storage_binding.py (opens an implementation), corpus_store_sqlite.py (the SQLite
implementation), corpus_store.py (the catalog logic every implementation shares),
recording_contracts.py (CatalogReference, CatalogSnapshot), docs/CORPUS_STORE.md.

An implementation keeps the store's rules: one transaction per change with every rule
checked inside it, writers serialized so versions are consecutive, snapshots written once
under ``provenance/corpus/snapshots`` and readable without the store, and the same codes and
security classes. Path, ownership and mode invariants fail as ``CorpusStoreSecurityError``;
a snapshot that a reference names but that is missing or altered fails as
``CorpusSnapshotSecurityError``. Both are ``SecurityError`` subclasses so existing
fail-closed handling applies unchanged. This module imports no implementation.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Final, Literal, Protocol, Self

from .errors import ContractError, SecurityError
from .models import Prompt, PromptSuite, PromptSuitePurpose
from .recording_contracts import CatalogReference, CatalogSnapshot

type CorpusStoreCode = Literal[
    "CORPUS.STORE_MISSING",
    "CORPUS.STORE_UNAVAILABLE",
    "CORPUS.STORE_UNSAFE",
    "CORPUS.PROMPT_DUPLICATE",
    "CORPUS.PROMPT_UNKNOWN",
    "CORPUS.PROMPT_RETIRED",
    "CORPUS.PROMPT_INVALID",
    "CORPUS.PROMPT_UNCHANGED",
    "CORPUS.PROMPT_CHANGED",
    "CORPUS.PROMPT_UNPUBLISHABLE",
    "CORPUS.REWORDING_REFUSED",
    "CORPUS.SOURCE_INVALID",
    "CORPUS.SOURCE_UNAVAILABLE",
    "CORPUS.SOURCE_REFERENCE_INVALID",
    "CORPUS.CATALOG_DUPLICATE",
    "CORPUS.CATALOG_UNKNOWN",
    "CORPUS.CATALOG_INVALID",
    "CORPUS.CATALOG_UNPUBLISHABLE",
    "CORPUS.VERSION_UNKNOWN",
    "CORPUS.VERSION_UNCHANGED",
    "CORPUS.SUBJECT_INVALID",
    "CORPUS.NO_ACTIVE_CATALOG",
    "CORPUS.SNAPSHOT_MISSING",
    "CORPUS.SNAPSHOT_MISMATCH",
    "CORPUS.EMPTY_CATALOG",
]
type PromptSource = Literal["pilot-v1", "pilot-v2", "harvard", "arctic", "manual", "import"]


class CorpusStoreError(ContractError):
    """A named corpus store failure; ``code`` is one ``CORPUS.<FAILURE>`` value.

    ``str(error)`` is the message alone. Instances pickle with code and message intact so
    one raised in another process arrives as the same error.
    """

    def __init__(self, code: CorpusStoreCode, message: str) -> None:
        self.code: CorpusStoreCode = code
        super().__init__(message)

    def __reduce__(self) -> tuple[type[CorpusStoreError], tuple[CorpusStoreCode, str]]:
        return (type(self), (self.code, str(self)))


class CorpusStoreSecurityError(SecurityError, CorpusStoreError):
    """A store directory, lock or database file failed an ownership, mode or identity check.

    Always ``CORPUS.STORE_UNSAFE``; nothing is repaired or moved aside.
    """


class CorpusSnapshotSecurityError(SecurityError, CorpusStoreError):
    """A snapshot that a reference names is missing, altered, or disagrees with the reference.

    ``CORPUS.SNAPSHOT_MISSING`` when no file exists, ``CORPUS.SNAPSHOT_MISMATCH`` otherwise.
    """


CORPUS_ERROR_CLASSES: Final[Mapping[CorpusStoreCode, type[CorpusStoreError]]] = MappingProxyType(
    {
        "CORPUS.STORE_MISSING": CorpusStoreError,
        "CORPUS.STORE_UNAVAILABLE": CorpusStoreError,
        "CORPUS.STORE_UNSAFE": CorpusStoreSecurityError,
        "CORPUS.PROMPT_DUPLICATE": CorpusStoreError,
        "CORPUS.PROMPT_UNKNOWN": CorpusStoreError,
        "CORPUS.PROMPT_RETIRED": CorpusStoreError,
        "CORPUS.PROMPT_INVALID": CorpusStoreError,
        "CORPUS.PROMPT_UNCHANGED": CorpusStoreError,
        "CORPUS.PROMPT_CHANGED": CorpusStoreError,
        "CORPUS.PROMPT_UNPUBLISHABLE": CorpusStoreError,
        "CORPUS.REWORDING_REFUSED": CorpusStoreError,
        "CORPUS.SOURCE_INVALID": CorpusStoreError,
        "CORPUS.SOURCE_UNAVAILABLE": CorpusStoreError,
        "CORPUS.SOURCE_REFERENCE_INVALID": CorpusStoreError,
        "CORPUS.CATALOG_DUPLICATE": CorpusStoreError,
        "CORPUS.CATALOG_UNKNOWN": CorpusStoreError,
        "CORPUS.CATALOG_INVALID": CorpusStoreError,
        "CORPUS.CATALOG_UNPUBLISHABLE": CorpusStoreError,
        "CORPUS.VERSION_UNKNOWN": CorpusStoreError,
        "CORPUS.VERSION_UNCHANGED": CorpusStoreError,
        "CORPUS.SUBJECT_INVALID": CorpusStoreError,
        "CORPUS.NO_ACTIVE_CATALOG": CorpusStoreError,
        "CORPUS.SNAPSHOT_MISSING": CorpusSnapshotSecurityError,
        "CORPUS.SNAPSHOT_MISMATCH": CorpusSnapshotSecurityError,
        "CORPUS.EMPTY_CATALOG": CorpusStoreError,
    }
)
"""The class every raise of a code uses: the security codes keep their ``SecurityError``
subclass (the recording service and the fine-tune worker test for it before the code), and
every other code is a plain ``CorpusStoreError``."""


class _Unchanged(enum.Enum):
    token = "unchanged"


UNCHANGED: Final = _Unchanged.token
"""Passed for an ``edit_prompt`` field that keeps its current value."""


@dataclass(frozen=True, slots=True)
class PromptRecord:
    """One prompt revision with its prompt-level provenance and lifecycle."""

    prompt: Prompt
    prompt_sha256: str
    revision: int
    keeps_recordings: bool
    accepted_prompt_sha256s: tuple[str, ...]
    source: PromptSource
    source_reference: str | None
    created_at: datetime
    revision_created_at: datetime
    retired_at: datetime | None


@dataclass(frozen=True, slots=True)
class CatalogRecord:
    """One catalog with every version it has published, ascending."""

    catalog_id: str
    suite_id: str
    purpose: PromptSuitePurpose
    description: str
    created_at: datetime
    retired_at: datetime | None
    versions: tuple[CatalogReference, ...]


@dataclass(frozen=True, slots=True)
class CatalogDefinition:
    """A catalog's identity as ``catalog_definition`` validated it, before it is stored."""

    catalog_id: str
    suite_id: str
    purpose: PromptSuitePurpose
    description: str


class CatalogReader(Protocol):
    """Read access to prompts, catalogs, published versions and subjects' active versions.

    Used as a context manager (``with open_catalog_reader(layout) as catalog:``); ``close``
    releases the implementation and is idempotent, and any call after it fails with
    ``CORPUS.STORE_UNAVAILABLE``. Every method may also fail as the opener does: with
    ``CORPUS.STORE_UNAVAILABLE`` when the store cannot serve the call and
    ``CorpusStoreSecurityError`` (``CORPUS.STORE_UNSAFE``) when its storage fails an
    ownership, mode or identity check. Timestamps are timezone-aware.
    """

    def __enter__(self) -> Self:
        """Returns: this catalog."""

    def __exit__(self, *exc_info: object) -> None:
        """Close the catalog; an exception in the block propagates."""

    def close(self) -> None:
        """Release the catalog; idempotent."""

    def resolve_reference(self, catalog_id: str, version: int | None = None) -> CatalogReference:
        """Look up a published version by catalog id and version number.

        Params: ``catalog_id``; ``version`` or ``None`` for the latest published version.
        Returns: the stored reference.
        Raises: ``CorpusStoreError`` with ``CORPUS.CATALOG_UNKNOWN`` or
        ``CORPUS.VERSION_UNKNOWN`` (no such version -- any number outside 1 to
        ``corpus_store.MAX_CATALOG_VERSION`` included -- or nothing published yet).
        """

    def materialize(self, reference: CatalogReference) -> CatalogSnapshot:
        """Read the snapshot of a published version and verify it against the reference.

        Params: ``reference`` from ``publish_version``, ``resolve_reference`` or
        ``active_catalog``.
        Returns: the ``CatalogSnapshot`` (exact suite plus prompt aliases).
        Raises: ``CorpusStoreError`` with ``CORPUS.CATALOG_UNKNOWN`` or
        ``CORPUS.VERSION_UNKNOWN``; ``CorpusSnapshotSecurityError`` with
        ``CORPUS.SNAPSHOT_MISMATCH`` when the reference disagrees with the published version,
        the snapshot's bytes are not the published ones (suite or aliases) or disagree with
        the version's prompt revisions and aliases, ``CORPUS.SNAPSHOT_MISSING`` when the
        snapshot is gone.
        """

    def list_catalogs(self) -> tuple[CatalogRecord, ...]:
        """Every catalog with its published versions, in creation order.

        Returns: the records, possibly empty.
        """

    def active_catalog(self, subject_id: str) -> tuple[PromptSuite, CatalogReference]:
        """The suite a subject records against, read from its verified snapshot.

        Params: ``subject_id``.
        Returns: the exact ``PromptSuite`` and the reference to the active version.
        Raises: ``CorpusStoreError`` with ``CORPUS.NO_ACTIVE_CATALOG`` (the message names
        ``voice-lab catalog init``); ``CorpusSnapshotSecurityError`` as ``materialize``.
        """

    def list_prompts(self, *, include_retired: bool = False) -> tuple[PromptRecord, ...]:
        """Every prompt's current revision in insertion order.

        Params: ``include_retired`` also lists retired prompts.
        Returns: the records, possibly empty.
        """

    def prompt_history(self, prompt_id: str) -> tuple[PromptRecord, ...]:
        """Every revision of one prompt, ascending.

        Params: ``prompt_id``.
        Returns: the records, revision 1 first.
        Raises: ``CorpusStoreError`` with ``CORPUS.PROMPT_UNKNOWN``.
        """


class CatalogEditor(CatalogReader, Protocol):
    """A ``CatalogReader`` that also changes the store, one transaction per call.

    Every rule is checked inside the change's own transaction, and nothing is written on any
    refusal; concurrent editors are serialized, so published versions are consecutive.
    """

    def add_prompt(
        self,
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
        source: str,
        source_reference: str | None = None,
    ) -> PromptRecord:
        """Add one prompt as revision 1.

        Params: the ``Prompt`` contract fields; ``source`` names where the prompt came from;
        ``source_reference`` is the ``PromptSourceReference`` JSON for imported sets.
        Returns: the stored record with its canonical ``prompt_sha256``.
        Raises: ``CorpusStoreError`` with ``CORPUS.PROMPT_INVALID`` (not a valid ``Prompt``,
        a format or control character, or no prompt-suite/v2 catalog of any purpose could
        hold it), ``CORPUS.SOURCE_INVALID``, ``CORPUS.SOURCE_REFERENCE_INVALID``,
        ``CORPUS.PROMPT_UNPUBLISHABLE`` (a catalog of the store refuses it) or
        ``CORPUS.PROMPT_DUPLICATE`` (the id exists, or a live prompt reads the same).
        """

    def import_prompt_suite(
        self, suite: PromptSuite, *, source: str, source_reference: str | None = None
    ) -> tuple[str, ...]:
        """Seed every prompt of a suite in one transaction, in suite order.

        Params: ``suite`` is a validated ``PromptSuite``; ``source`` and
        ``source_reference`` as in ``add_prompt``, applied to every prompt.
        Returns: the added prompt ids in order.
        Raises: as ``add_prompt``, for any prompt of the suite (also when two of its prompts
        read the same). Nothing is added unless every prompt is.
        """

    def edit_prompt(
        self,
        prompt_id: str,
        *,
        keep_recordings: bool = False,
        allow_rewording: bool = False,
        text: str | None | _Unchanged = UNCHANGED,
        category: str | _Unchanged = UNCHANGED,
        style: str | _Unchanged = UNCHANGED,
        instructions: str | None | _Unchanged = UNCHANGED,
        labels: Sequence[str] | _Unchanged = UNCHANGED,
        expected_seconds: float | _Unchanged = UNCHANGED,
        reference_candidate: bool | _Unchanged = UNCHANGED,
        holdout: bool | _Unchanged = UNCHANGED,
        expected_revision: int | None = None,
        dry_run: bool = False,
    ) -> PromptRecord:
        """Append a revision with the given fields changed; unmentioned fields stay.

        Params: ``prompt_id``; ``keep_recordings`` makes the revision accept the recordings
        of the earlier ones (only a typo-class text change unless ``allow_rewording``, and
        never a change of the held-out flag or of spoken/silent behaviour); each field or
        ``UNCHANGED``; ``expected_revision`` is refused unless it is still the current
        revision; ``dry_run`` runs every check and stores nothing.
        Returns: the new current record (with ``dry_run``, the record the edit would store).
        Raises: ``ValueError`` for ``allow_rewording`` without ``keep_recordings``;
        ``CorpusStoreError`` with ``CORPUS.PROMPT_UNKNOWN``, ``CORPUS.PROMPT_RETIRED``,
        ``CORPUS.PROMPT_CHANGED``, ``CORPUS.PROMPT_INVALID``, ``CORPUS.PROMPT_UNCHANGED``,
        ``CORPUS.PROMPT_DUPLICATE``, ``CORPUS.REWORDING_REFUSED`` or
        ``CORPUS.PROMPT_UNPUBLISHABLE``, checked in this order.
        """

    def retire_prompt(self, prompt_id: str) -> PromptRecord:
        """Retire a prompt: new versions omit it; published snapshots keep it.

        Params: ``prompt_id``.
        Returns: the record with ``retired_at`` set.
        Raises: ``CorpusStoreError`` with ``CORPUS.PROMPT_UNKNOWN`` or
        ``CORPUS.PROMPT_RETIRED``.
        """

    def create_catalog(
        self,
        catalog_id: str,
        *,
        suite_id: str | None = None,
        purpose: str,
        description: str = "",
    ) -> CatalogRecord:
        """Create an empty catalog that later versions materialize as one ``PromptSuite``.

        Params: as ``corpus_store.catalog_definition``.
        Returns: the record with no versions.
        Raises: ``CorpusStoreError`` with ``CORPUS.CATALOG_INVALID``,
        ``CORPUS.CATALOG_DUPLICATE`` or ``CORPUS.CATALOG_UNPUBLISHABLE`` (the purpose
        refuses a live prompt).
        """

    def initialize_catalog(
        self,
        definition: CatalogDefinition,
        *,
        seed: PromptSuite | None = None,
        source: str = "import",
        subject_id: str | None = None,
    ) -> tuple[CatalogRecord, CatalogReference | None]:
        """Set up an empty store in one transaction: seed, create, publish, activate.

        Params: ``definition`` from ``corpus_store.catalog_definition``; ``seed`` becomes
        revision 1 of every prompt and version 1 of the catalog; ``source`` as in
        ``add_prompt``; ``subject_id`` is required with a seed and refused without one.
        Returns: the stored catalog record and the version-1 reference (``None`` without a
        seed).
        Raises: ``ValueError`` for a seed without a subject or a subject without a seed;
        ``CorpusStoreError`` with ``CORPUS.CATALOG_DUPLICATE`` when the store is not empty,
        ``CORPUS.SUBJECT_INVALID``, and as ``import_prompt_suite``, ``create_catalog``,
        ``publish_version`` and ``activate``. All of it is stored or none of it.
        """

    def publish_version(
        self, catalog_id: str, prompt_ids: Sequence[str] | None = None
    ) -> CatalogReference:
        """Materialize, validate, hash and snapshot the next version of a catalog.

        Params: ``catalog_id``; ``prompt_ids`` selects live prompts in the given order, or
        ``None`` for every live prompt in insertion order.
        Returns: the reference to the new version, whose snapshot exists at its
        ``snapshot_path`` before the reference is returned.
        Raises: ``CorpusStoreError`` with ``CORPUS.CATALOG_UNKNOWN``,
        ``CORPUS.PROMPT_UNKNOWN``, ``CORPUS.PROMPT_RETIRED``, ``CORPUS.PROMPT_DUPLICATE``,
        ``CORPUS.EMPTY_CATALOG``, ``CORPUS.CATALOG_INVALID`` (an invalid or oversized suite)
        or ``CORPUS.VERSION_UNCHANGED`` (this exact content is published already);
        ``CorpusSnapshotSecurityError`` when a snapshot of the same digest has other bytes.
        """

    def activate(self, subject_id: str, reference: CatalogReference) -> None:
        """Make a published version the one a subject records against.

        Params: ``subject_id``; ``reference`` to a published version whose snapshot must be
        readable now.
        Returns: nothing; ``active_catalog(subject_id)`` returns this version afterwards.
        Raises: ``CorpusStoreError`` with ``CORPUS.SUBJECT_INVALID``,
        ``CORPUS.CATALOG_UNKNOWN`` or ``CORPUS.VERSION_UNKNOWN``;
        ``CorpusSnapshotSecurityError`` as ``materialize``.
        """
