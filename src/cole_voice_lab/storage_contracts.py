"""The library vault's records: blob and ref values, manifests, the binding, state and ledger.

@canonical storage_contracts -- every value a storage port returns and every record the vault
keeps (library manifests and the envelopes a head names), the Mac keeps under
provenance/vault (the binding, replication state, ledger entries) or the vault server reads
and writes (its namespace configuration and its access-log lines) is one of these strict
models; ref names and library ids are validated here, a manifest's digest is always
manifest_digest (SHA-256 of its canonical JSON), the digest of what a generation carries (its
ManifestPlan, known before any chunk is stored, which the LibraryManifest built after the
upload extends with chunk refs) is always manifest_payload_digest, and a ledger entry's digest
(which the next entry chains to) is always ledger_entry_digest.
Related: storage_ports.py (BlobStore, RefStore, the STORAGE.* errors; re-exports the name
helpers), canonical.py (canonical_json_bytes), schema.py (registers STORAGE_SCHEMA_MODELS),
tests/test_storage_contracts.py.

This module imports only the standard library, pydantic and the package's ``errors``,
``models``, ``paths`` and ``canonical`` modules: the vault server loads it, so it must never
pull in the recording, capture or catalog stack. Every record is a strict ``ContractModel``;
each rule that ties two fields together is a model validator whose message names the rule.
Size, version, generation and sequence numbers are capped at ``MAX_STORAGE_COUNTER``
(2**63 - 1) so every value fits a signed 64-bit integer.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import UTC, datetime
from typing import Annotated, Final, Literal, NamedTuple, Self

from pydantic import AfterValidator, Field, StringConstraints, field_validator, model_validator

from .canonical import canonical_sha256
from .errors import ContractError
from .models import ContractModel, Sha256, StableId, has_hidden_control
from .paths import validate_absolute_path, validate_relative_path

MAX_BLOB_BYTES: Final = 16 * 1024 * 1024
MAX_STORAGE_COUNTER: Final = 2**63 - 1
MAX_REF_NAME_LENGTH: Final = 256
MAX_MANIFEST_PATH_LENGTH: Final = 1024
MAX_ENDPOINT_LENGTH: Final = 1024
MAX_DNS_NAME_LENGTH: Final = 253
# A tailnet user login (``person@example.com`` or ``person@github``) and one proxy-set
# forwarded-for value, as the vault server may log them.
MAX_USER_LOGIN_LENGTH: Final = 320
MAX_FORWARDED_FOR_LENGTH: Final = 256
# The vault server's bounds: enough workers for one Mac's pushes, and a request timeout whose
# ceiling still allows a 16 MiB blob to arrive whole over a slow tailnet link (the timeout is
# one deadline for the whole request, so the default suits a fast link only).
MAX_VAULT_WORKERS: Final = 64
DEFAULT_VAULT_WORKERS: Final = 8
MAX_VAULT_REQUEST_TIMEOUT_SECONDS: Final = 600
DEFAULT_VAULT_REQUEST_TIMEOUT_SECONDS: Final = 120
# How often the Mac's client may send one request: a first try and a few retries of what may
# pass by itself. It bounds the ``attempt`` a request record carries.
MAX_VAULT_ATTEMPTS: Final = 16
# The largest manifest (its canonical JSON) a generation stores or a reader accepts: far above
# a real library's (tens of thousands of entries are a few tens of MiB) and small enough to
# assemble in memory.
MAX_MANIFEST_BYTES: Final = 256 * 1024 * 1024
EMPTY_SHA256: Final = hashlib.sha256(b"").hexdigest()
# Manifest paths never name this directory (or anything in it): the binding, approvals,
# replication state, ledger and request log stay on the Mac and are never restored.
VAULT_PROVENANCE_ROOT: Final = "provenance/vault"
KEYCHAIN_ITEM_PREFIX: Final = "private.cole.voice-lab.vault."
# Names the hashed plan format and keeps a payload digest apart from the digest of any other
# canonical JSON, a manifest's included.
MANIFEST_PAYLOAD_DOMAIN: Final = "cole-voice-lab.library-manifest-payload/v1"

_REF_SEGMENT = r"[a-z0-9][a-z0-9._-]{0,63}"
# Up to eight segments of at most 64 characters; a segment starts with a letter or digit, so
# "." and ".." (and empty segments) can never match.
REF_NAME_PATTERN: Final = rf"^{_REF_SEGMENT}(?:/{_REF_SEGMENT}){{0,7}}$"
# A StableId (models.StableId) short enough to be one ref segment.
LIBRARY_ID_PATTERN: Final = r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$"
NAMESPACE_PATTERN: Final = r"^[a-z][a-z0-9-]{0,62}$"
NODE_STABLE_ID_PATTERN: Final = r"^[A-Za-z0-9]{1,64}$"
TAILSCALE_TAG_PATTERN: Final = r"^tag:[A-Za-z][A-Za-z0-9-]{0,123}$"
# A vault access token: the prefix names the format, and the 43 base64url characters are the
# 256 bits ``secrets.token_bytes(32)`` returned. The server keeps only sha256(token) hex.
VAULT_TOKEN_PREFIX: Final = "cvlv_"
VAULT_TOKEN_PATTERN: Final = rf"^{VAULT_TOKEN_PREFIX}[A-Za-z0-9_-]{{43}}$"
# One ``DOMAIN.FAILURE`` storage code as a logged or transmitted string. The codes themselves
# are ``storage_ports.StorageErrorCode``, which imports this module, so a record spells the
# shape and ``tests/test_storage_server.py`` pins the two to each other.
STORAGE_CODE_PATTERN: Final = r"^STORAGE\.[A-Z][A-Z0-9_]{0,30}$"
PIN_SOURCE_PATTERN: Final = r"^[a-z][a-z0-9_-]{0,63}(?:\.[a-z][a-z0-9_-]{0,63}){0,7}$"

_REF_NAME = re.compile(REF_NAME_PATTERN)
_LIBRARY_ID = re.compile(LIBRARY_ID_PATTERN)
_DNS_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
# Two or more lowercase labels, no trailing dot; the last label has a letter, so an IPv4
# literal is not a name.
_DNS_NAME = re.compile(rf"(?:{_DNS_LABEL}\.)+(?=[a-z0-9-]*[a-z]){_DNS_LABEL}")
# Characters a restorable path or a directory endpoint never carries, beyond what
# ``has_hidden_control`` refuses: line and paragraph separators. (A lone surrogate, which is
# how Python spells an undecodable file name, never reaches a validator: pydantic refuses it
# as an invalid string.)
_UNSAFE_EXTRA_CATEGORIES = frozenset({"Zl", "Zp"})


class StorageNameError(ContractError, ValueError):
    """A ref name, or a library id that must become one ref segment, is not valid.

    It is a ``ContractError`` (reported by the CLI like any contract failure) and a
    ``ValueError`` (so a pydantic validator that calls the helpers reports it as a validation
    error); it is never a ``StorageError``, because no store was asked anything.
    ``str(error)`` is the message, which names the rule and repeats the rejected value.
    """


def validate_ref_name(name: str) -> str:
    """Return ``name`` when it can name a ref in a ``RefStore``.

    Params: ``name``, for example ``libraries/<library_id>/head``.
    Returns: ``name`` unchanged.
    Raises: ``StorageNameError`` unless ``name`` is at most ``MAX_REF_NAME_LENGTH``
    characters of one to eight ``/``-separated segments, each 1-64 characters of lowercase
    ASCII letters, digits, ``.``, ``_`` or ``-`` starting with a letter or digit (so an
    empty, ``.`` or ``..`` segment, a leading or trailing ``/`` and uppercase are refused).
    """
    if len(name) > MAX_REF_NAME_LENGTH or _REF_NAME.fullmatch(name) is None:
        raise StorageNameError(
            f"a ref name is 1-8 '/'-separated segments of [a-z0-9._-] (each starting with a "
            f"letter or digit, at most 64 characters), at most {MAX_REF_NAME_LENGTH} "
            f"characters in all: {name!r}"
        )
    return name


def validate_library_id(library_id: str) -> str:
    """Return ``library_id`` when it is a stable id that fits one ref segment.

    Params: ``library_id``.
    Returns: ``library_id`` unchanged.
    Raises: ``StorageNameError`` unless it is a ``models.StableId`` of at most 64
    characters (lowercase letters, digits, ``.``, ``_``, ``-``; a letter or digit first and
    last).
    """
    if _LIBRARY_ID.fullmatch(library_id) is None:
        raise StorageNameError(
            "a library id is a stable id of at most 64 characters ([a-z0-9._-], starting and "
            f"ending with a letter or digit): {library_id!r}"
        )
    return library_id


def library_head_ref(library_id: str) -> str:
    """The name of the ref that points at a library's newest manifest.

    Params: ``library_id``.
    Returns: ``libraries/<library_id>/head``.
    Raises: ``StorageNameError`` when ``library_id`` is not a valid library id.
    """
    return validate_ref_name(f"libraries/{validate_library_id(library_id)}/head")


def validate_tailnet_endpoint(endpoint: str) -> str:
    """Return ``endpoint`` when it can name the vault's node on the tailnet.

    Params: ``endpoint``, for example ``voice-lab-vault.colobus-stargazer.ts.net``.
    Returns: ``endpoint`` unchanged.
    Raises: ``StorageNameError`` unless it is a lowercase DNS name of two or more labels
    whose last label holds a letter, at most ``MAX_DNS_NAME_LENGTH`` characters, with no
    scheme, port, path or trailing dot; an ``http://`` string and an IP literal are refused,
    so a client can only ever speak TLS to a name.
    """
    problem = _endpoint_problem("tailnet-vault", endpoint)
    if problem is not None:
        raise StorageNameError(f"{problem}: {endpoint!r:.120}")
    return endpoint


class KeychainItemNames(NamedTuple):
    """The login-Keychain item names a library's vault secrets are kept under."""

    key: str
    token: str


