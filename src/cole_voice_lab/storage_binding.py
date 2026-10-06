"""Composition root: the one place that picks the implementation behind each storage port.

@canonical storage_binding -- every prompt and catalog store is opened through
open_catalog_reader or open_catalog_editor, and every vault through open_library_vault; no
other module (besides the adapters and the two native fixture seeders) imports an adapter.
The catalog openers are local-only and read no binding file: the catalog is the SQLite corpus
store under provenance/corpus. open_library_vault reads provenance/vault (the library
identity, and the named destination's binding and, for a tailnet vault, its approval, all
under provenance/vault/destinations/<destination>/, D-034), takes the library key and that
destination's vault token out of the keychain (keychain_client: the login Keychain on macOS,
the unit's systemd credentials on Linux), and returns the sealed BlobStore, the RefStore, the
DestinationContext and the request logger a library copy needs -- or None when the
destination is not bound ("Vault . off"). It refuses, in this order: a replication state with
no binding, an unreadable binding or one of an unknown backend, a local endpoint this Mac will
not write to and a missing key or token item (STORAGE.NOT_BOUND); a tailnet binding whose
approval is missing, changed, not yet effective, expired, for another library, endpoint or
namespace, or short of a class the binding copies (STORAGE.NOT_APPROVED). The identity of the
node answering is checked by the client at connect time (STORAGE.IDENTITY_MISMATCH).
Related: catalog_ports.py (CatalogReader, CatalogEditor), corpus_store_sqlite.py, storage_local
(local-directory vaults), storage_sealing.py (the key, the Keychain and the sealed store),
storage_http_client.py (the tailnet client), library_generations.py (what the vault is for),
tests/test_storage_import_boundary.py, tests/test_storage_binding.py.

Every adapter is imported inside a function, never at module level: recording_service and the
fine-tune job worker import this module to open a catalog, and must not pay for cryptography,
the vault client or the local store to do it (a test proves the closure). The Keychain, the
tailnet resolver and the vault connection are module-level seams so a test can replace them;
open_library_vault itself takes only a layout and a destination name, so nothing a command or
an environment variable can say reaches the client's loopback mode. Every function that reads
or writes a destination's records takes ``destination`` (default ``DEFAULT_VAULT_DESTINATION``,
the one destination of a host that copies to a single vault, as the Mac's recording service
does); the library identity and the library key are per library.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

from .canonical import (
    canonical_json_bytes,
    open_regular_no_follow,
    read_canonical_model,
    require_evidence_file,
    require_private_metadata,
    sha256_file,
    write_canonical_json,
    write_new_private_file,
)
from .catalog_ports import CatalogEditor, CatalogReader
from .corpus_store_sqlite import (
    CORPUS_DATABASE_NAME,
    CORPUS_DIRECTORY,
    CorpusStore,
    corpus_store_migration_resources,
)
from .errors import ContractError, SecurityError
from .models import utc_now
from .paths import AssetLayout, validate_absolute_path
from .storage_contracts import (
    DEFAULT_VAULT_DESTINATION,
    VAULT_TOKEN_PATTERN,
    FileClass,
    LibraryIdentity,
    StorageBackend,
    StorageBinding,
    VaultAccessRecord,
    VaultDestinationApproval,
    VaultRequestRecord,
    destination_records_path,
    keychain_item_names,
    new_library_id,
    validate_destination_name,
    validate_library_id,
)
from .storage_ports import BlobStore, RefStore, StorageError, storage_error

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from .library_generations import DestinationContext, VaultRecords
    from .storage_http_client import ExpectedIdentity, TailnetResolver, VaultConnection
    from .storage_sealing import KeychainClient

__all__ = [
    "APPROVALS_DIRECTORY",
    "AUDITED_OPERATIONS",
    "BINDING_NAME",
    "BindResult",
    "LIBRARY_IDENTITY_NAME",
    "LibraryKeyCreation",
    "LibraryVault",
    "REQUESTS_DIRECTORY",
    "UnexplainedAccess",
    "VAULT_TOKEN_ACCOUNTS",
    "VaultAuditReport",
    "VaultRequestLog",
    "VaultStores",
    "VaultTokenScope",
    "VaultTokens",
    "approval_path",
    "audit_vault_access",
    "bind_library",
    "binding_path",
    "catalog_store_build_resources",
    "catalog_store_location",
    "check_recovery_sheet_target",
    "create_library_vault_key",
    "create_vault_tokens",
    "import_library_vault_key",
    "import_vault_token",
    "initialize_library",
    "keychain_client",
    "vault_destinations",
    "library_identity_path",
    "library_key_fingerprint",
    "library_vault_binding",
    "open_catalog_editor",
    "open_catalog_reader",
    "open_library_vault",
    "open_vault_for_restore",
    "prepare_local_destination",
    "read_access_records",
    "read_destination_approval",
    "read_library_identity",
    "read_request_records",
    "read_storage_binding",
    "require_library_identity",
    "tailnet_resolver",
    "validate_local_endpoint",
    "vault_connection",
    "vault_token_present",
    "write_destination_approval",
    "write_library_recovery_sheet",
]

LIBRARY_IDENTITY_NAME: Final = "library.json"
BINDING_NAME: Final = "binding.json"
APPROVALS_DIRECTORY: Final = "approvals"
REQUESTS_DIRECTORY: Final = "requests"
# The library identity, the binding and one approval are each a few hundred bytes.
MAX_BINDING_RECORD_BYTES: Final = 1024 * 1024
# One day of request records; a push of the whole library writes a few hundred thousand.
MAX_REQUEST_LOG_BYTES: Final = 256 * 1024 * 1024
# The Keychain accounts the two vault tokens are kept under, beside the library key's.
VAULT_TOKEN_ACCOUNTS: Final[Mapping[str, str]] = {
    "write": "vault-write-token",
    "read": "vault-read-token",
}
# How a superseded replication state and its manifest copies are kept when a binding moves
# the library to another destination: renamed, never removed.
SUPERSEDED_INFIX: Final = "-superseded-"
MAX_SUPERSEDED: Final = 1000

type VaultTokenScope = Literal["write", "read"]

_TOKEN: Final = re.compile(VAULT_TOKEN_PATTERN)


# -- catalog ---------------------------------------------------------------------------------


def open_catalog_reader(layout: AssetLayout) -> CatalogReader:
    """Open the library's catalog for reading, as a context manager.

    Opening never creates a store, but it is not side-effect free: the SQLite store takes
    its lease and lock, creates its snapshot directory, discards crashed-publish scratch and
    migrates an empty schema-version-0 database.
    Params: ``layout`` is the validated asset root.
    Returns: an open ``CatalogReader``; ``with open_catalog_reader(layout) as catalog:``
    closes it.
    Raises: ``CorpusStoreError`` with ``CORPUS.STORE_MISSING`` when the library has no
    store, ``CORPUS.STORE_UNAVAILABLE`` when it cannot be opened, migrated or fails an
    integrity check; ``CorpusStoreSecurityError`` (``CORPUS.STORE_UNSAFE``) when a store
    directory or member is not owned and private, is a symlink, was replaced or is not part
    of the store; ``SecurityError`` for an unsafe root.
    """
    return CorpusStore(layout, create=False)


def open_catalog_editor(layout: AssetLayout, *, create: bool = False) -> CatalogEditor:
    """Open the library's catalog for reading and changing, as a context manager.

    Params: ``layout`` is the validated asset root; ``create`` allows an empty store to be
    created when the library has none (an existing store is opened either way).
    Returns: an open ``CatalogEditor``; ``with open_catalog_editor(layout) as catalog:``
    closes it.
    Raises: as ``open_catalog_reader`` (``CORPUS.STORE_MISSING`` only without ``create``).
    """
    return CorpusStore(layout, create=create)


def catalog_store_location(layout: AssetLayout) -> str:
    """Where the catalog of ``layout`` is kept, for operator output.

    Params: ``layout`` is the asset root the location is relative to.
    Returns: the store's database path relative to the asset root.
    Raises: nothing.
    """
    return f"provenance/{CORPUS_DIRECTORY}/{CORPUS_DATABASE_NAME}"


def catalog_store_build_resources() -> tuple[tuple[str, str, int], ...]:
    """Fingerprint the resources the catalog store applies, for a build identity.

    Returns: one ``("migrations/<name>", sha256 hex, byte length)`` entry per migration.
    Raises: as ``sqlite_migrations.migration_resources``.
    """
    return corpus_store_migration_resources()


# -- the Mac's vault records -------------------------------------------------------------------


def _records(layout: AssetLayout, destination: str) -> VaultRecords:
    from .library_generations import VaultRecords

    return VaultRecords(layout, destination)


def _library_directory(layout: AssetLayout) -> Path | None:
    """``provenance/vault`` itself (``library.json``), or ``None`` when it is missing."""
    from .library_generations import library_records_directory

    return library_records_directory(layout)


def _make_library_directory(layout: AssetLayout) -> Path:
    """``provenance/vault`` itself, made at 0700 where missing."""
    from .library_generations import make_library_records_directory

    return make_library_records_directory(layout)


def _vault_directory(layout: AssetLayout, destination: str, name: str | None = None) -> Path | None:
    """The destination's directory (or a child), or ``None`` when it is missing."""
    return _records(layout, destination).directory(name)


