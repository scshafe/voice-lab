"""The vault's two storage ports: content-addressed blobs and named compare-and-set refs.

@canonical storage_ports -- every reader and writer of the library vault depends on BlobStore
and RefStore and the STORAGE.* errors they raise; STORAGE_ERROR_CLASSES names the one error
class of every code, STORAGE_RETRYABLE whether it may be retried, and storage_error builds
the right class for a code.
Related: storage_contracts.py (BlobRef, RefValue, the vault records and the name helpers
re-exported here), storage_binding.py (the composition root that builds adapters),
tests/test_storage_contracts.py (these ports' tests).

The vault stores bytes and pointers and applies no lab rules. A namespace is fixed when an
adapter is built (by the server token or by a local directory) and is never a parameter of a
port method. Every method may fail with ``STORAGE.UNAVAILABLE`` or ``STORAGE.TIMEOUT``
(retryable), ``STORAGE.UNAUTHORIZED``, ``STORAGE.IDENTITY_MISMATCH``, ``STORAGE.NOT_APPROVED``,
``STORAGE.NOT_BOUND`` or ``STORAGE.PROTOCOL`` where its implementation can meet them; an
adapter wraps every transport failure (``OSError``, TLS and HTTP errors, timeouts, parse
errors) in a ``StorageError`` and never puts a secret in an error message. This module imports
only the standard library and the package's ``errors`` and ``storage_contracts`` modules.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Literal, Protocol

from .errors import SecurityError, VoiceLabError
from .storage_contracts import (
    MAX_BLOB_BYTES,
    BlobRef,
    RefValue,
    StorageNameError,
    library_head_ref,
    validate_library_id,
    validate_ref_name,
)

__all__ = [
    "MAX_BLOB_BYTES",
    "STORAGE_ERROR_CLASSES",
    "STORAGE_RETRYABLE",
    "BlobRef",
    "BlobStore",
    "RefStore",
    "RefValue",
    "StorageError",
    "StorageErrorCode",
    "StorageIntegrityError",
    "StorageNameError",
    "library_head_ref",
    "storage_error",
    "validate_library_id",
    "validate_ref_name",
]

type StorageErrorCode = Literal[
    "STORAGE.UNAVAILABLE",
    "STORAGE.TIMEOUT",
    "STORAGE.UNAUTHORIZED",
    "STORAGE.NOT_FOUND",
    "STORAGE.CONFLICT",
    "STORAGE.TOO_LARGE",
    "STORAGE.QUOTA",
    "STORAGE.INTEGRITY",
    "STORAGE.IDENTITY_MISMATCH",
    "STORAGE.NOT_APPROVED",
    "STORAGE.NOT_BOUND",
    "STORAGE.PROTOCOL",
]

# Only a failure that may pass by itself is retryable; every other code needs a change first.
STORAGE_RETRYABLE: Final[Mapping[StorageErrorCode, bool]] = MappingProxyType(
    {
        "STORAGE.UNAVAILABLE": True,
        "STORAGE.TIMEOUT": True,
        "STORAGE.UNAUTHORIZED": False,
        "STORAGE.NOT_FOUND": False,
        "STORAGE.CONFLICT": False,
        "STORAGE.TOO_LARGE": False,
        "STORAGE.QUOTA": False,
        "STORAGE.INTEGRITY": False,
        "STORAGE.IDENTITY_MISMATCH": False,
        "STORAGE.NOT_APPROVED": False,
        "STORAGE.NOT_BOUND": False,
        "STORAGE.PROTOCOL": False,
    }
)


class StorageError(VoiceLabError):
    """A named storage failure; ``code`` is one ``STORAGE.<FAILURE>`` value.

    Codes: ``STORAGE.UNAVAILABLE`` (the store cannot serve the call now; retryable),
    ``STORAGE.TIMEOUT`` (the call did not finish in time; retryable), ``STORAGE.NOT_FOUND``
    (no blob with that ref), ``STORAGE.CONFLICT`` (a ref swap's expected version is not the
    current one), ``STORAGE.TOO_LARGE`` (more than ``MAX_BLOB_BYTES``), ``STORAGE.QUOTA`` (the
    store has no room), ``STORAGE.NOT_APPROVED`` (no matching, effective destination
    approval), ``STORAGE.NOT_BOUND`` (the library has vault state but no binding, or a
    Keychain item the binding names is missing) and ``STORAGE.PROTOCOL`` (the peer answered
    outside the protocol). ``STORAGE.UNAUTHORIZED``, ``STORAGE.INTEGRITY`` and
    ``STORAGE.IDENTITY_MISMATCH`` are always ``StorageIntegrityError``.
    ``retryable`` comes from ``STORAGE_RETRYABLE``. ``str(error)`` is the message alone.
    Instances pickle with class, code and message intact, so one raised in a worker process
    arrives as the same error. It is not a ``ContractError``.
    Raises (on construction): ``ValueError`` for an unknown code, or for a code that
    ``STORAGE_ERROR_CLASSES`` gives to the other of the two classes; ``storage_error`` picks
    the class.
    """

    def __init__(self, code: StorageErrorCode, message: str) -> None:
        error_class = STORAGE_ERROR_CLASSES.get(code)
        if error_class is None:
            raise ValueError(f"unknown storage error code: {code!r}")
        integrity_code = issubclass(error_class, StorageIntegrityError)
        if isinstance(self, StorageIntegrityError) != integrity_code:
            raise ValueError(
                f"{code} is raised as {error_class.__name__}, not {type(self).__name__}; "
                "build it with storage_error"
            )
        self.code: StorageErrorCode = code
        self.retryable: bool = STORAGE_RETRYABLE[code]
        super().__init__(message)

    def __reduce__(self) -> tuple[type[StorageError], tuple[StorageErrorCode, str]]:
        return (type(self), (self.code, str(self)))


class StorageIntegrityError(SecurityError, StorageError):
    """Stored or returned bytes, the destination or the credentials failed a check.

    Codes: ``STORAGE.INTEGRITY`` (a digest, size, decryption, record or file-privacy check
    failed; nothing is repaired or overwritten), ``STORAGE.IDENTITY_MISMATCH`` (the
    destination is not the approved node, tag or tailnet) and ``STORAGE.UNAUTHORIZED`` (the
    store refused the credentials, or a token was used outside its namespace or scope).
    It is a ``SecurityError``, so existing fail-closed handling applies unchanged.
    """


STORAGE_ERROR_CLASSES: Final[Mapping[StorageErrorCode, type[StorageError]]] = MappingProxyType(
    {
        "STORAGE.UNAVAILABLE": StorageError,
        "STORAGE.TIMEOUT": StorageError,
        "STORAGE.UNAUTHORIZED": StorageIntegrityError,
        "STORAGE.NOT_FOUND": StorageError,
        "STORAGE.CONFLICT": StorageError,
        "STORAGE.TOO_LARGE": StorageError,
        "STORAGE.QUOTA": StorageError,
        "STORAGE.INTEGRITY": StorageIntegrityError,
        "STORAGE.IDENTITY_MISMATCH": StorageIntegrityError,
        "STORAGE.NOT_APPROVED": StorageError,
        "STORAGE.NOT_BOUND": StorageError,
        "STORAGE.PROTOCOL": StorageError,
    }
)


def storage_error(code: StorageErrorCode, message: str) -> StorageError:
    """Build the error for ``code`` as the class ``STORAGE_ERROR_CLASSES`` names.

    Params: ``code``; ``message``, which never contains a secret.
    Returns: a ``StorageError`` (a ``StorageIntegrityError`` for the three integrity codes).
    Raises: ``ValueError`` for an unknown code.
    """
    return STORAGE_ERROR_CLASSES.get(code, StorageError)(code, message)


class BlobStore(Protocol):
    """Write-once bytes in one namespace, named by the digest of what the store keeps.

    There is no list and no delete. A store that keeps bytes as given returns the same ref for
    the same bytes and keeps one copy. A store that transforms them (the sealed store encrypts
    each put with a fresh nonce) returns the ref of the bytes it keeps, so equal data put twice
    may get two refs; both stay readable.
    """

    def put(self, data: bytes) -> BlobRef:
        """Store ``data`` once.

        Params: ``data``, at most ``MAX_BLOB_BYTES`` for a store that keeps bytes as given.
        Returns: the ``BlobRef`` (SHA-256 and size) of the stored bytes; putting bytes that
        are already stored changes nothing and returns their ref.
        Raises: ``StorageError`` with ``STORAGE.TOO_LARGE`` (before anything is stored or
        sent) or ``STORAGE.QUOTA``; ``StorageIntegrityError`` (``STORAGE.INTEGRITY``) when a
        blob already stored under that ref is corrupt; it is never overwritten.
        """

    def has(self, ref: BlobRef) -> bool:
        """Whether a blob with this ref is stored, from metadata only; ``get`` is the proof.

        Params: ``ref``.
        Returns: ``True`` when an entry of the ref's size is stored, else ``False``.
        Raises: ``StorageIntegrityError`` (``STORAGE.INTEGRITY``) when the stored entry is
        unsafe (a link, or not private).
        """

    def get(self, ref: BlobRef) -> bytes:
        """Read one blob and check it before returning it.

        Params: ``ref``, as ``put`` returned it.
        Returns: the data that was put (after reversing any transform), only once the stored
        bytes match the ref's digest and size.
        Raises: ``StorageError`` with ``STORAGE.NOT_FOUND``; ``StorageIntegrityError``
        (``STORAGE.INTEGRITY``) when the bytes, their size or their decryption do not match.
        """


class RefStore(Protocol):
    """Named pointers at blobs in one namespace, changed only by compare-and-set.

    A library's newest manifest is named ``library_head_ref(library_id)``.
    """

    def read(self, name: str) -> RefValue | None:
        """The current value of a ref.

        Params: ``name``, a valid ref name.
        Returns: the ``RefValue`` with its version, or ``None`` when the ref does not exist.
        Raises: ``StorageNameError`` for an invalid name (before any I/O);
        ``StorageIntegrityError`` (``STORAGE.INTEGRITY``) when the stored value is invalid or
        names another ref.
        """

    def swap(self, name: str, *, expected_version: int | None, target: BlobRef) -> RefValue:
        """Point a ref at ``target`` if nobody changed it since ``expected_version``.

        Params: ``name``, a valid ref name; ``expected_version``, the version last read, or
        ``None`` when the ref must not exist yet; ``target``.
        Returns: the new ``RefValue``: version 1 for a new ref, else the previous version
        plus 1, with the store's time; the change is durable before this returns.
        Raises: ``StorageNameError`` for an invalid name (before any I/O); ``StorageError``
        with ``STORAGE.CONFLICT`` when the current version is not ``expected_version``.
        """