def keychain_item_names(library_id: str) -> KeychainItemNames:
    """Derive the Keychain item names for a library; bindings name exactly these.

    Params: ``library_id``.
    Returns: ``private.cole.voice-lab.vault.<library_id>.key`` (the library key, used for
    every backend) and ``private.cole.voice-lab.vault.<library_id>.token`` (the vault access
    token, used by ``tailnet-vault``).
    Raises: ``StorageNameError`` when ``library_id`` is not a valid library id.
    """
    prefix = f"{KEYCHAIN_ITEM_PREFIX}{validate_library_id(library_id)}"
    return KeychainItemNames(key=f"{prefix}.key", token=f"{prefix}.token")


def manifest_path_key(path: str) -> str:
    """The key under which two paths name one file on a case- and normalization-blind volume.

    APFS treats ``raw/A.wav`` and ``raw/a.wav``, and the NFC and NFD spellings of a name, as
    the same file, so a manifest refuses two entries with one key.
    Params: ``path``, a manifest path.
    Returns: the NFC form of the Unicode case fold of the NFD form of ``path``.
    Raises: nothing.
    """
    return unicodedata.normalize("NFC", unicodedata.normalize("NFD", path).casefold())


def manifest_digest(manifest: LibraryManifest) -> str:
    """The digest that names a manifest: SHA-256 of its canonical JSON bytes.

    The bytes are ``canonical.canonical_json_bytes(manifest)`` (sorted keys, no whitespace, no
    trailing newline); an ``upload-completed`` ledger entry, the replication state and the next
    generation's ``previous_manifest_sha256`` all record this value. It covers every chunk ref,
    and a sealed chunk's ref exists only once the chunk is stored, so nothing written before an
    upload can name it: that is what ``manifest_payload_digest`` of the ``ManifestPlan`` is for.
    Params: ``manifest``.
    Returns: 64 lowercase hex characters.
    Raises: nothing.
    """
    return canonical_sha256(manifest)