def _make_vault_directory(layout: AssetLayout, destination: str, name: str | None = None) -> Path:
    """The destination's directory (or a child), made at 0700 where missing."""
    return _records(layout, destination).make_directory(name)


def vault_destinations(layout: AssetLayout) -> tuple[str, ...]:
    """The destinations this host keeps records for (bound, approved or copied to), by name.

    Raises: ``StorageIntegrityError`` for an unsafe ``provenance/vault`` directory.
    """
    from .library_generations import recorded_destinations

    return recorded_destinations(layout)


def _not_bound(message: str) -> StorageError:
    return storage_error("STORAGE.NOT_BOUND", message)


def _not_approved(message: str) -> StorageError:
    return storage_error("STORAGE.NOT_APPROVED", message)


def _read_vault_record[ModelT: LibraryIdentity | StorageBinding | VaultDestinationApproval](
    path: Path, model: type[ModelT], label: str
) -> ModelT | None:
    """One canonical vault record, or ``None`` when the file is not there.

    Raises: ``StorageError`` (``STORAGE.NOT_BOUND``) when the file is unsafe, is not valid
    for ``model`` (an unknown backend included) or is not in canonical form.
    """
    try:
        return read_canonical_model(path, model, max_bytes=MAX_BINDING_RECORD_BYTES)
    except FileNotFoundError:
        return None
    except (ContractError, SecurityError) as error:
        raise _not_bound(f"{path} is not a usable {label}: {error}") from error


def library_identity_path(layout: AssetLayout) -> Path:
    """Where this Mac records which library it holds."""
    return layout.root / "provenance/vault" / LIBRARY_IDENTITY_NAME


def read_library_identity(layout: AssetLayout) -> LibraryIdentity | None:
    """The library this Mac holds, or ``None`` when ``vault init`` has not run.

    Raises: ``StorageError`` (``STORAGE.NOT_BOUND``) for an unsafe or invalid record;
    ``StorageIntegrityError`` for an unsafe ``provenance/vault`` directory.
    """
    directory = _library_directory(layout)
    if directory is None:
        return None
    return _read_vault_record(directory / LIBRARY_IDENTITY_NAME, LibraryIdentity, "library record")


def require_library_identity(layout: AssetLayout) -> LibraryIdentity:
    """The library this Mac holds.

    Raises: ``StorageError`` (``STORAGE.NOT_BOUND``) when there is none, or as
    ``read_library_identity``.
    """
    identity = read_library_identity(layout)
    if identity is None:
        raise _not_bound(
            f"{library_identity_path(layout)} does not exist; run `voice-lab vault init` first"
        )
    return identity


def initialize_library(
    layout: AssetLayout, *, library_id: str | None = None, clock: Callable[[], datetime] = utc_now
) -> tuple[Path, LibraryIdentity]:
    """Record which library this Mac holds; the record is written once and never replaced.

    Params: ``layout``; ``library_id``, the id of an existing vault library to adopt (a
    restore onto another Mac); a fresh ``lib-<yyyymmdd>-<12 hex>`` is made when it is
    omitted; ``clock``.
    Returns: the record's path and the record.
    Raises: ``StorageNameError`` for an invalid ``library_id``; ``StorageError``
    (``STORAGE.CONFLICT``) when this Mac already holds a library; ``OSError`` from the write.
    """
    created_at = clock()
    chosen = new_library_id(created_at) if library_id is None else validate_library_id(library_id)
    existing = read_library_identity(layout)
    if existing is not None:
        raise storage_error(
            "STORAGE.CONFLICT",
            f"this library is already {existing.library_id}; a library identity is never replaced",
        )
    identity = LibraryIdentity(library_id=chosen, created_at=created_at)
    path = _make_library_directory(layout) / LIBRARY_IDENTITY_NAME
    write_new_private_file(path, canonical_json_bytes(identity) + b"\n", mode=0o600)
    return path, identity


def binding_path(layout: AssetLayout, destination: str = DEFAULT_VAULT_DESTINATION) -> Path:
    """Where one destination's binding is kept."""
    return layout.root / destination_records_path(destination) / BINDING_NAME


def read_storage_binding(
    layout: AssetLayout, destination: str = DEFAULT_VAULT_DESTINATION
) -> StorageBinding | None:
    """One destination's binding, or ``None`` when that destination is not bound.

    Raises: ``StorageError`` (``STORAGE.NOT_BOUND``) for an unsafe, invalid or
    unknown-backend binding, or one that names another destination; ``StorageIntegrityError``
    for an unsafe directory; ``StorageNameError`` for a bad destination name.
    """
    directory = _vault_directory(layout, destination)
    if directory is None:
        return None
    binding = _read_vault_record(directory / BINDING_NAME, StorageBinding, "vault binding")
    if binding is not None and binding.destination != destination:
        raise _not_bound(
            f"{directory / BINDING_NAME} binds destination {binding.destination}, not "
            f"{destination}; a binding is never moved between destinations"
        )
    return binding


def approval_path(
    layout: AssetLayout, approval_id: str, destination: str = DEFAULT_VAULT_DESTINATION
) -> Path:
    """Where one destination approval is kept."""
    return (
        layout.root
        / destination_records_path(destination)
        / APPROVALS_DIRECTORY
        / f"{approval_id}.json"
    )


