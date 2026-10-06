"""Encryption of every blob the library vault keeps, and the library key that does it.

@canonical storage_sealing -- SealedBlobStore seals every blob with AES-256-GCM under the
library key (sealed format v1, the library id as associated data) before an inner BlobStore
sees it, and authenticates and opens it after; LibraryKey is the only holder of key bytes; the
key is kept through KeychainClient -- in the login Keychain on macOS (SecurityCliKeychain runs
/usr/bin/security only, never with a secret in argv), as systemd credentials on Linux
(SystemdCredentialKeychain reads $CREDENTIALS_DIRECTORY, D-034) -- and leaves the host only as
the recovery sheet; this is the only module that imports cryptography.
Related: storage_ports.py (BlobStore, the STORAGE.* errors), storage_contracts.py
(library_key_item, keychain_item_names, validate_library_id), storage_local.py (the store it
wraps first),
storage_binding.py (the composition root, which always wraps the vault in SealedBlobStore),
tests/test_storage_sealing.py, tests/storage_conformance.py (the sealed-local spec).

Threat model. The vault host is not trusted with plaintext: every blob is encrypted and
authenticated on the Mac before it leaves, and its ref is the digest and size of the
ciphertext. A fresh random nonce per put gives equal plaintexts unrelated ciphertext digests,
so a digest never shows that two blobs hold the same bytes (the copying layer de-duplicates on
the Mac instead). Only the digests are unrelated. Lengths are not hidden: a sealed blob is
exactly ``SEALING_OVERHEAD`` bytes longer than its plaintext, so the vault host learns every
blob's plaintext size (and when it arrived). Blobs with the same plaintext size can therefore
be linked to each other as probably the same file, and a distinctive size makes that link
strong: a file uploaded again (for example a restored library pushed again, or a take copied
again after the copy state was reset) repeats the sizes its blobs had. A host that already
holds a candidate file of distinctive size (a WAV file's size fixes its duration) can likewise
tell that a file of exactly that size is probably stored. For a file split into chunks, the
last chunk's size narrows both matches the same way. Sealed format v1 accepts that leak;
hiding lengths would need padding inside the authenticated plaintext and a new format version.
The key never leaves the Mac except as the recovery sheet the operator stores offline; losing
both loses what the vault holds.

Sealed format v1 (``SEALING_OVERHEAD`` = 33 bytes more than the plaintext)::

    b"CVLS" | 0x01 | nonce (12 random bytes) | AES-256-GCM ciphertext | tag (16 bytes)

The associated data is ``SEALED_BLOB_AAD_DOMAIN + library_id`` (ASCII), so a blob sealed for
one library never opens as another's. Any failure to open (a short blob, another magic or
version, a wrong key or library id, a changed nonce, ciphertext or tag) is
``StorageIntegrityError`` (``STORAGE.INTEGRITY``) whose message names only the ciphertext
ref. No message, repr or log of this module contains key material or plaintext, and no
secret is ever placed in a subprocess argument list or environment.

Library key custody. Each library has one 256-bit key, an item named
``library_key_item(library_id)`` holding 64 lowercase hex characters: on macOS a generic
password in the login Keychain (account ``LIBRARY_KEY_ACCOUNT``), on Linux a systemd
credential of that name, encrypted at rest by ``systemd-creds`` and decrypted by the service
manager into the vault unit's private, read-only credential directory. It is created once
(an existing item is never replaced) and restored only from its recovery sheet, a plain-text
page holding the library id, a fingerprint, the key in 16 groups of four hex digits and a
checksum. Apart from ``cryptography``, this module imports only the standard library and the
package's ``storage_contracts`` and ``storage_ports`` modules.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import os
import pwd
import re
import secrets
import stat
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, NoReturn, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .storage_contracts import library_key_item, validate_library_id
from .storage_ports import (
    MAX_BLOB_BYTES,
    BlobRef,
    BlobStore,
    StorageError,
    StorageErrorCode,
    storage_error,
)

__all__ = [
    "DEFAULT_KEYCHAIN_TIMEOUT_SECONDS",
    "KEYCHAIN_ITEM_NOT_FOUND_STATUS",
    "LIBRARY_KEY_ACCOUNT",
    "LIBRARY_KEY_BYTES",
    "MAX_SEALED_PLAINTEXT_BYTES",
    "RECOVERY_SHEET_FORMAT",
    "SEALED_BLOB_AAD_DOMAIN",
    "SEALED_BLOB_MAGIC",
    "SEALED_BLOB_VERSION",
    "SEALING_NONCE_BYTES",
    "SEALING_OVERHEAD",
    "SEALING_TAG_BYTES",
    "CREDENTIALS_DIRECTORY_VARIABLE",
    "SECURITY_TOOL",
    "KeychainClient",
    "LibraryKey",
    "SealedBlobStore",
    "SecurityCliKeychain",
    "SystemdCredentialKeychain",
    "create_library_key",
    "import_library_key",
    "library_key_checksum",
    "load_library_key",
    "open_sealed_blob",
    "parse_recovery_sheet",
    "render_recovery_sheet",
    "seal_blob",
    "write_recovery_sheet",
]

SEALED_BLOB_MAGIC: Final = b"CVLS"
SEALED_BLOB_VERSION: Final = 1
SEALING_NONCE_BYTES: Final = 12
SEALING_TAG_BYTES: Final = 16
_HEADER_BYTES: Final = len(SEALED_BLOB_MAGIC) + 1
SEALING_OVERHEAD: Final = _HEADER_BYTES + SEALING_NONCE_BYTES + SEALING_TAG_BYTES
MAX_SEALED_PLAINTEXT_BYTES: Final = MAX_BLOB_BYTES - SEALING_OVERHEAD
SEALED_BLOB_AAD_DOMAIN: Final = b"cole-voice-lab.sealed-blob.v1\x00"

LIBRARY_KEY_BYTES: Final = 32
LIBRARY_KEY_ACCOUNT: Final = "library-key"
_FINGERPRINT_DOMAIN: Final = b"cole-voice-lab.library-key-fingerprint/v1\x00"
_CHECKSUM_DOMAIN: Final = b"cole-voice-lab.library-key-checksum/v1\x00"
_KEY_HEX: Final = re.compile(r"[0-9a-f]{64}")

RECOVERY_SHEET_FORMAT: Final = "cole-voice-lab.library-key-recovery/v1"
_MAX_RECOVERY_SHEET_CHARACTERS: Final = 64 * 1024
_KEY_GROUP_CHARACTERS: Final = 4
_KEY_GROUPS_PER_LINE: Final = 4

SECURITY_TOOL: Final = Path("/usr/bin/security")
DEFAULT_KEYCHAIN_TIMEOUT_SECONDS: Final = 30.0
_MAX_KEYCHAIN_TIMEOUT_SECONDS: Final = 600.0
# errSecItemNotFound, as /usr/bin/security reports it in its exit status.
KEYCHAIN_ITEM_NOT_FOUND_STATUS: Final = 44
_KEYCHAIN_PATH: Final = "/usr/bin:/bin"
# Every word passed to /usr/bin/security (and on its interactive command line) is plain, so no
# quoting is ever needed and nothing can be read as another option or command.
_PLAIN_WORD: Final = re.compile(r"[A-Za-z0-9._:-]{1,255}")
_SECRET_WORD: Final = re.compile(r"[A-Za-z0-9._:-]{1,1024}")
_PLAIN_PATH: Final = re.compile(r"/[A-Za-z0-9._:/-]{1,1023}")
_SECRET_OUTPUT: Final = re.compile(rb"[\x21-\x7e]{1,1024}\n")
_NO_SPACE_ERRNOS: Final = frozenset({errno.ENOSPC, errno.EDQUOT})
# What systemd sets for a unit with LoadCredential(Encrypted)=/ImportCredential=: the absolute
# path of the unit's private, read-only credential directory.
CREDENTIALS_DIRECTORY_VARIABLE: Final = "CREDENTIALS_DIRECTORY"
# One credential: one secret word and an optional newline.
_MAX_CREDENTIAL_BYTES: Final = 1025
_CREDENTIAL: Final = re.compile(rb"[\x21-\x7e]{1,1024}\n?")
_CREDENTIAL_READ_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_CREDENTIAL_DIRECTORY_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class LibraryKey:
    """The 256-bit key of one library; the only object that holds key bytes.

    Its ``repr`` and ``str`` show only the fingerprint, it cannot be pickled or copied, and
    two keys compare equal (in constant time) when their bytes are equal.
    """

    __slots__ = ("_secret",)

    def __init__(self, secret: bytes) -> None:
        """Wrap ``secret``.

        Params: ``secret``, exactly ``LIBRARY_KEY_BYTES`` bytes.
        Raises: ``ValueError`` (whose message never shows the bytes) for anything else.
        """
        if type(secret) is not bytes or len(secret) != LIBRARY_KEY_BYTES:
            raise ValueError(f"a library key is exactly {LIBRARY_KEY_BYTES} bytes")
        self._secret = secret

    @classmethod
    def generate(cls) -> LibraryKey:
        """A new key from the operating system's secure random source."""
        return cls(secrets.token_bytes(LIBRARY_KEY_BYTES))

    def fingerprint(self) -> str:
        """16 hex characters that identify the key and are safe to show."""
        return hashlib.sha256(_FINGERPRINT_DOMAIN + self._secret).hexdigest()[:16]

    def __repr__(self) -> str:
        return f"LibraryKey(<redacted>, fingerprint={self.fingerprint()})"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, LibraryKey):
            return NotImplemented
        return hmac.compare_digest(self._secret, other._secret)

    __hash__ = None  # type: ignore[assignment]

    def __reduce__(self) -> NoReturn:
        raise TypeError("a library key is never pickled or copied")