def manifest_plan(manifest: ManifestPlan) -> ManifestPlan:
    """The plan a manifest fulfils: the same generation and files, without any chunk ref.

    A ``LibraryManifest`` is a ``ManifestPlan`` whose entries also carry chunks. This returns
    the exact ``ManifestPlan`` (of exact ``PlannedManifestEntry`` values) it extends, so
    ``manifest_plan(manifest) == plan`` shows that a manifest fulfils ``plan``; a plan comes
    back as an equal plan.
    Params: ``manifest``, a ``LibraryManifest`` or a ``ManifestPlan``.
    Returns: a new, validated ``ManifestPlan``.
    Raises: ``pydantic.ValidationError`` only for a value built without validation that breaks
    a plan rule.
    """
    entries = tuple(
        PlannedManifestEntry.model_validate(
            {name: getattr(entry, name) for name in PlannedManifestEntry.model_fields}
        )
        for entry in manifest.entries
    )
    fields = {name: getattr(manifest, name) for name in ManifestPlan.model_fields}
    return ManifestPlan.model_validate(fields | {"entries": entries})


def manifest_payload_digest(plan: ManifestPlan) -> str:
    """The digest of what a generation carries, known before any of its chunks is stored.

    Before sealing or storing anything, the replicator builds the generation's
    ``ManifestPlan`` from the files it read and names this digest in the ``upload-started``
    ledger entry; after the upload it builds the ``LibraryManifest`` from the same plan plus
    each file's chunk refs, and that manifest has the same payload digest. The hashed bytes
    are ``canonical.canonical_json_bytes`` of ``{"domain": MANIFEST_PAYLOAD_DOMAIN, "plan": P}``,
    where ``P`` is the JSON of ``manifest_plan(plan)``: ``library_id``, ``generation``,
    ``previous_manifest_sha256``, ``created_at``, ``classes`` and each entry's ``path``,
    ``file_class``, ``kind``, ``sha256``, ``size``, ``mode`` and ``pin_source``. They never
    include a chunk ref (a chunk's ref changes every time it is sealed) or the manifest's
    ``schema_version`` (the domain names the format).
    Params: ``plan``, a ``ManifestPlan`` or a ``LibraryManifest``.
    Returns: 64 lowercase hex characters; the hashed bytes always differ from a manifest's
    own, so this is never a ``manifest_digest``.
    Raises: ``pydantic.ValidationError`` only as ``manifest_plan`` does.
    """
    planned = manifest_plan(plan).model_dump(mode="json")
    return canonical_sha256({"domain": MANIFEST_PAYLOAD_DOMAIN, "plan": planned})


def ledger_entry_digest(entry: LedgerEntry) -> str:
    """The digest that names a ledger entry: SHA-256 of its canonical JSON bytes.

    The next entry's ``previous_entry_sha256`` records this value, so changing, removing or
    reordering any entry but the last breaks the chain. The hashed bytes are
    ``canonical.canonical_json_bytes(entry)`` (no trailing newline), the bytes of the entry's
    file without the newline it ends with.
    Params: ``entry``.
    Returns: 64 lowercase hex characters.
    Raises: nothing.
    """
    return canonical_sha256(entry)


def _utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("storage timestamps require an explicit timezone")
    return value.astimezone(UTC)


def _has_unsafe_character(value: str) -> bool:
    return has_hidden_control(value) or any(
        unicodedata.category(character) in _UNSAFE_EXTRA_CATEGORIES for character in value
    )


def _plain_line(value: str) -> str:
    """Return ``value`` when it can be written as one field of a JSON line.

    Raises: ``ValueError`` for a hidden, control or line-separator character, which a log
    reader or a header comparison must never have to guess at.
    """
    if _has_unsafe_character(value) or "\n" in value:
        raise ValueError(
            "this value cannot contain hidden, control, newline or separator characters"
        )
    return value