def read_destination_approval(
    layout: AssetLayout, approval_id: str, destination: str = DEFAULT_VAULT_DESTINATION
) -> tuple[VaultDestinationApproval, str]:
    """One destination approval and the digest of its file.

    Params: ``layout``; ``approval_id``, which must also be the id inside the record;
    ``destination``, whose approvals to read, which the record must also name.
    Returns: the approval and the SHA-256 of its bytes.
    Raises: ``StorageError`` (``STORAGE.NOT_APPROVED``) when there is no such approval, it
    cannot be read, or it names another approval id or destination.
    """
    path = approval_path(layout, approval_id, destination)
    try:
        approval = read_canonical_model(
            path, VaultDestinationApproval, max_bytes=MAX_BINDING_RECORD_BYTES
        )
        digest, _ = sha256_file(path)
    except FileNotFoundError as error:
        raise _not_approved(
            f"{path} does not exist; approve the destination with `voice-lab vault approve`"
        ) from error
    except (ContractError, SecurityError) as error:
        raise _not_approved(f"{path} is not a usable destination approval: {error}") from error
    if approval.approval_id != approval_id:
        raise _not_approved(f"{path} records approval {approval.approval_id}, not {approval_id}")
    if approval.destination != destination:
        raise _not_approved(
            f"{path} approves destination {approval.destination}, not {destination}"
        )
    return approval, digest


def write_destination_approval(
    layout: AssetLayout, approval: VaultDestinationApproval
) -> tuple[Path, str]:
    """Write one reviewed approval, read-only and once (0400), under its own destination.

    Params: ``layout``; ``approval``, which names the destination it is kept under.
    Returns: the path and the digest of the bytes written.
    Raises: ``StorageError`` (``STORAGE.CONFLICT``) when an approval of that id exists for
    that destination; ``OSError`` from the write.
    """
    directory = _make_vault_directory(layout, approval.destination, APPROVALS_DIRECTORY)
    path = directory / f"{approval.approval_id}.json"
    data = canonical_json_bytes(approval) + b"\n"
    try:
        write_new_private_file(path, data, mode=0o400)
    except FileExistsError as error:
        raise storage_error(
            "STORAGE.CONFLICT",
            f"{path} already exists; a destination approval is never replaced (approve a new "
            "one with another --approval-id)",
        ) from error
    return path, hashlib.sha256(data).hexdigest()


# -- the request log ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VaultRequestLog:
    """Appends one JSON line per vault request to the destination's ``requests/<date>.jsonl``.

    The file is opened append-only without following a link, at 0600, and refused when it is
    not a private regular file of this user. It holds no token: a ``VaultRequestRecord``
    records what was asked and how the attempt ended, nothing else.
    """

    layout: AssetLayout
    destination: str = DEFAULT_VAULT_DESTINATION

    def path(self, at: datetime) -> Path:
        """The file a record made at ``at`` is appended to."""
        return (
            self.layout.root
            / destination_records_path(self.destination)
            / REQUESTS_DIRECTORY
            / f"{at.astimezone(UTC).date().isoformat()}.jsonl"
        )

    def __call__(self, record: VaultRequestRecord) -> None:
        """Append ``record``.

        Raises: ``StorageIntegrityError`` (``STORAGE.INTEGRITY``) when the day's file is not
        a private regular file of this user; ``OSError`` from the write.
        """
        _make_vault_directory(self.layout, self.destination, REQUESTS_DIRECTORY)
        path = self.path(record.time)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
        descriptor = os.open(path, flags, 0o600)
        try:
            try:
                require_private_metadata(os.fstat(descriptor), str(path))
            except SecurityError as error:
                raise storage_error(
                    "STORAGE.INTEGRITY",
                    f"{path} is not a private regular file with one link",
                ) from error
            os.write(descriptor, canonical_json_bytes(record) + b"\n")
        finally:
            os.close(descriptor)


def read_request_records(
    layout: AssetLayout, destination: str = DEFAULT_VAULT_DESTINATION
) -> tuple[VaultRequestRecord, ...]:
    """Every request this host recorded for one destination, oldest day first.

    Raises: ``StorageError`` (``STORAGE.INTEGRITY``) when a line is not a request record;
    ``OSError`` from the read.
    """
    directory = _vault_directory(layout, destination, REQUESTS_DIRECTORY)
    if directory is None:
        return ()
    records: list[VaultRequestRecord] = []
    for path in sorted(directory.glob("*.jsonl")):
        require_evidence_file(path, max_bytes=MAX_REQUEST_LOG_BYTES)
        for number, line in enumerate(path.read_bytes().splitlines(), start=1):
            try:
                records.append(VaultRequestRecord.model_validate_json(line))
            except ValueError as error:
                raise storage_error(
                    "STORAGE.INTEGRITY", f"{path} line {number} is not a vault request record"
                ) from error
    return tuple(records)


# -- the seams a test replaces -----------------------------------------------------------------


def keychain_client() -> KeychainClient:
    """Where this host keeps the library key and the vault tokens.

    Returns: on macOS a ``SecurityCliKeychain`` over ``/usr/bin/security`` and the login
    Keychain; elsewhere (the PC, D-034) a ``SystemdCredentialKeychain`` over the
    ``$CREDENTIALS_DIRECTORY`` systemd gives the vault unit (or, once, the operator's private
    staging directory, to create the items before encrypting them).
    Raises: ``ValueError`` when the tool or keychain path is not usable; ``StorageError``
    (``STORAGE.PROTOCOL``) on Linux when no ``$CREDENTIALS_DIRECTORY`` is set.
    """
    if sys.platform == "darwin":
        from .storage_sealing import SecurityCliKeychain

        return SecurityCliKeychain()
    from .storage_sealing import SystemdCredentialKeychain

    return SystemdCredentialKeychain.from_environment()


def tailnet_resolver(tailnet_suffix: str) -> TailnetResolver:
    """The resolver that says which node answers for an endpoint on the tailnet.

    Params: ``tailnet_suffix``, the approved tailnet's MagicDNS suffix.
    Returns: a ``TailscaleCliResolver`` over the Tailscale app's binary.
    Raises: ``StorageNameError`` for a suffix that is not a DNS name.
    """
    from .storage_http_client import TailscaleCliResolver

    return TailscaleCliResolver(tailnet_suffix=tailnet_suffix)


def vault_connection(
    *,
    endpoint: str,
    namespace: str,
    token: Callable[[], str],
    resolver: TailnetResolver,
    identity: ExpectedIdentity,
    request_log: Callable[[VaultRequestRecord], None],
) -> VaultConnection:
    """The client that speaks to one vault namespace over the tailnet.

    It always verifies TLS for ``endpoint`` and always checks the node's identity: the
    client's loopback mode takes another constructor, which nothing here can reach.
    Params: the endpoint, namespace, token callable, resolver, expected identity and request
    logger ``open_library_vault`` built.
    Returns: the connection.
    Raises: ``StorageNameError`` for an endpoint or namespace that is not one.
    """
    from .storage_http_client import VaultConnection

    return VaultConnection(
        endpoint,
        namespace,
        token,
        resolver,
        identity,
        request_log=request_log,
    )