def _associated_data(library_id: str) -> bytes:
    return SEALED_BLOB_AAD_DOMAIN + validate_library_id(library_id).encode("ascii")


def _too_large(size: int) -> StorageError:
    return storage_error(
        "STORAGE.TOO_LARGE",
        f"a sealed blob holds at most {MAX_SEALED_PLAINTEXT_BYTES} bytes of data, not {size}",
    )


def _seal(cipher: AESGCM, associated_data: bytes, plaintext: bytes) -> bytes:
    if len(plaintext) > MAX_SEALED_PLAINTEXT_BYTES:
        raise _too_large(len(plaintext))
    nonce = os.urandom(SEALING_NONCE_BYTES)
    header = SEALED_BLOB_MAGIC + bytes((SEALED_BLOB_VERSION,))
    return header + nonce + cipher.encrypt(nonce, plaintext, associated_data)


def _open(cipher: AESGCM, associated_data: bytes, sealed: bytes, label: str) -> bytes:
    if len(sealed) < SEALING_OVERHEAD:
        problem = f"is {len(sealed)} bytes, shorter than any sealed blob"
    elif sealed[: len(SEALED_BLOB_MAGIC)] != SEALED_BLOB_MAGIC:
        problem = "does not start with the sealed-blob magic"
    elif sealed[len(SEALED_BLOB_MAGIC)] != SEALED_BLOB_VERSION:
        problem = f"has sealed format version {sealed[len(SEALED_BLOB_MAGIC)]}, not 1"
    else:
        nonce = sealed[_HEADER_BYTES : _HEADER_BYTES + SEALING_NONCE_BYTES]
        body = sealed[_HEADER_BYTES + SEALING_NONCE_BYTES :]
        try:
            return cipher.decrypt(nonce, body, associated_data)
        except InvalidTag:
            problem = (
                "does not authenticate under this library's key and id (another key or "
                "library, or changed bytes)"
            )
    raise storage_error("STORAGE.INTEGRITY", f"sealed blob {label} {problem}")


def seal_blob(key: LibraryKey, library_id: str, plaintext: bytes) -> bytes:
    """Seal ``plaintext`` for ``library_id`` in sealed format v1, with a fresh random nonce.

    Params: ``key``; ``library_id``; ``plaintext``, at most ``MAX_SEALED_PLAINTEXT_BYTES``.
    Returns: the sealed bytes (``SEALING_OVERHEAD`` longer than ``plaintext``).
    Raises: ``StorageNameError`` for an invalid library id; ``StorageError``
    (``STORAGE.TOO_LARGE``) for oversize plaintext, before anything is encrypted.
    """
    return _seal(AESGCM(key._secret), _associated_data(library_id), plaintext)