def _sorted_unique_classes(values: tuple[FileClass, ...]) -> tuple[FileClass, ...]:
    if len(set(values)) != len(values):
        raise ValueError("classes must not repeat")
    if list(values) != sorted(values):
        raise ValueError("classes must be sorted")
    return values


def _empty_digest_problem(sha256: str, size: int) -> str | None:
    if (size == 0) != (sha256 == EMPTY_SHA256):
        return "the empty SHA-256 belongs to size 0 and size 0 to the empty SHA-256"
    return None


def _endpoint_problem(backend: StorageBackend, endpoint: str) -> str | None:
    if backend == "tailnet-vault":
        if len(endpoint) > MAX_DNS_NAME_LENGTH or _DNS_NAME.fullmatch(endpoint) is None:
            return (
                "a tailnet-vault endpoint is a lowercase DNS name of two or more labels with a "
                f"letter in its last label, at most {MAX_DNS_NAME_LENGTH} characters, no scheme, "
                "port or trailing dot"
            )
        return None
    try:
        validate_absolute_path(endpoint)
    except ValueError:
        return "a local-directory endpoint is an absolute, normalized directory path"
    if _has_unsafe_character(endpoint):
        return "a local-directory endpoint cannot contain hidden, control or separator characters"
    return None


type UtcTimestamp = Annotated[datetime, AfterValidator(_utc)]
type RelativePath = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_MANIFEST_PATH_LENGTH),
    AfterValidator(validate_relative_path),
]
type RefName = Annotated[
    str, StringConstraints(pattern=REF_NAME_PATTERN, max_length=MAX_REF_NAME_LENGTH)
]
type LibraryId = Annotated[str, StringConstraints(pattern=LIBRARY_ID_PATTERN, max_length=64)]
type Namespace = Annotated[str, StringConstraints(pattern=NAMESPACE_PATTERN)]
type Endpoint = Annotated[str, StringConstraints(min_length=1, max_length=MAX_ENDPOINT_LENGTH)]
type NodeStableId = Annotated[str, StringConstraints(pattern=NODE_STABLE_ID_PATTERN)]
type TailscaleTag = Annotated[str, StringConstraints(pattern=TAILSCALE_TAG_PATTERN)]
type PinSource = Annotated[str, StringConstraints(pattern=PIN_SOURCE_PATTERN, max_length=256)]
type StorageCounter = Annotated[int, Field(ge=0, le=MAX_STORAGE_COUNTER)]
type PositiveStorageCounter = Annotated[int, Field(ge=1, le=MAX_STORAGE_COUNTER)]
type StorageBackend = Literal["local-directory", "tailnet-vault"]
# ``training`` (fine-tune runs under derived/finetune and datasets under derived/datasets) is
# the separate opt-in class. Speak clips (derived/speech) have no class: they stay on the Mac.
type FileClass = Literal["records", "prompt-database", "recordings", "derived-edits", "training"]
type FileClasses = Annotated[
    tuple[FileClass, ...], Field(min_length=1), AfterValidator(_sorted_unique_classes)
]
type ManifestEntryKind = Literal["file", "pin"]
type LedgerEntryKind = Literal["upload-started", "upload-completed"]
type UserLogin = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_USER_LOGIN_LENGTH),
    AfterValidator(_plain_line),
]
type ForwardedFor = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_FORWARDED_FOR_LENGTH),
    AfterValidator(_plain_line),
]
# A digest or a ref name: what the request's path named, once it parsed.
type AccessedName = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_REF_NAME_LENGTH),
    AfterValidator(_plain_line),
]
type StorageCodeText = Annotated[str, StringConstraints(pattern=STORAGE_CODE_PATTERN)]
type HttpStatus = Annotated[int, Field(ge=100, le=599)]
# ``none`` is every request that presented no token of this namespace, the health endpoint
# included; the write token also has read scope, so ``write`` implies it.
type VaultScope = Literal["read", "write", "none"]
# The endpoint a request reached, or ``refused`` when its path, method or framing never named
# one.
type VaultOperation = Literal[
    "put-blob", "has-blob", "get-blob", "read-ref", "swap-ref", "health", "refused"
]
# What the Mac's client asks for: the five store operations alone. It never calls the health
# endpoint, and a request it refuses before sending is no request.
type VaultRequestOperation = Literal["put-blob", "has-blob", "get-blob", "read-ref", "swap-ref"]
type VaultAttempt = Annotated[int, Field(ge=1, le=MAX_VAULT_ATTEMPTS)]


class BlobRef(ContractModel):
    """One stored blob, named by the SHA-256 and size of the bytes the store keeps."""

    sha256: Sha256
    size: int = Field(ge=0, le=MAX_BLOB_BYTES)

    @model_validator(mode="after")
    def _empty_blob_has_the_empty_digest(self) -> BlobRef:
        problem = _empty_digest_problem(self.sha256, self.size)
        if problem is not None:
            raise ValueError(problem)
        return self


class RefValue(ContractModel):
    """A named pointer at one blob; ``version`` counts its swaps, starting at 1."""

    name: RefName
    version: PositiveStorageCounter
    target: BlobRef
    updated_at: UtcTimestamp