# -- the library key and the vault tokens ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LibraryKeyCreation:
    """A library key that was just created, with the recovery sheet that can restore it.

    ``sheet`` carries the key itself: it goes to a new 0600 file or to the operator's own
    terminal and nowhere else, so it is kept out of every repr.
    """

    library_id: str
    fingerprint: str
    sheet: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class VaultTokens:
    """The hashes of one destination's two fresh vault tokens; the tokens stay in the keychain."""

    library_id: str
    destination: str
    namespace: str
    write_token_sha256: str
    read_token_sha256: str


def _token_item(library_id: str, destination: str, scope: VaultTokenScope) -> tuple[str, str]:
    names = keychain_item_names(library_id, destination)
    return (names.token if scope == "write" else names.read_token, VAULT_TOKEN_ACCOUNTS[scope])


def create_library_vault_key(
    layout: AssetLayout, *, clock: Callable[[], datetime] = utc_now
) -> LibraryKeyCreation:
    """Create this library's key in the keychain and render its recovery sheet.

    Params: ``layout``; ``clock``, the time the sheet records.
    Returns: the ``LibraryKeyCreation``; write or print its sheet, which is the only copy of
    the key outside the Keychain.
    Raises: ``StorageError`` (``STORAGE.NOT_BOUND``) without a library record,
    (``STORAGE.CONFLICT``) when the library already has a key, or (``STORAGE.PROTOCOL``) when
    the Keychain failed.
    """
    from .storage_sealing import create_library_key, render_recovery_sheet

    identity = require_library_identity(layout)
    key = create_library_key(keychain_client(), identity.library_id)
    return LibraryKeyCreation(
        library_id=identity.library_id,
        fingerprint=key.fingerprint(),
        sheet=render_recovery_sheet(identity.library_id, key, clock()),
    )


def import_library_vault_key(layout: AssetLayout, sheet_text: str) -> str:
    """Restore this library's key from its recovery sheet into the login Keychain.

    Params: ``layout``; ``sheet_text``, the whole sheet.
    Returns: the key's fingerprint.
    Raises: ``StorageError`` (``STORAGE.NOT_BOUND``) without a library record,
    (``STORAGE.INTEGRITY``) for a sheet that is not this library's or does not check out, or
    (``STORAGE.CONFLICT``) when the library already has a key.
    """
    from .storage_sealing import import_library_key

    identity = require_library_identity(layout)
    return import_library_key(keychain_client(), identity.library_id, sheet_text).fingerprint()


def library_key_fingerprint(layout: AssetLayout) -> str | None:
    """This library's key fingerprint, or ``None`` when the Keychain holds no key for it.

    Raises: ``StorageError`` (``STORAGE.NOT_BOUND``) without a library record,
    (``STORAGE.INTEGRITY``) when the item does not hold a key, or (``STORAGE.PROTOCOL``).
    """
    from .storage_sealing import load_library_key

    identity = require_library_identity(layout)
    try:
        return load_library_key(keychain_client(), identity.library_id).fingerprint()
    except StorageError as error:
        if error.code != "STORAGE.NOT_BOUND":
            raise
        return None


def check_recovery_sheet_target(layout: AssetLayout, path: Path) -> Path:
    """Refuse a recovery-sheet path before a key that only it could restore exists.

    The sheet holds the library key, so it never goes into the library or the repository.
    ``write_library_recovery_sheet`` checks this again (and more) as it writes; this is the
    check a command can make first, so a refused path never leaves a key with no sheet.
    Params: ``layout``; ``path``, an absolute path.
    Returns: ``path``.
    Raises: ``StorageError`` (``STORAGE.NOT_APPROVED``) for a path inside the library or the
    repository.
    """
    for root, name in ((layout.root, "library"), (_repository_root(), "repository")):
        if path == root or path.is_relative_to(root):
            raise storage_error(
                "STORAGE.NOT_APPROVED",
                f"a recovery sheet holds the library key, so it is never written inside the "
                f"{name}: {path}",
            )
    return path


def write_library_recovery_sheet(layout: AssetLayout, path: Path, text: str) -> Path:
    """Write a recovery sheet to a new 0600 file outside the library and the repository.

    Params: ``layout``; ``path``, the new file; ``text``, the sheet.
    Returns: the path written.
    Raises: as ``storage_sealing.write_recovery_sheet`` (``StorageError`` or
    ``SecurityError`` for a path inside a forbidden root, an existing file, or an unsafe
    parent).
    """
    from .storage_sealing import write_recovery_sheet

    write_recovery_sheet(path, text, forbidden_roots=(layout.root, _repository_root()))
    return path


def _require_token(text: str) -> tuple[str, str]:
    """One vault token as it arrived, stripped, with its SHA-256.

    Raises: ``StorageError`` (``STORAGE.PROTOCOL``) when ``text`` is not exactly one vault
    token; no message repeats it.
    """
    from .storage_server import hash_token

    token = text.strip()
    return token, hash_token(token)


def create_vault_tokens(
    layout: AssetLayout, namespace: str, *, destination: str = DEFAULT_VAULT_DESTINATION
) -> VaultTokens:
    """Create one destination's write and read vault tokens in the keychain.

    Neither token is returned, printed or logged: only the SHA-256 the vault server's
    configuration stores. Each destination has its own pair, so two vault servers never
    hold the hash of one token.
    Params: ``layout``; ``namespace``, the vault namespace the tokens are for;
    ``destination``, whose items they become.
    Returns: the ``VaultTokens`` hashes.
    Raises: ``StorageError`` (``STORAGE.NOT_BOUND``) without a library record,
    (``STORAGE.CONFLICT``) when either item exists (neither is created then), or
    (``STORAGE.PROTOCOL``) when the Keychain failed. A failure after the first item was
    stored names that item and its SHA-256, so the one token that exists is never lost.
    """
    from .storage_server import new_token

    identity = require_library_identity(layout)
    keychain = keychain_client()
    scopes: tuple[VaultTokenScope, ...] = ("write", "read")
    items = {scope: _token_item(identity.library_id, destination, scope) for scope in scopes}
    for scope, (service, account) in items.items():
        if keychain.has_generic_password(service=service, account=account):
            raise storage_error(
                "STORAGE.CONFLICT",
                f"the {scope} token item {service} already exists; a vault token is never replaced",
            )
    # Both tokens are generated and hashed before either is stored: a Keychain that fails on
    # the second add would otherwise strand the first with its hash never returned, and no
    # command can replace or print it afterwards.
    tokens = {scope: new_token() for scope in scopes}
    hashes = {
        scope: hashlib.sha256(token.encode("ascii")).hexdigest() for scope, token in tokens.items()
    }
    stored: list[VaultTokenScope] = []
    for scope, (service, account) in items.items():
        try:
            keychain.add_generic_password(
                service=service, account=account, label=service, secret=tokens[scope]
            )
        except StorageError as error:
            if not stored:
                raise
            kept = stored[0]
            raise storage_error(
                error.code,
                f"{error}; the {kept} token item {items[kept][0]} was stored first and is still "
                f"there, and its SHA-256 is {hashes[kept]}: either configure the vault with that "
                f"hash, or delete the item (on macOS `security delete-generic-password -s "
                f"{items[kept][0]}`, on Linux its file in the credential staging directory) and "
                "run `voice-lab vault token create` again",
            ) from error
        stored.append(scope)
    return VaultTokens(
        library_id=identity.library_id,
        destination=destination,
        namespace=namespace,
        write_token_sha256=hashes["write"],
        read_token_sha256=hashes["read"],
    )