def open_sealed_blob(key: LibraryKey, library_id: str, sealed: bytes, *, label: str) -> bytes:
    """Authenticate and open bytes that ``seal_blob`` produced.

    Params: ``key``; ``library_id``; ``sealed``; ``label``, what error messages call the
    blob (for example its ciphertext digest; never a secret).
    Returns: the plaintext.
    Raises: ``StorageNameError`` for an invalid library id; ``StorageIntegrityError``
    (``STORAGE.INTEGRITY``) when the bytes are not a v1 sealed blob of this key and library.
    """
    return _open(AESGCM(key._secret), _associated_data(library_id), sealed, label)


class SealedBlobStore:
    """``BlobStore`` that encrypts every blob before ``inner`` stores it.

    ``put`` returns the inner store's ref of the ciphertext, so the ref names only encrypted
    bytes; the same plaintext put twice gets two refs with different digests (a fresh nonce
    each time) and the same size, by design, and both stay readable. ``get`` lets the inner
    store check the ciphertext's digest and size, then authenticates and opens it. ``has``
    asks the inner store. The store holds the key, so it can be neither pickled nor copied.
    """

    def __init__(self, inner: BlobStore, *, key: LibraryKey, library_id: str) -> None:
        """Wrap ``inner`` for one library.

        Params: ``inner``, any ``BlobStore``; ``key``, the library's key; ``library_id``, the
        library every blob is sealed for.
        Raises: ``TypeError`` when ``key`` is not a ``LibraryKey``; ``StorageNameError`` for an
        invalid library id.
        """
        if not isinstance(key, LibraryKey):
            raise TypeError("a sealed blob store needs a LibraryKey")
        self._associated_data = _associated_data(library_id)
        self._library_id = library_id
        self._inner = inner
        self._cipher = AESGCM(key._secret)

    @property
    def library_id(self) -> str:
        """The library every blob of this store is sealed for."""
        return self._library_id

    def __repr__(self) -> str:
        return f"SealedBlobStore(library_id={self._library_id!r})"

    def __reduce__(self) -> NoReturn:
        raise TypeError("a sealed blob store holds a library key and is never pickled")

    def put(self, data: bytes) -> BlobRef:
        """Seal ``data`` and store the sealed bytes; see ``BlobStore.put``.

        Returns: the inner store's ref of the sealed bytes (a new ref on every call).
        Raises: ``StorageError`` (``STORAGE.TOO_LARGE``) when ``data`` is over
        ``MAX_SEALED_PLAINTEXT_BYTES``, before anything is encrypted or stored; otherwise
        whatever the inner store's ``put`` raises.
        """
        return self._inner.put(_seal(self._cipher, self._associated_data, data))

    def has(self, ref: BlobRef) -> bool:
        """Whether the inner store holds a blob with this ref; see ``BlobStore.has``.

        Raises: whatever the inner store's ``has`` raises.
        """
        return self._inner.has(ref)

    def get(self, ref: BlobRef) -> bytes:
        """Read, authenticate and open one blob; see ``BlobStore.get``.

        Raises: whatever the inner store's ``get`` raises (``STORAGE.NOT_FOUND``, and
        ``STORAGE.INTEGRITY`` for ciphertext that does not match its ref);
        ``StorageIntegrityError`` (``STORAGE.INTEGRITY``) when the stored bytes do not open
        under this library's key and id.
        """
        sealed = self._inner.get(ref)
        return _open(self._cipher, self._associated_data, sealed, ref.sha256)


class KeychainClient(Protocol):
    """Named secrets: the only way this lab keeps one (login Keychain, systemd credentials).

    Service, account and label are plain words (``[A-Za-z0-9._:-]``). An implementation never
    places a secret in a subprocess argument list, an environment or an error message.
    """

    def add_generic_password(self, *, service: str, account: str, label: str, secret: str) -> None:
        """Add a new item holding ``secret``; an existing item is never replaced.

        Raises: ``StorageError`` with ``STORAGE.CONFLICT`` when the item exists, or
        ``STORAGE.PROTOCOL`` when the Keychain did not keep exactly ``secret``.
        """
        ...

    def find_generic_password(self, *, service: str, account: str) -> str:
        """The secret an item holds.

        Raises: ``StorageError`` with ``STORAGE.NOT_BOUND`` (naming the item) when there is
        no such item, or ``STORAGE.PROTOCOL`` when the Keychain could not be read.
        """
        ...

    def has_generic_password(self, *, service: str, account: str) -> bool:
        """Whether the item exists, without reading its secret.

        Raises: ``StorageError`` (``STORAGE.PROTOCOL``) when the Keychain could not be asked.
        """
        ...


def _plain(field: str, value: str) -> str:
    if _PLAIN_WORD.fullmatch(value) is None:
        raise ValueError(f"a Keychain {field} is 1-255 characters of [A-Za-z0-9._:-]: {value!r}")
    return value


def _login_keychain() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / "Library/Keychains/login.keychain-db"