class PlannedManifestEntry(ContractModel):
    """One file a generation will carry, as known before any of its chunks is stored.

    ``sha256`` and ``size`` are always the file's own; ``mode`` is the mode restore writes
    (directories derive 0500 from 0400 and 0700 from 0600). A ``file`` entry is uploaded. A
    ``pin`` entry is not, because a pin elsewhere identifies it (a fine-tune run's hard-linked
    pretrained weights, for example); only a pin has a ``pin_source``, which names the pinned
    digest field as ``<schema name>.<field path>`` (for example
    ``finetune-run.runtime.pretrained_sha256``).
    """

    path: RelativePath
    file_class: FileClass
    kind: ManifestEntryKind
    sha256: Sha256
    size: StorageCounter
    mode: Literal[0o400, 0o600]
    pin_source: PinSource | None

    @field_validator("path")
    @classmethod
    def _restorable_path(cls, value: str) -> str:
        if _has_unsafe_character(value):
            raise ValueError(
                "manifest paths cannot contain hidden, control or separator characters"
            )
        key = manifest_path_key(value)
        if key == VAULT_PROVENANCE_ROOT or key.startswith(f"{VAULT_PROVENANCE_ROOT}/"):
            raise ValueError(
                "manifest paths cannot name provenance/vault, which never leaves the Mac"
            )
        return value

    @model_validator(mode="after")
    def _pin_source_matches_the_kind(self) -> Self:
        problem = _empty_digest_problem(self.sha256, self.size)
        if problem is not None:
            raise ValueError(problem)
        if (self.pin_source is not None) != (self.kind == "pin"):
            raise ValueError("pin_source is required for a pin entry and forbidden for a file")
        return self


class ManifestEntry(PlannedManifestEntry):
    """One file of a stored library generation: its planned entry plus its chunk refs.

    Every ``PlannedManifestEntry`` rule applies. ``chunks`` are the refs of the file's stored
    pieces, in order: a ``file`` entry has them exactly when ``size`` is above 0, and a
    ``pin`` entry has none.
    """

    chunks: tuple[BlobRef, ...]

    @model_validator(mode="after")
    def _chunks_match_the_kind(self) -> Self:
        if self.kind == "pin" and self.chunks:
            raise ValueError("a pin entry carries no chunks")
        if self.kind == "file" and bool(self.chunks) != (self.size > 0):
            raise ValueError("a file entry has chunks exactly when its size is above 0")
        return self


class ManifestPlan(ContractModel):
    """What one generation of a library carries, known before any of its chunks is stored.

    ``previous_manifest_sha256`` is the ``manifest_digest`` of generation ``generation - 1``
    and is null exactly for generation 1. ``classes`` are the file classes this generation
    covers (sorted, unique); ``entries`` are sorted by path, name no path twice under
    ``manifest_path_key``, never name a path that another entry uses as a directory, and
    each belongs to one of ``classes``. A plan is not a stored record: the
    ``upload-started`` ledger entry names its ``manifest_payload_digest``, and the
    ``LibraryManifest`` stored after the upload is the plan with every entry's chunks.
    """

    library_id: LibraryId
    generation: PositiveStorageCounter
    previous_manifest_sha256: Sha256 | None
    created_at: UtcTimestamp
    classes: FileClasses
    entries: tuple[PlannedManifestEntry, ...]

    @model_validator(mode="after")
    def _entries_form_one_restorable_tree(self) -> Self:
        if (self.previous_manifest_sha256 is None) != (self.generation == 1):
            raise ValueError("previous_manifest_sha256 is null exactly for generation 1")
        paths = [entry.path for entry in self.entries]
        if paths != sorted(paths):
            raise ValueError("manifest entries must be sorted by path")
        keys = [manifest_path_key(path) for path in paths]
        unique = set(keys)
        if len(unique) != len(keys):
            raise ValueError(
                "manifest paths must be unique ignoring case and Unicode normalization"
            )
        for key in keys:
            parent = key.rpartition("/")[0]
            while parent:
                if parent in unique:
                    raise ValueError("a manifest path cannot be both a file and a directory")
                parent = parent.rpartition("/")[0]
        if not {entry.file_class for entry in self.entries} <= set(self.classes):
            raise ValueError("every manifest entry's class must be one of the manifest's classes")
        return self


class LibraryManifest(ManifestPlan):
    """One stored generation of a library: its plan, with the chunk refs of every file.

    Every ``ManifestPlan`` rule applies, and each entry is a ``ManifestEntry``.
    ``manifest_plan`` recovers the plan, whose ``manifest_payload_digest`` is the manifest's.
    """

    schema_version: Literal["cole-voice-lab.library-manifest/v1"] = (
        "cole-voice-lab.library-manifest/v1"
    )
    entries: tuple[ManifestEntry, ...]