def import_vault_token(
    layout: AssetLayout,
    scope: VaultTokenScope,
    text: str,
    *,
    destination: str = DEFAULT_VAULT_DESTINATION,
) -> str:
    """Keep one vault token this host was given in the keychain, as a destination's item.

    Params: ``layout``; ``scope``, ``write`` or ``read``; ``text``, the token as it arrived
    on standard input; ``destination``, whose token it is.
    Returns: the token's SHA-256, which is what the server's configuration holds.
    Raises: ``StorageError`` (``STORAGE.NOT_BOUND``) without a library record,
    (``STORAGE.PROTOCOL``) when ``text`` is not one vault token, or (``STORAGE.CONFLICT``)
    when the item exists.
    """
    identity = require_library_identity(layout)
    token, digest = _require_token(text)
    service, account = _token_item(identity.library_id, destination, scope)
    keychain_client().add_generic_password(
        service=service, account=account, label=service, secret=token
    )
    return digest


def vault_token_present(
    layout: AssetLayout, scope: VaultTokenScope, *, destination: str = DEFAULT_VAULT_DESTINATION
) -> bool:
    """Whether the keychain holds one destination's token of ``scope``.

    Raises: ``StorageError`` (``STORAGE.NOT_BOUND``) without a library record, or
    (``STORAGE.PROTOCOL``) when the keychain could not be asked.
    """
    identity = require_library_identity(layout)
    service, account = _token_item(identity.library_id, destination, scope)
    return keychain_client().has_generic_password(service=service, account=account)


def _vault_token(library_id: str, destination: str, scope: VaultTokenScope) -> Callable[[], str]:
    """A callable that reads the token out of the keychain for one request, then drops it."""
    service, account = _token_item(library_id, destination, scope)

    def token() -> str:
        secret = keychain_client().find_generic_password(service=service, account=account).strip()
        if _TOKEN.fullmatch(secret) is None:
            raise storage_error(
                "STORAGE.INTEGRITY",
                f"the item {service} does not hold a vault token",
            )
        return secret

    return token


# -- the local-directory endpoint --------------------------------------------------------------


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def validate_local_endpoint(layout: AssetLayout, endpoint: str) -> Path:
    """Return the directory a ``local-directory`` vault may use.

    A local vault is a private directory on another disk: absolute, normalized, at least
    four components deep (so ``/`` and ``/Volumes`` are refused), not this user's home, not
    inside the library and not holding it, and nowhere in the repository.
    Params: ``layout``, the library this endpoint would serve; ``endpoint``.
    Returns: the endpoint as a ``Path``.
    Raises: ``StorageError`` (``STORAGE.NOT_BOUND``) naming the rule that refused it.
    """

    def refuse(problem: str) -> StorageError:
        return _not_bound(f"the local-directory endpoint {endpoint!r:.200} {problem}")

    try:
        validate_absolute_path(endpoint)
    except ValueError as error:
        raise refuse("is not an absolute, normalized path") from error
    path = Path(endpoint)
    if path in (Path.home(), Path.home().resolve()):
        raise refuse("is this user's home directory")
    if len(path.parts) < 4:
        raise refuse("is not specific enough (it needs at least three directories)")
    for other, relation in ((layout.root, "the library"), (_repository_root(), "the repository")):
        if path == other or path.is_relative_to(other) or other.is_relative_to(path):
            raise refuse(f"is {relation}, or holds it, or is inside it")
    return path


def prepare_local_destination(layout: AssetLayout, endpoint: str) -> Path:
    """Make (or check) the private directory a ``local-directory`` vault stores blobs in.

    Params: ``layout``; ``endpoint``, checked by ``validate_local_endpoint`` first.
    Returns: the directory.
    Raises: as ``validate_local_endpoint``; ``StorageError`` from the local store when the
    directory (or its parent) cannot be used.
    """
    from .storage_local import LocalBlobStore, LocalRefStore

    path = validate_local_endpoint(layout, endpoint)
    LocalBlobStore(path, create=True)
    LocalRefStore(path, create=True)
    return path


# -- binding -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BindResult:
    """A binding that was just written, and what writing it moved aside."""

    path: Path
    binding: StorageBinding
    replaced: bool
    superseded_state: Path | None
    superseded_manifests: Path | None


def _superseded(path: Path) -> Path:
    """The first free ``<stem>-superseded-<n><suffix>`` beside ``path``."""
    for number in range(1, MAX_SUPERSEDED + 1):
        candidate = path.with_name(f"{path.stem}{SUPERSEDED_INFIX}{number}{path.suffix}")
        if not os.path.lexists(candidate):
            return candidate
    raise storage_error(
        "STORAGE.QUOTA", f"{path} has already been superseded {MAX_SUPERSEDED} times"
    )


def _reset_state(layout: AssetLayout, destination: str) -> tuple[Path | None, Path | None]:
    """Move a destination's ``state.json`` and kept manifests aside; nothing is removed."""
    from .library_generations import MANIFEST_COPIES_DIRECTORY, STATE_NAME

    directory = _make_vault_directory(layout, destination)
    moved: list[Path | None] = []
    for name in (STATE_NAME, MANIFEST_COPIES_DIRECTORY):
        source = directory / name
        if not os.path.lexists(source):
            moved.append(None)
            continue
        target = _superseded(source)
        os.rename(source, target)
        moved.append(target)
    return moved[0], moved[1]