class SecurityCliKeychain:
    """``KeychainClient`` over ``/usr/bin/security`` and one keychain file.

    Every call runs the tool by absolute path with ``cwd="/"``, the environment
    ``{"PATH": "/usr/bin:/bin", "HOME": <the user's home from the password database>}``, a
    timeout and captured output that is never shown. A secret is written only on the
    standard input of ``security -i`` (as one ``add-generic-password`` command line) and read
    only from the standard output of ``find-generic-password -w``; it never appears in an
    argument list. Exit status ``KEYCHAIN_ITEM_NOT_FOUND_STATUS`` means "no such item"; any
    other failure (another status, a timeout, a tool that cannot be started, an answer that
    is not one line of printable ASCII) is ``STORAGE.PROTOCOL`` with a message that names the
    tool, the action and the item only.
    """

    def __init__(
        self,
        *,
        security: Path = SECURITY_TOOL,
        keychain: Path | None = None,
        timeout: float = DEFAULT_KEYCHAIN_TIMEOUT_SECONDS,
    ) -> None:
        """Choose the tool, the keychain file and the timeout.

        Params: ``security``, the tool's absolute path (tests pass a fake); ``keychain``, the
        keychain file (default: the user's ``~/Library/Keychains/login.keychain-db``, from the
        password database, not ``$HOME``); ``timeout``, seconds per call (0 < timeout <= 600).
        Raises: ``ValueError`` for a relative or unnormalized tool or keychain path, a keychain
        path with characters outside ``[A-Za-z0-9._:/-]``, or a bad timeout.
        """
        chosen = _login_keychain() if keychain is None else keychain
        for field, path in (("tool", security), ("keychain", chosen)):
            text = os.fspath(path)
            if _PLAIN_PATH.fullmatch(text) is None or os.path.normpath(text) != text:
                raise ValueError(
                    f"the Keychain {field} is an absolute, normalized path of "
                    f"[A-Za-z0-9._:/-]: {text!r}"
                )
        if not 0 < timeout <= _MAX_KEYCHAIN_TIMEOUT_SECONDS:
            raise ValueError(
                f"a Keychain timeout is more than 0 and at most "
                f"{_MAX_KEYCHAIN_TIMEOUT_SECONDS:g} seconds, not {timeout!r}"
            )
        self.security = security
        self.keychain = chosen
        self.timeout = timeout
        self.environment: dict[str, str] = {
            "PATH": _KEYCHAIN_PATH,
            "HOME": pwd.getpwuid(os.getuid()).pw_dir,
        }

    def _failure(self, action: str, problem: str) -> StorageError:
        return storage_error(
            "STORAGE.PROTOCOL", f"the Keychain tool {self.security} {problem} while {action}"
        )

    def _run(
        self, arguments: tuple[str, ...], *, stdin: bytes | None, action: str
    ) -> subprocess.CompletedProcess[bytes]:
        """Run the tool once; the exception text never carries argv, input or output."""
        command = (os.fspath(self.security), *arguments)
        try:
            if stdin is None:
                return subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    check=False,
                    timeout=self.timeout,
                    env=self.environment,
                    cwd="/",
                )
            return subprocess.run(
                command,
                input=stdin,
                capture_output=True,
                check=False,
                timeout=self.timeout,
                env=self.environment,
                cwd="/",
            )
        except subprocess.TimeoutExpired:
            problem = f"did not finish within {self.timeout:g} s"
        except OSError as error:
            problem = f"could not be started ({error.strerror or type(error).__name__})"
        # Raised outside the handler, so the failure (which holds argv and output) is not
        # even attached as the error's context.
        raise self._failure(action, problem)

    def has_generic_password(self, *, service: str, account: str) -> bool:
        """Whether the item exists; see ``KeychainClient.has_generic_password``.

        Raises: ``ValueError`` for a service or account that is not a plain word;
        ``StorageError`` (``STORAGE.PROTOCOL``) for any failure of the tool.
        """
        action = f"looking for Keychain item {_plain('service', service)}"
        arguments = ("find-generic-password", "-a", _plain("account", account), "-s", service)
        completed = self._run((*arguments, os.fspath(self.keychain)), stdin=None, action=action)
        if completed.returncode == 0:
            return True
        if completed.returncode == KEYCHAIN_ITEM_NOT_FOUND_STATUS:
            return False
        raise self._failure(action, f"exited with status {completed.returncode}")

    def find_generic_password(self, *, service: str, account: str) -> str:
        """The item's secret; see ``KeychainClient.find_generic_password``.

        Raises: ``ValueError`` for a service or account that is not a plain word;
        ``StorageError`` with ``STORAGE.NOT_BOUND`` (no such item) or ``STORAGE.PROTOCOL``.
        """
        action = f"reading Keychain item {_plain('service', service)}"
        arguments = ("find-generic-password", "-a", _plain("account", account), "-s", service)
        completed = self._run(
            (*arguments, "-w", os.fspath(self.keychain)), stdin=None, action=action
        )
        if completed.returncode == KEYCHAIN_ITEM_NOT_FOUND_STATUS:
            raise storage_error(
                "STORAGE.NOT_BOUND",
                f"the Keychain {self.keychain} has no item {service} (account {account}); a "
                "library key comes back only from its recovery sheet, a vault token by binding "
                "the vault again",
            )
        if completed.returncode != 0:
            raise self._failure(action, f"exited with status {completed.returncode}")
        if _SECRET_OUTPUT.fullmatch(completed.stdout) is None:
            raise self._failure(action, "answered with something other than one plain secret")
        return completed.stdout[:-1].decode("ascii")

    def add_generic_password(self, *, service: str, account: str, label: str, secret: str) -> None:
        """Add a new item, then read it back; see ``KeychainClient.add_generic_password``.

        The item is added by ``security -i`` with the command on standard input, without
        ``-U`` (so an existing item is never updated) and without ``-A``/``-T`` (so only
        ``/usr/bin/security`` may read it without asking).
        Raises: ``ValueError`` for a service, account or label that is not a plain word, or a
        secret that is not 1-1024 characters of ``[A-Za-z0-9._:-]`` (the message never shows
        it); ``StorageError`` with ``STORAGE.CONFLICT`` (the item exists) or
        ``STORAGE.PROTOCOL`` (the tool failed, or the item does not hold ``secret``
        afterwards).
        """
        _plain("label", label)
        if type(secret) is not str or _SECRET_WORD.fullmatch(secret) is None:
            raise ValueError("a Keychain secret is 1-1024 characters of [A-Za-z0-9._:-]")
        if self.has_generic_password(service=service, account=account):
            raise storage_error(
                "STORAGE.CONFLICT",
                f"the Keychain {self.keychain} already holds item {service} (account "
                f"{account}); an existing Keychain item is never replaced",
            )
        action = f"adding Keychain item {service}"
        line = (
            f"add-generic-password -a {account} -s {service} -l {label} -w {secret} "
            f"{os.fspath(self.keychain)}\n"
        )
        completed = self._run(("-i",), stdin=line.encode("ascii"), action=action)
        if completed.returncode != 0:
            raise self._failure(action, f"exited with status {completed.returncode}")
        try:
            stored = self.find_generic_password(service=service, account=account)
        except StorageError as error:
            if error.code != "STORAGE.NOT_BOUND":
                raise
            problem = "kept no item"
        else:
            if hmac.compare_digest(stored.encode("ascii"), secret.encode("ascii")):
                return
            problem = "kept an item that holds a different secret"
        raise self._failure(action, problem)