class ManifestEnvelope(ContractModel):
    """Where one generation's manifest is stored: the blob a library's head ref names.

    A manifest's canonical JSON (``size`` bytes, SHA-256 ``manifest_sha256``, which is its
    ``manifest_digest``) is stored as the blobs ``chunks``, in order, each holding at least one
    byte of it; the envelope is stored (sealed, like every blob) as one more blob, and the head
    ref's target names that blob. ``library_id`` and ``generation`` repeat the manifest's, so a
    reader refuses a manifest that is not the one the envelope announces. At most
    ``MAX_MANIFEST_BYTES`` are announced.
    """

    schema_version: Literal["cole-voice-lab.library-manifest-envelope/v1"] = (
        "cole-voice-lab.library-manifest-envelope/v1"
    )
    library_id: LibraryId
    generation: PositiveStorageCounter
    manifest_sha256: Sha256
    size: int = Field(ge=1, le=MAX_MANIFEST_BYTES)
    chunks: tuple[BlobRef, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _chunks_can_hold_the_manifest(self) -> ManifestEnvelope:
        if not len(self.chunks) <= self.size <= len(self.chunks) * MAX_BLOB_BYTES:
            raise ValueError(
                "a manifest is stored in chunks of 1 to MAX_BLOB_BYTES of its bytes each, so "
                "size is at least the chunk count and at most the chunk count times "
                "MAX_BLOB_BYTES"
            )
        return self


class StorageBinding(ContractModel):
    """Which vault a library copies to, and what it copies (``provenance/vault/binding.json``).

    ``tailnet-vault`` names a lowercase DNS ``endpoint`` and requires ``approval_id`` and
    ``token_item``; ``local-directory`` names an absolute, normalized directory and has
    neither. ``classes`` (sorted, unique) are the file classes copied to that destination on
    every backend, so ``training`` goes only where the binding opts in; for ``tailnet-vault``
    the approval must also cover each of them, which is checked where the approval is loaded.
    ``key_item`` and ``token_item`` are always the names ``keychain_item_names`` derives from
    ``library_id``. The binding holds no secret and no path to one.
    """

    schema_version: Literal["cole-voice-lab.storage-binding/v1"] = (
        "cole-voice-lab.storage-binding/v1"
    )
    backend: StorageBackend
    endpoint: Endpoint
    namespace: Namespace
    library_id: LibraryId
    classes: FileClasses
    approval_id: StableId | None
    key_item: str = Field(min_length=1, max_length=256)
    token_item: str | None = Field(min_length=1, max_length=256)
    created_at: UtcTimestamp

    @model_validator(mode="after")
    def _backend_names_its_own_fields(self) -> StorageBinding:
        problem = _endpoint_problem(self.backend, self.endpoint)
        if problem is not None:
            raise ValueError(problem)
        tailnet = self.backend == "tailnet-vault"
        if (self.approval_id is not None) != tailnet:
            raise ValueError(
                "approval_id is required for tailnet-vault and forbidden for local-directory"
            )
        names = keychain_item_names(self.library_id)
        if self.key_item != names.key:
            raise ValueError(f"key_item must be {names.key}")
        if self.token_item != (names.token if tailnet else None):
            raise ValueError(
                f"token_item must be {names.token} for tailnet-vault and null for local-directory"
            )
        return self


class ReplicationState(ContractModel):
    """The Mac's record of the newest generation it pushed (``provenance/vault/state.json``).

    Generation 0 means nothing was pushed yet, and only then are ``manifest_sha256`` and
    ``head`` null; ``head`` is always this library's head ref.
    """

    schema_version: Literal["cole-voice-lab.replication-state/v1"] = (
        "cole-voice-lab.replication-state/v1"
    )
    library_id: LibraryId
    generation: StorageCounter
    manifest_sha256: Sha256 | None
    head: RefValue | None
    updated_at: UtcTimestamp

    @model_validator(mode="after")
    def _pushed_state_names_its_head(self) -> ReplicationState:
        pushed = self.generation > 0
        if (self.manifest_sha256 is not None) != pushed:
            raise ValueError("manifest_sha256 is null exactly for generation 0")
        if (self.head is not None) != pushed:
            raise ValueError("head is null exactly for generation 0")
        if self.head is not None and self.head.name != library_head_ref(self.library_id):
            raise ValueError("head must be this library's head ref")
        return self


class LedgerDestination(ContractModel):
    """Where a generation was sent; the node id and tag identify a ``tailnet-vault`` node."""

    backend: StorageBackend
    endpoint: Endpoint
    namespace: Namespace
    node_stable_id: NodeStableId | None
    tag: TailscaleTag | None

    @model_validator(mode="after")
    def _tailnet_destination_names_its_node(self) -> LedgerDestination:
        problem = _endpoint_problem(self.backend, self.endpoint)
        if problem is not None:
            raise ValueError(problem)
        tailnet = self.backend == "tailnet-vault"
        if (self.node_stable_id is not None) != tailnet:
            raise ValueError(
                "node_stable_id is required for tailnet-vault and forbidden for local-directory"
            )
        if (self.tag is not None) != tailnet:
            raise ValueError("tag is required for tailnet-vault and forbidden for local-directory")
        return self


class LedgerEntry(ContractModel):
    """One append-only ledger record (``provenance/vault/ledger/<sequence:08d>.json``).

    The entries form a chain: ``previous_entry_sha256`` is null exactly for sequence 1.
    ``upload-started`` is written before the first new blob of a generation leaves the Mac, so
    it names only what is known then: ``payload_sha256``, the ``manifest_payload_digest`` of
    the generation's ``ManifestPlan`` (every file's path, class, kind, digest, size, mode and
    pin, but no chunk ref, since a chunk's ref exists only once the sealed chunk is stored).
    ``upload-completed`` is written after the head swap and adds ``manifest_sha256`` (the
    ``manifest_digest`` of the plaintext ``LibraryManifest`` the head now names, which
    extends that plan with its chunk refs) and ``head_version`` (the head's new version).
    ``approval_sha256`` is the digest of the destination approval the upload relies on:
    required for a ``tailnet-vault`` destination and null for ``local-directory``, which needs
    no approval (as in ``StorageBinding``). ``classes`` are the binding's classes.
    ``byte_count`` and ``blob_count`` count the stored bytes and blobs of the upload (planned
    in a started entry, sent in a completed one), so the bytes never exceed ``blob_count``
    full blobs.

    Pairing, which no single entry can show and the ledger reader checks: an
    ``upload-completed`` entry directly follows an ``upload-started`` entry with the same
    ``library_id``, ``generation``, ``approval_sha256``, ``destination``, ``payload_sha256``
    and ``classes``, and the manifest it names has ``payload_sha256`` as its payload digest
    (its ``manifest_plan`` is the plan the started entry names).
    A started entry with no completed entry after it records an upload that stopped.
    """

    schema_version: Literal["cole-voice-lab.replication-ledger-entry/v1"] = (
        "cole-voice-lab.replication-ledger-entry/v1"
    )
    sequence: PositiveStorageCounter
    previous_entry_sha256: Sha256 | None
    kind: LedgerEntryKind
    library_id: LibraryId
    generation: PositiveStorageCounter
    approval_sha256: Sha256 | None
    destination: LedgerDestination
    payload_sha256: Sha256
    manifest_sha256: Sha256 | None
    classes: FileClasses
    byte_count: StorageCounter
    blob_count: StorageCounter
    head_version: PositiveStorageCounter | None
    recorded_at: UtcTimestamp

    @model_validator(mode="after")
    def _entry_is_chained_and_complete(self) -> LedgerEntry:
        if (self.previous_entry_sha256 is None) != (self.sequence == 1):
            raise ValueError("previous_entry_sha256 is null exactly for sequence 1")
        if (self.approval_sha256 is not None) != (self.destination.backend == "tailnet-vault"):
            raise ValueError(
                "approval_sha256 is required for tailnet-vault and forbidden for local-directory"
            )
        completed = self.kind == "upload-completed"
        if (self.manifest_sha256 is not None) != completed:
            raise ValueError(
                "manifest_sha256 is required for upload-completed and forbidden for upload-started"
            )
        if (self.head_version is not None) != completed:
            raise ValueError(
                "head_version is required for upload-completed and forbidden for upload-started"
            )
        if self.byte_count > self.blob_count * MAX_BLOB_BYTES:
            raise ValueError("byte_count cannot exceed blob_count blobs of MAX_BLOB_BYTES")
        return self


class VaultNamespaceConfig(ContractModel):
    """One namespace the vault server serves: its token hashes, its quota and its logins.

    A token is never stored: ``read_token_sha256`` and ``write_token_sha256`` are the SHA-256
    hex of the two tokens, they differ from each other (and, across the configuration, from
    every other namespace's), and the write token also has read scope.  ``quota_bytes`` is the
    most this namespace's blobs may occupy; a put beyond it is refused with ``STORAGE.QUOTA``.
    ``allowed_user_logins`` is defence in depth on top of the tokens: when it is empty the
    proxy's ``Tailscale-User-Login`` header is not checked, and otherwise a request must carry
    exactly one of these logins.
    """

    name: Namespace
    read_token_sha256: Sha256
    write_token_sha256: Sha256
    quota_bytes: PositiveStorageCounter
    allowed_user_logins: tuple[UserLogin, ...] = ()

    @model_validator(mode="after")
    def _the_two_scopes_have_two_tokens(self) -> Self:
        if self.read_token_sha256 == self.write_token_sha256:
            raise ValueError("a namespace's read and write token hashes must differ")
        if len(set(self.allowed_user_logins)) != len(self.allowed_user_logins):
            raise ValueError("allowed_user_logins must not repeat")
        return self


class VaultServerConfig(ContractModel):
    """What the vault server is given at startup (a read-only file; no raw token is in it).

    ``namespaces`` names at least one namespace, no name twice and no token hash twice, so a
    token grants exactly one namespace and one scope. ``max_workers`` bounds the request
    threads (a request that finds them all busy is refused with ``STORAGE.UNAVAILABLE``
    instead of queueing), and ``request_timeout_seconds`` bounds a request's whole read phase
    (its request line, headers, body and any drained refusal together) as one wall-clock
    deadline taken when a worker picks the connection up, not as a per-read socket timeout. A
    blob upload must therefore *complete* within it or it is refused with ``STORAGE.TIMEOUT``:
    at the default of 120 seconds a 16 MiB blob must average about 140 KB/s, so a slower link
    needs a larger value (up to 600 seconds, which allows about 28 KB/s). Writing the answer
    gets the same number of seconds again as its own budget.
    """

    schema_version: Literal["cole-voice-lab.vault-server-config/v1"] = (
        "cole-voice-lab.vault-server-config/v1"
    )
    namespaces: tuple[VaultNamespaceConfig, ...] = Field(min_length=1)
    max_workers: int = Field(default=DEFAULT_VAULT_WORKERS, ge=1, le=MAX_VAULT_WORKERS)
    request_timeout_seconds: int = Field(
        default=DEFAULT_VAULT_REQUEST_TIMEOUT_SECONDS,
        ge=1,
        le=MAX_VAULT_REQUEST_TIMEOUT_SECONDS,
    )

    @model_validator(mode="after")
    def _no_name_and_no_token_serves_twice(self) -> Self:
        names = [namespace.name for namespace in self.namespaces]
        if len(set(names)) != len(names):
            raise ValueError("namespace names must be unique")
        hashes = [
            digest
            for namespace in self.namespaces
            for digest in (namespace.read_token_sha256, namespace.write_token_sha256)
        ]
        if len(set(hashes)) != len(hashes):
            raise ValueError("no token hash may serve two namespaces or two scopes")
        return self


class VaultAccessRecord(ContractModel):
    """One line of the vault server's access log, appended for every answered request.

    It records what was asked and how it was answered, never a credential: no ``Authorization``
    header, no token hash and no byte of any body. ``namespace`` and ``ref`` (a blob digest or
    a ref name) appear only once the path parsed; ``scope`` is the scope of the token the
    request presented, so a request without one is ``none``. ``operation`` is ``refused`` when
    the path, method or framing never named an endpoint, and a refusal always has a status of
    400 or more and a ``code``, which every answer under 400 lacks. ``bytes`` counts the blob
    bytes accepted or sent, so only a blob operation has any. ``forwarded_for`` and
    ``user_login`` are the proxy's ``X-Forwarded-For`` and ``Tailscale-User-Login`` values,
    informational and null unless exactly one arrived in a form that can be logged.
    """

    time: UtcTimestamp
    namespace: Namespace | None
    scope: VaultScope
    operation: VaultOperation
    ref: AccessedName | None
    bytes: StorageCounter
    status: HttpStatus
    code: StorageCodeText | None
    forwarded_for: ForwardedFor | None
    user_login: UserLogin | None

    @model_validator(mode="after")
    def _the_record_answers_one_request(self) -> Self:
        if self.ref is not None and self.namespace is None:
            raise ValueError("a ref is recorded only with the namespace whose path named it")
        if (self.code is not None) != (self.status >= 400):
            raise ValueError("a code is recorded exactly for a status of 400 or more")
        if self.operation == "health" and (
            self.scope != "none" or self.namespace is not None or self.ref is not None
        ):
            raise ValueError("the health endpoint takes no namespace, scope or ref")
        if self.operation == "refused" and self.status < 400:
            raise ValueError("a refused request is never answered under 400")
        if self.status < 400 and self.scope == "none" and self.operation != "health":
            raise ValueError("only the health endpoint answers without a scope")
        if self.bytes > 0 and self.operation not in {"put-blob", "get-blob"}:
            raise ValueError("only a blob operation accepts or sends bytes")
        return self


class VaultRequestRecord(ContractModel):
    """One attempt the Mac's vault client made, written where the Mac can audit it.

    It records what was asked of which vault and how the attempt ended, never a credential:
    no token, no token hash and no byte of any blob. ``endpoint`` is the name the client
    spoke to (and the ``Host`` it sent), ``node_stable_id`` the tailnet node the identity
    check accepted, which is null when no identity was checked. ``ref`` is the blob digest or
    ref name the request named. ``attempt`` counts from 1 for every send of one call, so a
    retried call writes one record per attempt. ``status`` is the HTTP status that arrived,
    and is null when no answer did; ``code`` is the ``STORAGE.*`` failure the attempt ended
    with, recorded exactly when the attempt failed. ``bytes`` counts the blob bytes sent or
    received, so only a blob operation has any.
    """

    time: UtcTimestamp
    endpoint: Endpoint
    namespace: Namespace
    operation: VaultRequestOperation
    ref: AccessedName
    bytes: StorageCounter
    status: HttpStatus | None
    code: StorageCodeText | None
    attempt: VaultAttempt
    node_stable_id: NodeStableId | None

    @model_validator(mode="after")
    def _the_record_answers_one_attempt(self) -> Self:
        if (self.code is not None) != (self.status is None or self.status >= 400):
            raise ValueError(
                "a code is recorded exactly for an attempt that failed: one with no answer, "
                "or one answered with a status of 400 or more"
            )
        if self.bytes > 0 and self.operation not in {"put-blob", "get-blob"}:
            raise ValueError("only a blob operation sends or receives bytes")
        return self


STORAGE_SCHEMA_MODELS: dict[str, type[ContractModel]] = {
    "storage-blob-ref": BlobRef,
    "storage-ref-value": RefValue,
    "library-manifest-entry": ManifestEntry,
    "library-manifest": LibraryManifest,
    "library-manifest-envelope": ManifestEnvelope,
    "storage-binding": StorageBinding,
    "replication-state": ReplicationState,
    "replication-ledger-entry": LedgerEntry,
    "vault-server-config": VaultServerConfig,
    "vault-access-record": VaultAccessRecord,
    "vault-request-record": VaultRequestRecord,
}