def bind_library(
    layout: AssetLayout,
    *,
    backend: StorageBackend,
    endpoint: str,
    namespace: str,
    classes: Sequence[FileClass],
    approval_id: str | None = None,
    approval_sha256: str | None = None,
    replace: bool = False,
    reset_state: bool = False,
    destination: str = DEFAULT_VAULT_DESTINATION,
    clock: Callable[[], datetime] = utc_now,
) -> BindResult:
    """Write one destination's ``binding.json``: where that destination is, and what it copies.

    The binding is ``provenance/vault/destinations/<destination>/binding.json``; every other
    destination's binding, state and chain are left alone.

    A ``tailnet-vault`` binding needs an existing, effective approval of ``approval_id``
    whose file has ``approval_sha256`` and whose library, endpoint, namespace and classes
    cover the binding. A ``local-directory`` binding needs an endpoint this Mac will write to
    (``validate_local_endpoint``), and its store is created. An existing binding is replaced
    only with ``replace``; when a replication state exists and the new binding names another
    destination, ``reset_state`` must also move ``state.json`` and the kept manifests aside.
    Params: ``layout``; ``backend``; ``endpoint``; ``namespace``; ``classes``, the file
    classes copied there; ``approval_id`` and ``approval_sha256`` (tailnet only; an approval
    kept for this ``destination``); ``replace``; ``reset_state``; ``destination``;
    ``clock``.
    Returns: the ``BindResult``.
    Raises: ``StorageError`` (``STORAGE.NOT_BOUND``) without a library record or for a local
    endpoint this Mac refuses, (``STORAGE.NOT_APPROVED``) for a missing, changed or
    unsuitable approval, (``STORAGE.CONFLICT``) when a binding exists without ``replace`` or
    a state must be reset without ``reset_state``; ``PushRunningError``
    (``REPLICATION.PUSH_RUNNING``) while a push of this library holds ``.push.lock``;
    ``pydantic.ValidationError`` for fields that break a ``StorageBinding`` rule.
    """
    from .library_generations import DestinationContext

    validate_destination_name(destination)
    identity = require_library_identity(layout)
    names = keychain_item_names(identity.library_id, destination)
    tailnet = backend == "tailnet-vault"
    existing = read_storage_binding(layout, destination)
    if existing is not None and not replace:
        raise storage_error(
            "STORAGE.CONFLICT",
            f"{binding_path(layout, destination)} already binds destination {destination} to "
            f"{existing.backend} {existing.endpoint} (namespace {existing.namespace}); pass "
            "--replace to change it",
        )
    approval: VaultDestinationApproval | None = None
    if tailnet:
        if approval_id is None or approval_sha256 is None:
            raise _not_approved(
                "a tailnet-vault binding needs --approval-id and --approval-sha256 of a "
                "reviewed destination approval"
            )
        approval = _approval_for(
            layout, identity.library_id, approval_id, approval_sha256, clock(), destination
        )
    binding = StorageBinding(
        destination=destination,
        backend=backend,
        endpoint=endpoint,
        namespace=namespace,
        library_id=identity.library_id,
        classes=tuple(sorted(set(classes))),
        approval_id=approval_id if tailnet else None,
        approval_sha256=approval_sha256 if tailnet else None,
        key_item=names.key,
        token_item=names.token if tailnet else None,
        created_at=clock(),
    )
    if approval is None:
        prepare_local_destination(layout, endpoint)
    else:
        _require_approval_covers(approval, binding)
    context = DestinationContext(
        backend=binding.backend,
        endpoint=binding.endpoint,
        namespace=binding.namespace,
        node_stable_id=None if approval is None else approval.node_stable_id,
        tag=None if approval is None else approval.tag,
        approval_sha256=None if approval is None else binding.approval_sha256,
        name=destination,
    )
    moved: tuple[Path | None, Path | None] = (None, None)
    # ``state.json`` and the kept manifests are a push's records: reading them, moving them
    # aside and writing the binding beside them happens under the same lock a push holds, so
    # a rebind during a push is a named refusal rather than a reset the push quietly undoes.
    with _records(layout, destination).push_lock():
        recorded = _state_destination(layout, destination, identity.library_id)
        if recorded is not None and recorded != context.digest:
            if not reset_state:
                raise storage_error(
                    "STORAGE.CONFLICT",
                    f"destination {destination} of this library has already been copied to "
                    "another vault; pass --reset-state as well to set that record aside and "
                    "start at generation 1",
                )
            moved = _reset_state(layout, destination)
        write_canonical_json(
            _make_vault_directory(layout, destination) / BINDING_NAME, binding, mode=0o600
        )
    return BindResult(
        path=binding_path(layout, destination),
        binding=binding,
        replaced=existing is not None,
        superseded_state=moved[0],
        superseded_manifests=moved[1],
    )


def _state_destination(layout: AssetLayout, destination: str, library_id: str) -> str | None:
    """The vault digest a destination's ``state.json`` records, or ``None`` without one."""
    state = _records(layout, destination).read_state(library_id)
    return None if state is None else str(state.destination_sha256)


def _approval_for(
    layout: AssetLayout,
    library_id: str,
    approval_id: str,
    approval_sha256: str,
    now: datetime,
    destination: str,
) -> VaultDestinationApproval:
    """The approval named by id and digest among ``destination``'s, effective now.

    Raises: ``StorageError`` (``STORAGE.NOT_APPROVED``) naming what refused it.
    """
    approval, digest = read_destination_approval(layout, approval_id, destination)
    path = approval_path(layout, approval_id, destination)
    if digest != approval_sha256:
        raise _not_approved(
            f"{path} is {digest}, not the reviewed {approval_sha256}; the approval changed"
        )
    if approval.library_id != library_id:
        raise _not_approved(f"{path} approves library {approval.library_id}, not {library_id}")
    if not approval.effective_at(now):
        when = "not yet effective" if now.astimezone(UTC) < approval.approved_at else "expired"
        raise _not_approved(
            f"{path} is {when} (approved {approval.approved_at.isoformat()}, expires "
            f"{approval.expires_at.isoformat()})"
        )
    return approval


def _require_approval_covers(approval: VaultDestinationApproval, binding: StorageBinding) -> None:
    """The approval must cover the binding's destination, endpoint, namespace and classes.

    Raises: ``StorageError`` (``STORAGE.NOT_APPROVED``) naming the first difference.
    """
    if approval.destination != binding.destination:
        raise _not_approved(
            f"approval {approval.approval_id} approves destination {approval.destination}, not "
            f"{binding.destination}"
        )
    if approval.endpoint != binding.endpoint:
        raise _not_approved(
            f"approval {approval.approval_id} approves {approval.endpoint}, not {binding.endpoint}"
        )
    if approval.namespace != binding.namespace:
        raise _not_approved(
            f"approval {approval.approval_id} approves namespace {approval.namespace}, not "
            f"{binding.namespace}"
        )
    uncovered = tuple(sorted(set(binding.classes) - set(approval.classes)))
    if uncovered:
        raise _not_approved(
            f"approval {approval.approval_id} does not cover {', '.join(uncovered)}; approve a "
            "destination that covers every class the binding copies"
        )


# -- the vault ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LibraryVault:
    """One library's vault, with every adapter already chosen and checked.

    ``blob_store`` is always sealed with the library key, so nothing a binding can say turns
    encryption off; ``chunk_bytes`` is the largest plaintext that store takes.
    ``destination`` is what a ledger entry records, and ``request_log`` is where the client
    writes one record per attempt (``None`` for a local directory, which sends no request).
    """

    binding: StorageBinding
    approval: VaultDestinationApproval | None
    destination: DestinationContext
    stores: VaultStores

    @property
    def classes(self) -> tuple[FileClass, ...]:
        """The file classes this binding copies."""
        return self.binding.classes

    @property
    def library_id(self) -> str:
        """The library this vault holds."""
        return self.binding.library_id

    @property
    def blob_store(self) -> BlobStore:
        """The sealed blob store; nothing a binding says can turn encryption off."""
        return self.stores.blob_store

    @property
    def ref_store(self) -> RefStore:
        """The ref store this library's head lives in."""
        return self.stores.ref_store

    @property
    def chunk_bytes(self) -> int:
        """The largest plaintext the sealed store takes."""
        return self.stores.chunk_bytes

    @property
    def key_fingerprint(self) -> str:
        """The fingerprint of the library key that seals every blob."""
        return self.stores.key_fingerprint

    @property
    def request_log(self) -> VaultRequestLog | None:
        """Where the client records each attempt (``None`` for a local directory)."""
        return self.stores.request_log