class SystemdCredentialKeychain:
    """``KeychainClient`` over one systemd credential directory (Linux; D-034).

    An item is the file named by its service in the directory: the vault unit's
    ``$CREDENTIALS_DIRECTORY``, which the service manager fills from the encrypted credentials
    under ``/etc/credstore.encrypted/`` (``ImportCredential=``) and keeps private and
    read-only, so nothing a unit runs can add or replace an item. Each item name belongs to one
    account in this lab, so ``account`` is checked as a plain word and not stored. Reading
    follows ``SecurityCliKeychain``'s rules: an item holds one plain secret word (an ending
    newline is allowed), and no message ever repeats it. An item must be a regular file with
    one link, owned by this user or root, that no other user may open and no group may write.

    ``add_generic_password`` is how the operator creates the key and the tokens once, before
    any unit runs: ``CREDENTIALS_DIRECTORY`` names a private staging directory of this user
    (0700, best on a tmpfs), each item becomes a new 0600 file there (never replacing one), and
    the operator then encrypts each with ``systemd-creds encrypt --name=<item>`` and removes
    the staging directory. Under systemd the directory is read-only, so an add is refused.
    """

    def __init__(self, directory: Path) -> None:
        """Choose the credential directory.

        Params: ``directory``, an absolute, normalized path (it may hold ``@``, as systemd's
        ``/run/credentials/<unit>`` does, but no control character).
        Raises: ``ValueError`` for anything else.
        """
        text = os.fspath(directory)
        if (
            not os.path.isabs(text)
            or os.path.normpath(text) != text
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in text)
        ):
            raise ValueError(
                f"a credential directory is an absolute, normalized path: {text!r:.200}"
            )
        self.directory = Path(text)

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> SystemdCredentialKeychain:
        """The credential directory systemd named for this process.

        Params: ``environment`` (default ``os.environ``).
        Returns: the keychain over ``$CREDENTIALS_DIRECTORY``.
        Raises: ``StorageError`` (``STORAGE.PROTOCOL``) when the variable is unset, empty or
        not an absolute, normalized path: outside a unit that loads the vault credentials
        there is nothing to read.
        """
        values = os.environ if environment is None else environment
        named = values.get(CREDENTIALS_DIRECTORY_VARIABLE, "")
        if named:
            try:
                return cls(Path(named))
            except ValueError:
                pass
        raise storage_error(
            "STORAGE.PROTOCOL",
            f"${CREDENTIALS_DIRECTORY_VARIABLE} does not name a credential directory, so the "
            "library key and vault tokens cannot be read: on Linux they are systemd "
            "credentials, loaded only for the vault unit (voice-lab-vault@<destination>) or a "
            "`systemd-run -p ImportCredential=…` command; to create them once, point "
            f"{CREDENTIALS_DIRECTORY_VARIABLE} at a private staging directory "
            "(docs/LIBRARY_VAULT.md)",
        )

    def __repr__(self) -> str:
        return f"SystemdCredentialKeychain({os.fspath(self.directory)!r})"

    def _failure(self, problem: str) -> StorageError:
        return storage_error(
            "STORAGE.PROTOCOL", f"the credential directory {self.directory} {problem}"
        )

    def _open_directory(self) -> int:
        try:
            return os.open(self.directory, _CREDENTIAL_DIRECTORY_FLAGS)
        except OSError as error:
            problem = error.strerror or type(error).__name__
        raise self._failure(f"cannot be opened as a directory ({problem})")

    def _metadata(self, directory: int, service: str) -> os.stat_result | None:
        """The item's metadata without following a link; ``None`` when there is none."""
        try:
            return os.stat(service, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as error:
            problem = error.strerror or type(error).__name__
        raise self._failure(f"could not be asked for item {service} ({problem})")

    def _require_private(self, metadata: os.stat_result, service: str) -> None:
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid not in (os.getuid(), 0)
            or metadata.st_mode & 0o027
        ):
            raise self._failure(
                f"holds item {service}, but not as a private regular file with one link "
                "(owned by this user or root, no access for others, no group write)"
            )

    def has_generic_password(self, *, service: str, account: str) -> bool:
        """Whether the item exists; see ``KeychainClient.has_generic_password``.

        Raises: ``ValueError`` for a service or account that is not a plain word;
        ``StorageError`` (``STORAGE.PROTOCOL``) when the directory cannot be read or holds
        the item as something other than a private regular file.
        """
        _plain("service", service)
        _plain("account", account)
        directory = self._open_directory()
        try:
            metadata = self._metadata(directory, service)
        finally:
            os.close(directory)
        if metadata is None:
            return False
        self._require_private(metadata, service)
        return True

    def find_generic_password(self, *, service: str, account: str) -> str:
        """The item's secret; see ``KeychainClient.find_generic_password``.

        Raises: ``ValueError`` for a service or account that is not a plain word;
        ``StorageError`` with ``STORAGE.NOT_BOUND`` (no such item) or ``STORAGE.PROTOCOL``
        (an unreadable directory or item, or one that is not one plain secret word).
        """
        _plain("service", service)
        _plain("account", account)
        directory = self._open_directory()
        try:
            data = self._read(directory, service)
        finally:
            os.close(directory)
        if data is None:
            raise storage_error(
                "STORAGE.NOT_BOUND",
                f"the credential directory {self.directory} has no item {service}; a library key "
                "comes back only from its recovery sheet, a vault token by creating or importing "
                "it again, and both reach a unit only as encrypted credentials it imports",
            )
        if _CREDENTIAL.fullmatch(data) is None:
            raise self._failure(f"holds item {service}, but not as one plain secret")
        return data.rstrip(b"\n").decode("ascii")

    def _read(self, directory: int, service: str) -> bytes | None:
        try:
            descriptor = os.open(service, _CREDENTIAL_READ_FLAGS, dir_fd=directory)
        except FileNotFoundError:
            return None
        except OSError as error:
            problem = error.strerror or type(error).__name__
            raise self._failure(f"could not open item {service} ({problem})") from None
        try:
            metadata = os.fstat(descriptor)
            self._require_private(metadata, service)
            if metadata.st_size > _MAX_CREDENTIAL_BYTES:
                raise self._failure(f"holds item {service}, but it is too large to be a secret")
            os.set_blocking(descriptor, True)
            return os.read(descriptor, _MAX_CREDENTIAL_BYTES + 1)
        except OSError as error:
            problem = error.strerror or type(error).__name__
            raise self._failure(f"could not read item {service} ({problem})") from None
        finally:
            os.close(descriptor)

    def add_generic_password(self, *, service: str, account: str, label: str, secret: str) -> None:
        """Write a new item, then read it back; see ``KeychainClient.add_generic_password``.

        The directory must be a private (0700, no group or other access) directory of this
        user; the item is created with ``O_CREAT|O_EXCL|O_NOFOLLOW`` at 0600, written as the
        secret and a newline, fsynced, and the directory fsynced. An item that could not be
        written completely is removed.
        Raises: ``ValueError`` for a service, account or label that is not a plain word, or a
        secret that is not 1-1024 characters of ``[A-Za-z0-9._:-]`` (the message never shows
        it); ``StorageError`` with ``STORAGE.CONFLICT`` (the item exists) or
        ``STORAGE.PROTOCOL`` (the directory is not a private writable directory of this user
        -- as under systemd, where it is read-only -- the write failed, or the item does not
        hold ``secret`` afterwards).
        """
        _plain("service", service)
        _plain("account", account)
        _plain("label", label)
        if type(secret) is not str or _SECRET_WORD.fullmatch(secret) is None:
            raise ValueError("a credential secret is 1-1024 characters of [A-Za-z0-9._:-]")
        directory = self._open_directory()
        try:
            metadata = os.fstat(directory)
            if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
                raise self._failure(
                    "is not a private directory of this user (0700), so no credential is "
                    "written there; under systemd it is read-only by design"
                )
            try:
                _write_new_file(directory, service, f"{secret}\n".encode("ascii"), label=service)
            except FileExistsError:
                raise storage_error(
                    "STORAGE.CONFLICT",
                    f"the credential directory {self.directory} already holds item {service}; "
                    "an existing credential is never replaced",
                ) from None
            except (OSError, _LeftoverFileError) as failed:
                error = failed.error if isinstance(failed, _LeftoverFileError) else failed
                problem = error.strerror or type(error).__name__
                left = (
                    f"; the incomplete item {service} could not be removed, delete it by hand"
                    if isinstance(failed, _LeftoverFileError)
                    else ""
                )
                raise self._failure(f"refused item {service} ({problem}){left}") from None
        finally:
            os.close(directory)
        stored = self.find_generic_password(service=service, account=account)
        if not hmac.compare_digest(stored.encode("ascii"), secret.encode("ascii")):
            raise self._failure(f"kept an item {service} that holds a different secret")