def library_vault_binding(
    layout: AssetLayout, destination: str = DEFAULT_VAULT_DESTINATION
) -> StorageBinding | None:
    """Where one destination is, or ``None`` when it is bound to no vault ("Vault · off").

    These are ``open_library_vault``'s first two checks on their own, and they build no
    adapter, read no Keychain item and reach no vault: the recording service's supervisor
    asks this much to learn whether to copy at all, and its worker process opens the vault.
    Params: ``layout``, the validated asset root; ``destination``.
    Returns: the binding, or ``None`` when there is none and nothing was ever copied.
    Raises: ``StorageError`` (``STORAGE.NOT_BOUND``) when a replication state records copies
    of this library without a binding, or the binding is unreadable, invalid or of an unknown
    backend; ``StorageIntegrityError`` for an unsafe ``provenance/vault`` directory.
    """
    binding = read_storage_binding(layout, destination)
    if binding is None and _state_present(layout, destination):
        raise _not_bound(
            f"{layout.root}/{destination_records_path(destination)}/state.json records copies "
            f"of this library, but {binding_path(layout, destination)} is gone; bind the vault "
            "again with `voice-lab vault bind` before the library is copied anywhere"
        )
    return binding


def open_library_vault(
    layout: AssetLayout, destination: str = DEFAULT_VAULT_DESTINATION
) -> LibraryVault | None:
    """Open one destination's vault, or report that it is not bound to one.

    Params: ``layout``, the validated asset root; ``destination``, whose binding, approval,
    token and records to use (the Mac's recording service uses the default). Nothing else is
    taken: no argument, flag or environment variable chooses a backend, a token or a way of
    connecting.
    Returns: the ``LibraryVault``, or ``None`` when there is no binding ("Vault . off").
    Raises: ``StorageError`` (``STORAGE.NOT_BOUND``) when a replication state exists without
    a binding, the binding is unreadable or of an unknown backend, there is no library
    record or it names another library, a local endpoint is one this Mac refuses, or the
    Keychain holds no library key or vault token; (``STORAGE.NOT_APPROVED``) when a tailnet
    binding's approval is missing, changed, not yet effective, expired, for another library,
    endpoint or namespace, or short of a class the binding copies;
    ``StorageIntegrityError`` for an unsafe ``provenance/vault`` record. The node's identity
    is checked by the client on the first request (``STORAGE.IDENTITY_MISMATCH``).
    """
    from .library_generations import DestinationContext
    from .storage_http_client import ExpectedIdentity

    binding = library_vault_binding(layout, destination)
    if binding is None:
        return None
    identity = require_library_identity(layout)
    if binding.library_id != identity.library_id:
        raise _not_bound(
            f"{binding_path(layout, destination)} binds library {binding.library_id}, but this "
            f"host holds {identity.library_id}"
        )
    approval: VaultDestinationApproval | None = None
    # A binding has both an approval id and its digest exactly when it is a tailnet vault.
    if binding.approval_id is not None and binding.approval_sha256 is not None:
        approval = _approval_for(
            layout,
            binding.library_id,
            binding.approval_id,
            binding.approval_sha256,
            utc_now(),
            destination,
        )
        _require_approval_covers(approval, binding)
    expected = (
        None
        if approval is None
        else ExpectedIdentity(
            node_stable_id=approval.node_stable_id,
            tag=approval.tag,
            tailnet_suffix=approval.tailnet_suffix,
        )
    )
    stores = _open_stores(
        layout,
        endpoint=binding.endpoint,
        namespace=binding.namespace,
        library_id=binding.library_id,
        identity=expected,
        scope="write",
        destination=destination,
    )
    return LibraryVault(
        binding=binding,
        approval=approval,
        destination=DestinationContext(
            backend=binding.backend,
            endpoint=binding.endpoint,
            namespace=binding.namespace,
            node_stable_id=None if approval is None else approval.node_stable_id,
            tag=None if approval is None else approval.tag,
            approval_sha256=None if approval is None else binding.approval_sha256,
            name=destination,
        ),
        stores=stores,
    )


def _state_present(layout: AssetLayout, destination: str) -> bool:
    from .library_generations import STATE_NAME

    directory = _vault_directory(layout, destination)
    return directory is not None and os.path.lexists(directory / STATE_NAME)


@dataclass(frozen=True, slots=True)
class VaultStores:
    """The two ports of one vault namespace, with the sealed blob store already wrapped."""

    library_id: str
    blob_store: BlobStore
    ref_store: RefStore
    chunk_bytes: int
    key_fingerprint: str
    request_log: VaultRequestLog | None


def _open_stores(
    layout: AssetLayout,
    *,
    endpoint: str,
    namespace: str,
    library_id: str,
    identity: ExpectedIdentity | None,
    scope: VaultTokenScope,
    destination: str,
) -> VaultStores:
    """Build the sealed blob store and the ref store of one vault, whatever it runs on.

    ``identity`` decides which: the approved node of a ``tailnet-vault``, or ``None`` for a
    ``local-directory``, whose endpoint must then be a directory this Mac will write to (a DNS
    name is not one, so a tailnet destination with no identity is refused rather than opened
    locally).
    Raises: ``StorageError`` (``STORAGE.NOT_BOUND``) for a local endpoint this host refuses
    or a missing library key or vault token item (the token is ``destination``'s);
    whatever the local store raises.
    """
    from .storage_sealing import MAX_SEALED_PLAINTEXT_BYTES, SealedBlobStore, load_library_key

    key = load_library_key(keychain_client(), library_id)
    request_log: VaultRequestLog | None = None
    if identity is None:
        from .storage_local import LocalBlobStore, LocalRefStore

        path = validate_local_endpoint(layout, endpoint)
        inner: BlobStore = LocalBlobStore(path)
        refs: RefStore = LocalRefStore(path)
    else:
        service, account = _token_item(library_id, destination, scope)
        if not keychain_client().has_generic_password(service=service, account=account):
            raise _not_bound(
                f"there is no {scope} vault token item {service} for destination {destination} "
                f"of library {library_id}; create the tokens with `voice-lab vault token "
                "create`, or import the one the vault already knows with `voice-lab vault "
                "token import`"
            )
        request_log = VaultRequestLog(layout, destination)
        connection = vault_connection(
            endpoint=endpoint,
            namespace=namespace,
            token=_vault_token(library_id, destination, scope),
            resolver=tailnet_resolver(identity.tailnet_suffix),
            identity=identity,
            request_log=request_log,
        )
        inner = connection.blob_store()
        refs = connection.ref_store()
    return VaultStores(
        library_id=library_id,
        blob_store=SealedBlobStore(inner, key=key, library_id=library_id),
        ref_store=refs,
        chunk_bytes=MAX_SEALED_PLAINTEXT_BYTES,
        key_fingerprint=key.fingerprint(),
        request_log=request_log,
    )


def open_vault_for_restore(
    layout: AssetLayout,
    *,
    backend: StorageBackend,
    endpoint: str,
    namespace: str | None = None,
    node_stable_id: str | None = None,
    tailnet_suffix: str | None = None,
    destination: str = DEFAULT_VAULT_DESTINATION,
) -> VaultStores:
    """Open a vault by hand, to restore a library onto a host that has no binding yet.

    There is no approval here: this Mac has just been told the library id (``vault init
    --library-id``), the key (``vault key import``) and the read token (``vault token import
    --scope read``), and it reads what the vault holds with them. Every byte is still proven
    by the manifest the head names, so the vault is trusted for nothing.
    Params: ``layout``; ``backend``; ``endpoint``; ``namespace``, required for
    ``tailnet-vault`` (a ``local-directory`` endpoint is its own namespace and ignores it);
    ``node_stable_id`` and ``tailnet_suffix``, required for ``tailnet-vault`` and refused for
    ``local-directory``; ``destination``, whose read token item to use (and whose request
    log records the requests).
    Returns: the ``VaultStores``, whose token is the read one.
    Raises: ``StorageError`` (``STORAGE.NOT_BOUND``) without a library record, for a local
    endpoint this Mac refuses, or for a missing key or read-token item;
    (``STORAGE.PROTOCOL``) when the namespace or the node arguments do not match the backend;
    ``StorageNameError`` for an endpoint, namespace, node id or suffix that is not one.
    """
    from .storage_contracts import VAULT_NODE_TAG
    from .storage_http_client import ExpectedIdentity

    library_id = require_library_identity(layout).library_id
    tailnet = backend == "tailnet-vault"
    if tailnet != (node_stable_id is not None and tailnet_suffix is not None):
        raise storage_error(
            "STORAGE.PROTOCOL",
            "a tailnet-vault restore needs --node-stable-id and --tailnet-suffix, and a "
            "local-directory restore takes neither",
        )
    # Without this the namespace would reach the client as ``None`` and break it there, after
    # the Keychain had already been read; a missing name is an operator's mistake, not a fault.
    if tailnet and namespace is None:
        raise storage_error(
            "STORAGE.PROTOCOL",
            "a tailnet-vault restore needs --namespace: it names the vault's namespace for "
            "this library",
        )
    identity = (
        None
        if node_stable_id is None or tailnet_suffix is None
        else ExpectedIdentity(
            node_stable_id=node_stable_id, tag=VAULT_NODE_TAG, tailnet_suffix=tailnet_suffix
        )
    )
    return _open_stores(
        layout,
        endpoint=endpoint,
        namespace="" if namespace is None else namespace,
        library_id=library_id,
        identity=identity,
        scope="read",
        destination=destination,
    )


# -- auditing ----------------------------------------------------------------------------------


# What the Mac's client asks of a vault; every other access record is the server's own.
AUDITED_OPERATIONS: Final = ("put-blob", "has-blob", "get-blob", "read-ref", "swap-ref")
# One copied access log: far above a year of this Mac's own pushes.
MAX_ACCESS_LOG_BYTES: Final = 256 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class UnexplainedAccess:
    """One access the vault recorded that this Mac's request log does not account for."""

    time: datetime
    operation: str
    ref: str | None
    status: int
    bytes: int
    scope: str
    user_login: str | None


@dataclass(frozen=True, slots=True)
class VaultAuditReport:
    """What the vault's access log holds for one namespace, against this Mac's requests."""

    namespace: str
    server_accesses: int
    mac_requests: int
    matched: int
    unexplained: tuple[UnexplainedAccess, ...]
    unanswered: int
    refused_at_server: int

    @property
    def exit_status(self) -> int:
        """0 when every read and write the vault recorded is one this Mac sent, else 1."""
        return 1 if self.unexplained else 0


def read_access_records(path: Path) -> tuple[VaultAccessRecord, ...]:
    """Every line of a vault access log copied from the server.

    Params: ``path``, the copied log.
    Returns: the records, in the order they were written.
    Raises: ``SecurityError`` for an unsafe file; ``ContractError`` when it is too large;
    ``StorageError`` (``STORAGE.INTEGRITY``) when a line is not an access record.
    """
    require_evidence_file(path, max_bytes=MAX_ACCESS_LOG_BYTES)
    records: list[VaultAccessRecord] = []
    with open_regular_no_follow(path) as stream:
        data = stream.read(MAX_ACCESS_LOG_BYTES + 1)
    for number, line in enumerate(data.splitlines(), start=1):
        try:
            records.append(VaultAccessRecord.model_validate_json(line))
        except ValueError as error:
            raise storage_error(
                "STORAGE.INTEGRITY", f"{path} line {number} is not a vault access record"
            ) from error
    return tuple(records)


def _expected_server_bytes(request: VaultRequestRecord) -> int | None:
    """What the vault records for an attempt this Mac made, or ``None`` when it varies.

    Every accepted operation counts the same bytes on both sides. A refusal does not: the
    vault counts none of a download it never sent, none of an upload it turned away before
    reading a byte (an unauthorized one), but all of an upload whose body it had already read
    when it found the rule that was broken (a full namespace, a digest that did not match, a
    store that failed). So a refused attempt is paired on its operation, ref and status, and
    its byte count is not compared on either side.
    """
    return None if request.status is None or request.status >= 400 else request.bytes


def _served_bytes(record: VaultAccessRecord) -> int | None:
    """The byte count an access is paired on; ``None`` for a refusal (see above)."""
    return None if record.status >= 400 else record.bytes


def audit_vault_access(
    accesses: Sequence[VaultAccessRecord], requests: Sequence[VaultRequestRecord], namespace: str
) -> VaultAuditReport:
    """Hold a vault's access log for one namespace against this Mac's request log.

    Every read and write the vault answered must be one this Mac sent: attempts are matched
    by operation, ref, status and bytes (with the refusal rule above), and an attempt the Mac
    never saw answered matches an access of the same operation and ref. What is left over is
    unexplained, and that is what makes the audit fail. Requests the vault has no record of
    are counted (``unanswered``), not refused: they are the attempts that never arrived.
    Params: ``accesses``, the vault's records; ``requests``, this Mac's; ``namespace``.
    Returns: the ``VaultAuditReport``.
    Raises: nothing.
    """
    answered: Counter[tuple[str, str | None, int | None, int | None]] = Counter()
    unanswered: Counter[tuple[str, str]] = Counter()
    for request in requests:
        if request.namespace != namespace:
            continue
        if request.status is None:
            unanswered[(request.operation, request.ref)] += 1
        else:
            key = (request.operation, request.ref, request.status, _expected_server_bytes(request))
            answered[key] += 1
    mac_requests = sum(answered.values()) + sum(unanswered.values())
    served = [record for record in accesses if record.namespace == namespace]
    unexplained: list[UnexplainedAccess] = []
    matched = 0
    for record in served:
        if record.operation not in AUDITED_OPERATIONS:
            continue
        served_key = (record.operation, record.ref, record.status, _served_bytes(record))
        loose = (record.operation, record.ref or "")
        if answered[served_key] > 0:
            answered[served_key] -= 1
        elif unanswered[loose] > 0:
            unanswered[loose] -= 1
        else:
            unexplained.append(
                UnexplainedAccess(
                    time=record.time,
                    operation=record.operation,
                    ref=record.ref,
                    status=record.status,
                    bytes=record.bytes,
                    scope=record.scope,
                    user_login=record.user_login,
                )
            )
            continue
        matched += 1
    return VaultAuditReport(
        namespace=namespace,
        server_accesses=sum(record.operation in AUDITED_OPERATIONS for record in served),
        mac_requests=mac_requests,
        matched=matched,
        unexplained=tuple(unexplained),
        unanswered=sum(unanswered.values()),
        refused_at_server=sum(record.operation == "refused" for record in served),
    )