def _library_key_item(library_id: str) -> str:
    return library_key_item(library_id)


def load_library_key(keychain: KeychainClient, library_id: str) -> LibraryKey:
    """Read a library's key from the Keychain.

    Params: ``keychain``; ``library_id``.
    Returns: the key.
    Raises: ``StorageNameError`` for an invalid library id; ``StorageError`` with
    ``STORAGE.NOT_BOUND`` (naming the item and the recovery sheet) when the item is missing,
    or ``STORAGE.PROTOCOL`` when the Keychain could not be read; ``StorageIntegrityError``
    (``STORAGE.INTEGRITY``) when the item does not hold 64 lowercase hex characters.
    """
    service = _library_key_item(library_id)
    try:
        secret = keychain.find_generic_password(service=service, account=LIBRARY_KEY_ACCOUNT)
    except StorageError as error:
        if error.code != "STORAGE.NOT_BOUND":
            raise
        missing = True
    else:
        missing = False
    if missing:
        raise storage_error(
            "STORAGE.NOT_BOUND",
            f"there is no library key item {service} for library {library_id} (the login "
            "Keychain on macOS, the unit's systemd credentials on Linux); restore it from the "
            "library's recovery sheet",
        )
    if _KEY_HEX.fullmatch(secret) is None:
        raise storage_error(
            "STORAGE.INTEGRITY",
            f"the Keychain item {service} does not hold a library key "
            "(64 lowercase hex characters)",
        )
    return LibraryKey(bytes.fromhex(secret))


def _store_new_key(keychain: KeychainClient, library_id: str, key: LibraryKey) -> None:
    service = _library_key_item(library_id)
    if keychain.has_generic_password(service=service, account=LIBRARY_KEY_ACCOUNT):
        raise storage_error(
            "STORAGE.CONFLICT",
            f"the library key item {service} already exists; a library key is never replaced",
        )
    keychain.add_generic_password(
        service=service, account=LIBRARY_KEY_ACCOUNT, label=service, secret=key._secret.hex()
    )
    if load_library_key(keychain, library_id) != key:
        raise storage_error(
            "STORAGE.PROTOCOL",
            f"the Keychain item {service} does not hold the key that was just written",
        )


def create_library_key(keychain: KeychainClient, library_id: str) -> LibraryKey:
    """Create a library's key and keep it in the Keychain.

    Params: ``keychain``; ``library_id``.
    Returns: the new key (render its recovery sheet next).
    Raises: ``StorageNameError`` for an invalid library id; ``StorageError`` with
    ``STORAGE.CONFLICT`` when the library already has a key item (nothing is changed), or
    ``STORAGE.PROTOCOL`` when the Keychain failed or does not hold the new key afterwards.
    """
    key = LibraryKey.generate()
    _store_new_key(keychain, library_id, key)
    return key


def import_library_key(keychain: KeychainClient, library_id: str, sheet_text: str) -> LibraryKey:
    """Restore a library's key from its recovery sheet into the Keychain.

    The sheet is checked completely before the Keychain is touched.
    Params: ``keychain``; ``library_id``; ``sheet_text``, the whole sheet.
    Returns: the restored key.
    Raises: as ``parse_recovery_sheet``, then as ``create_library_key`` (``STORAGE.CONFLICT``
    when the library already has a key item).
    """
    key = parse_recovery_sheet(sheet_text, library_id)
    _store_new_key(keychain, library_id, key)
    return key


def library_key_checksum(library_id: str, key: LibraryKey) -> str:
    """The recovery sheet's checksum: 8 hex characters over the library id and the key.

    Raises: ``StorageNameError`` for an invalid library id.
    """
    material = _CHECKSUM_DOMAIN + validate_library_id(library_id).encode("ascii") + key._secret
    return hashlib.sha256(material).hexdigest()[:8]


def render_recovery_sheet(library_id: str, key: LibraryKey, created_at: datetime) -> str:
    """The plain-text recovery sheet of a library's key.

    Params: ``library_id``; ``key``; ``created_at``, a timezone-aware time.
    Returns: the sheet: what it is and how to keep it, the format, the library id, the
    creation time, the key's fingerprint, the key as 16 groups of four lowercase hex digits,
    the checksum, and how to restore it.
    Raises: ``StorageNameError`` for an invalid library id; ``ValueError`` for a naive time.
    """
    if created_at.utcoffset() is None:
        raise ValueError("a recovery sheet's creation time is timezone-aware")
    checksum = library_key_checksum(library_id, key)
    digits = key._secret.hex()
    groups = [
        digits[start : start + _KEY_GROUP_CHARACTERS]
        for start in range(0, len(digits), _KEY_GROUP_CHARACTERS)
    ]
    rows = [
        "  " + " ".join(groups[start : start + _KEY_GROUPS_PER_LINE])
        for start in range(0, len(groups), _KEY_GROUPS_PER_LINE)
    ]
    created = created_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return "\n".join(
        (
            "COLE VOICE LAB - LIBRARY VAULT KEY RECOVERY SHEET",
            "",
            f"This sheet holds the key that encrypts library {library_id} in the vault.",
            "Anyone with this key and the vault's contents can read the library, including",
            "recordings of a voice. Keep it offline (for example in a password manager) and",
            "never in the library, the repository or a chat. If both this sheet and the Mac's",
            "Keychain item are lost, the vault's copy of the library cannot be decrypted.",
            "",
            f"Format: {RECOVERY_SHEET_FORMAT}",
            f"Library id: {library_id}",
            f"Created: {created}",
            f"Key fingerprint: {key.fingerprint()}",
            "Key:",
            *rows,
            f"Checksum: {checksum}",
            "",
            "To restore: import this whole sheet with the lab's vault key import on the Mac",
            "that holds the library. The import checks the library id and the checksum, then",
            "writes the key into the login Keychain; it never replaces an existing key.",
            "",
        )
    )


def _sheet_field(lines: Sequence[str], name: str) -> str:
    """The value of the one ``<name>: <value>`` line of a sheet."""
    prefix = f"{name}:"
    values = [line[len(prefix) :].strip() for line in lines if line.startswith(prefix)]
    if len(values) != 1:
        raise storage_error(
            "STORAGE.INTEGRITY",
            f"a recovery sheet has exactly one '{prefix}' line, not {len(values)}",
        )
    return values[0]


def parse_recovery_sheet(text: str, library_id: str) -> LibraryKey:
    """Read the key back from a recovery sheet after checking it.

    Spaces, blank lines, indentation and upper case in the key groups are tolerated, so a
    retyped sheet reads back; every other line is matched as ``render_recovery_sheet`` wrote
    it (after stripping surrounding whitespace).
    Params: ``text``, the whole sheet; ``library_id``, the library it must belong to.
    Returns: the key.
    Raises: ``StorageNameError`` for an invalid library id; ``StorageIntegrityError``
    (``STORAGE.INTEGRITY``, never showing key digits) when the sheet is too long, has another
    format, names another library, lacks or repeats a field, its key is not 64 hex digits, or
    its fingerprint or checksum does not match.
    """
    validate_library_id(library_id)
    if len(text) > _MAX_RECOVERY_SHEET_CHARACTERS:
        raise storage_error(
            "STORAGE.INTEGRITY",
            f"a recovery sheet is at most {_MAX_RECOVERY_SHEET_CHARACTERS} characters",
        )
    lines = [line.strip() for line in text.splitlines()]
    if _sheet_field(lines, "Format") != RECOVERY_SHEET_FORMAT:
        raise storage_error(
            "STORAGE.INTEGRITY", f"the recovery sheet is not in format {RECOVERY_SHEET_FORMAT}"
        )
    named = _sheet_field(lines, "Library id")
    if named != library_id:
        raise storage_error(
            "STORAGE.INTEGRITY",
            f"the recovery sheet is for library {named!r:.80}, not {library_id}",
        )
    fingerprint = _sheet_field(lines, "Key fingerprint").lower()
    checksum = _sheet_field(lines, "Checksum").lower()
    # The key groups run from the one "Key:" line (which may hold some of them) to the one
    # "Checksum:" line; both lines exist once, as _sheet_field just checked.
    first = _sheet_field(lines, "Key")
    start = next(index for index, line in enumerate(lines) if line.startswith("Key:"))
    end = next(index for index, line in enumerate(lines) if line.startswith("Checksum:"))
    digits = "".join(" ".join((first, *lines[start + 1 : end])).split()).lower()
    if end < start or _KEY_HEX.fullmatch(digits) is None:
        raise storage_error(
            "STORAGE.INTEGRITY",
            "the recovery sheet's key is not 16 groups of four hex digits between 'Key:' and "
            "'Checksum:'",
        )
    key = LibraryKey(bytes.fromhex(digits))
    if not hmac.compare_digest(checksum.encode(), library_key_checksum(library_id, key).encode()):
        raise storage_error(
            "STORAGE.INTEGRITY",
            "the recovery sheet's checksum does not match its key; a key group was probably "
            "mistyped",
        )
    if not hmac.compare_digest(fingerprint.encode(), key.fingerprint().encode()):
        raise storage_error(
            "STORAGE.INTEGRITY", "the recovery sheet's fingerprint does not match its key"
        )
    return key


def _identity(path: str) -> tuple[int, int] | None:
    try:
        metadata = os.stat(path)
    except FileNotFoundError:
        return None
    return metadata.st_dev, metadata.st_ino


def _forbidden_root_holding(
    parent: str, parent_identity: tuple[int, int], forbidden_roots: Sequence[Path]
) -> Path | None:
    """The forbidden root that the directory ``parent`` is inside (or is), if any.

    ``parent`` is resolved and ``parent_identity`` is the identity of its open descriptor.
    Compared by path (both resolved, case-folded, as APFS compares names) and by the identity
    of the directory and each of its ancestors, so neither a symlink nor another spelling
    hides a root.
    """
    folded = parent.casefold()
    identities: dict[tuple[int, int], Path] = {}
    for root in forbidden_roots:
        resolved = os.path.realpath(root).casefold()
        if folded == resolved or folded.startswith(resolved.rstrip("/") + "/"):
            return root
        identity = _identity(os.fspath(root))
        if identity is not None:
            identities[identity] = root
    identity = parent_identity
    current = parent
    while identity not in identities:
        above = os.path.dirname(current)
        if above == current:
            return None
        current = above
        identity = _identity(current)
    return identities[identity]


class _LeftoverFileError(Exception):
    """A new file failed to be written completely and then failed to be removed."""

    def __init__(self, error: OSError) -> None:
        super().__init__(error.strerror or type(error).__name__)
        self.error = error


def _removed(directory: int, name: str) -> bool:
    """Remove ``name`` from ``directory``; whether nothing is left there under that name."""
    try:
        os.unlink(name, dir_fd=directory)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def _close_while_failing(descriptor: int, failure: BaseException) -> None:
    """Close ``descriptor`` while ``failure`` propagates; a close error never replaces it.

    The descriptor is released even when ``close`` reports an error, so that error is only
    added to ``failure`` as a note.
    """
    try:
        os.close(descriptor)
    except OSError as error:
        failure.add_note(f"closing the file also failed ({error.strerror or type(error).__name__})")


def _write_new_file(directory: int, name: str, data: bytes, *, label: str) -> None:
    """Create ``name`` (0600, never following or replacing an entry), write, fsync and close it.

    The directory is fsynced after the file is closed. If any step fails, closing included,
    the new file is removed again; a failure to close while another failure is on its way is
    added to that failure as a note and never replaces it.
    Raises: the ``OSError`` of the failed step when the file is gone again (or was never
    created); ``_LeftoverFileError`` holding it when the file could not be removed, so
    it may still hold part or all of ``data``. Any other exception propagates with a note
    naming ``label`` when the file could not be removed.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(name, flags, 0o600, dir_fd=directory)
    still_open = True
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(data)
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        # Marked first: a descriptor whose close() failed is released all the same, and
        # closing its number again could close a descriptor opened since.
        still_open = False
        os.close(descriptor)
        os.fsync(directory)
    except BaseException as error:
        if still_open:
            _close_while_failing(descriptor, error)
        if _removed(directory, name):
            raise
        if isinstance(error, OSError):
            raise _LeftoverFileError(error) from None
        error.add_note(f"an incompletely written file could not be removed: delete {label}")
        raise


def _open_owned_directory(parent: str, label: Path) -> int:
    """A descriptor of the existing directory ``parent`` if the current user owns it."""
    try:
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        pass
    else:
        if os.fstat(directory).st_uid == os.getuid():
            return directory
        os.close(directory)
    raise storage_error(
        "STORAGE.INTEGRITY",
        f"a recovery sheet goes into an existing directory owned by you: {label}",
    )


def write_recovery_sheet(path: Path, text: str, *, forbidden_roots: Sequence[Path]) -> None:
    """Write a recovery sheet to a new private file outside the library and the repository.

    The file is created with ``O_CREAT|O_EXCL|O_NOFOLLOW`` and mode 0600 relative to its
    parent directory's descriptor, written, fsynced and closed; the directory is fsynced
    too. A file for which any of those steps failed is removed; if even that fails, the error
    says the file is still at ``path`` and must be deleted by hand.
    Params: ``path``, an absolute, normalized path whose parent is an existing directory
    owned by the current user; ``text``, a sheet from ``render_recovery_sheet``;
    ``forbidden_roots``, at least one directory the sheet must never be inside (the CLI passes
    the asset root and the repository).
    Returns: nothing.
    Raises: ``ValueError`` for a relative or unnormalized path, the root directory, no
    forbidden roots, or text that is not a rendered sheet; ``StorageIntegrityError``
    (``STORAGE.INTEGRITY``) when the parent is not an existing directory owned by the current
    user or is inside a forbidden root; ``StorageError`` with ``STORAGE.CONFLICT`` when
    something already exists at ``path`` (it is never replaced), ``STORAGE.QUOTA`` (no space)
    or ``STORAGE.UNAVAILABLE`` (any other filesystem failure).
    """
    text_path = os.fspath(path)
    if not os.path.isabs(text_path) or os.path.normpath(text_path) != text_path or not path.name:
        raise ValueError(
            f"a recovery sheet path is an absolute, normalized file path: {text_path!r}"
        )
    if not forbidden_roots:
        raise ValueError("a recovery sheet needs the roots it must stay out of")
    if f"\nFormat: {RECOVERY_SHEET_FORMAT}\n" not in text:
        raise ValueError("only a rendered recovery sheet is written")
    parent = os.path.realpath(path.parent)
    directory = _open_owned_directory(parent, path.parent)
    try:
        failure = _write_sheet_into(directory, parent, path, text, forbidden_roots)
    except BaseException as error:
        _close_while_failing(directory, error)
        raise
    closing = ""
    try:
        os.close(directory)
    except OSError as error:
        # The descriptor is released even when close() fails. It was opened read-only, so
        # the failure cannot change the sheet; it is reported, never swallowed.
        closing = error.strerror or type(error).__name__
    if failure is None:
        if closing:
            raise storage_error(
                "STORAGE.UNAVAILABLE",
                f"the recovery sheet was written completely to {text_path}, but closing its "
                f"directory then failed ({closing}); check the file before relying on it",
            )
        return
    code, problem, leftover = failure
    if closing:
        problem = f"{problem}; closing its directory also failed ({closing})"
    if leftover:
        raise storage_error(
            code,
            f"the recovery sheet at {text_path} was not written completely: {problem}; the file "
            "could not be removed and may hold the whole key, so delete it by hand before "
            "writing the sheet again",
        )
    raise storage_error(code, f"the recovery sheet was not written to {text_path}: {problem}")


def _write_sheet_into(
    directory: int, parent: str, path: Path, text: str, forbidden_roots: Sequence[Path]
) -> tuple[StorageErrorCode, str, bool] | None:
    """Write the sheet into the open ``directory``; None, or the refusal to report.

    Returns: None when the sheet was written, otherwise ``(code, problem, leftover)``, where
    ``leftover`` says the incomplete file could not be removed.
    Raises: ``StorageIntegrityError`` (``STORAGE.INTEGRITY``) when ``parent`` is inside a
    forbidden root.
    """
    text_path = os.fspath(path)
    metadata = os.fstat(directory)
    inside = _forbidden_root_holding(parent, (metadata.st_dev, metadata.st_ino), forbidden_roots)
    if inside is not None:
        raise storage_error(
            "STORAGE.INTEGRITY",
            f"a recovery sheet is never written inside {inside}: {text_path}",
        )
    try:
        _write_new_file(directory, path.name, text.encode("utf-8"), label=text_path)
    except FileExistsError:
        return (
            "STORAGE.CONFLICT",
            "something already exists there, and a recovery sheet never replaces it",
            False,
        )
    except (OSError, _LeftoverFileError) as failed:
        error = failed.error if isinstance(failed, _LeftoverFileError) else failed
        code: StorageErrorCode = (
            "STORAGE.QUOTA" if error.errno in _NO_SPACE_ERRNOS else "STORAGE.UNAVAILABLE"
        )
        problem = f"the filesystem refused it ({error.strerror or type(error).__name__})"
        return (code, problem, isinstance(failed, _LeftoverFileError))
    return None
