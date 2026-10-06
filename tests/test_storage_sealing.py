"""The sealed store, the library key, its Keychain custody and its recovery sheet.

Scenario tests for ``src/cole_voice_lab/storage_sealing.py`` beyond the port behaviour that
``tests/storage_conformance.py`` checks for the ``sealed-local`` spec. Stores live under
``tmp_path``; keys are synthetic. No test touches the real login Keychain: the Keychain tool is
a fake ``security`` script written into ``tmp_path`` that records its argv, standard input and
environment, and an autouse guard refuses (without running it) any ``subprocess.run`` of
``/usr/bin/security`` or of any other ``security`` outside the test's own directory.
"""

from __future__ import annotations

import ast
import copy
import errno
import hashlib
import json
import os
import pickle
import pwd
import re
import stat
import subprocess
import sys
import tomllib
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import macos_only
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from cole_voice_lab import storage_sealing
from cole_voice_lab.storage_contracts import keychain_item_names
from cole_voice_lab.storage_local import LocalBlobStore
from cole_voice_lab.storage_ports import (
    MAX_BLOB_BYTES,
    BlobRef,
    StorageError,
    StorageIntegrityError,
    StorageNameError,
    storage_error,
)
from cole_voice_lab.storage_sealing import (
    KEYCHAIN_ITEM_NOT_FOUND_STATUS,
    LIBRARY_KEY_ACCOUNT,
    MAX_SEALED_PLAINTEXT_BYTES,
    RECOVERY_SHEET_FORMAT,
    SEALED_BLOB_AAD_DOMAIN,
    SEALING_OVERHEAD,
    SECURITY_TOOL,
    LibraryKey,
    SealedBlobStore,
    SecurityCliKeychain,
    create_library_key,
    import_library_key,
    library_key_checksum,
    load_library_key,
    open_sealed_blob,
    parse_recovery_sheet,
    render_recovery_sheet,
    seal_blob,
    write_recovery_sheet,
)

REPOSITORY = Path(__file__).resolve().parents[1]
SOURCE = REPOSITORY / "src" / "cole_voice_lab"
LIBRARY = "sealing-library"
OTHER_LIBRARY = "another-library"
PAYLOAD = b"synthetic sealed payload"
# Synthetic keys only; never a real one.
KEY_BYTES = bytes(range(32))
OTHER_KEY_BYTES = bytes(range(100, 132))
CREATED = datetime(2026, 9, 17, 12, 30, 45, tzinfo=UTC)
SERVICE = keychain_item_names(LIBRARY).key
HOME = pwd.getpwuid(os.getuid()).pw_dir
# Python itself adds these to a child's environment on macOS (locale coercion, CoreFoundation).
INTERPRETER_ENVIRONMENT = frozenset({"__CF_USER_TEXT_ENCODING", "LC_CTYPE"})


def key() -> LibraryKey:
    return LibraryKey(KEY_BYTES)


def other_key() -> LibraryKey:
    return LibraryKey(OTHER_KEY_BYTES)


def secret_forms(secret: bytes) -> tuple[str, ...]:
    """Every spelling of a key that must never appear in text: hex, groups, upper case."""
    digits = secret.hex()
    groups = " ".join(digits[start : start + 4] for start in range(0, 64, 4))
    return (digits, digits.upper(), groups, digits[:16], repr(secret)[2:-1])


def texts_of(error: BaseException) -> list[str]:
    """The text of an error and of everything chained to it."""
    texts: list[str] = []
    current: BaseException | None = error
    while current is not None:
        texts += [str(current), repr(current), *(str(argument) for argument in current.args)]
        current = current.__cause__ or current.__context__
    return texts


def assert_secret_absent(secret: str | bytes, *texts: str) -> None:
    forms = secret_forms(secret) if isinstance(secret, bytes) else (secret,)
    for text in texts:
        for form in forms:
            assert form not in text, text


def failure(
    code: str, action: Callable[[], object], *, match: str | None = None, integrity: bool = False
) -> StorageError:
    with pytest.raises(StorageIntegrityError if integrity else StorageError, match=match) as caught:
        action()
    assert caught.value.code == code, str(caught.value)
    assert isinstance(caught.value, StorageIntegrityError) is integrity
    return caught.value


def digest_ref(data: bytes) -> BlobRef:
    return BlobRef(sha256=hashlib.sha256(data).hexdigest(), size=len(data))


@pytest.fixture
def base(tmp_path: Path) -> Path:
    resolved = Path(os.path.realpath(tmp_path)) / "base"
    resolved.mkdir(mode=0o700)
    return resolved


@pytest.fixture
def local(base: Path) -> LocalBlobStore:
    return LocalBlobStore(base / "store", create=True)


@pytest.fixture
def sealed(local: LocalBlobStore) -> SealedBlobStore:
    return SealedBlobStore(local, key=key(), library_id=LIBRARY)


# -- the guard against the real Keychain tool ---------------------------------------------


class RealKeychainToolRun(BaseException):
    """Raised instead of running the real Keychain tool; no handler in the module catches it."""


@dataclass
class Launch:
    """One ``subprocess.run`` call as the code under test made it."""

    arguments: tuple[str, ...]
    options: dict[str, Any]


@dataclass
class Launches:
    """Every ``subprocess.run`` of this test, and the refused runs of a real Keychain tool."""

    runs: list[Launch] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)


@pytest.fixture(autouse=True)
def launches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Launches]:
    recorded = Launches()
    real_run = subprocess.run
    allowed = Path(os.path.realpath(tmp_path))
    real_tool = os.path.realpath(SECURITY_TOOL)

    def guarded(arguments: Any, *positional: Any, **options: Any) -> Any:
        executable = os.path.realpath(os.fspath(arguments[0]))
        if executable == real_tool or (
            os.path.basename(executable) == "security"
            and not Path(executable).is_relative_to(allowed)
        ):
            recorded.refused.append(executable)
            raise RealKeychainToolRun(executable)
        recorded.runs.append(Launch(tuple(os.fspath(item) for item in arguments), dict(options)))
        return real_run(arguments, *positional, **options)

    monkeypatch.setattr(subprocess, "run", guarded)
    yield recorded
    if recorded.refused:
        pytest.fail(f"a test tried to run a real Keychain tool: {recorded.refused}")


def test_the_guard_refuses_the_real_keychain_tool_without_running_it(launches: Launches) -> None:
    client = SecurityCliKeychain()
    assert client.security == Path("/usr/bin/security")
    with pytest.raises(RealKeychainToolRun):
        client.has_generic_password(service=SERVICE, account=LIBRARY_KEY_ACCOUNT)
    assert launches.refused == [os.path.realpath("/usr/bin/security")]
    assert launches.runs == []
    launches.refused.clear()


# -- the fake Keychain tool -----------------------------------------------------------------

FAKE_SECURITY = """#!{python} -IB
import json, os, shlex, sys, time

STATE = {state!r}


def load(name, default):
    try:
        with open(os.path.join(STATE, name)) as stream:
            return json.load(stream)
    except FileNotFoundError:
        return default


def save(name, value):
    with open(os.path.join(STATE, name), "w") as stream:
        json.dump(value, stream)


def finish(status, output=""):
    sys.stdout.write(output)
    sys.stdout.flush()
    os._exit(status)


arguments = sys.argv[1:]
given = sys.stdin.read()
calls = load("calls.json", [])
calls.append({{"argv": sys.argv, "stdin": given, "env": dict(os.environ), "cwd": os.getcwd()}})
save("calls.json", calls)
command = "add" if arguments[:1] == ["-i"] else "read" if "-w" in arguments else "look"
rule = load("behaviour.json", {{}}).get(command, {{}})
if "sleep" in rule:
    time.sleep(rule["sleep"])
if "exit" in rule:
    finish(rule["exit"], rule.get("stdout", ""))
if "stdout" in rule:
    finish(0, rule["stdout"])
items = load("items.json", {{}})
if command == "add":
    for line in given.splitlines():
        words = shlex.split(line)
        values = dict(zip(words[1:-1:2], words[2:-1:2]))
        item = values["-s"] + "|" + values["-a"]
        if words[0] != "add-generic-password" or item in items:
            sys.stderr.write("security: the item already exists or the command is unknown\\n")
            continue
        if not rule.get("ignore"):
            secret = rule.get("replace", values["-w"])
            items[item] = {{"label": values["-l"], "secret": secret, "keychain": words[-1]}}
    save("items.json", items)
    # Interactive mode does not report a failed command in its exit status.
    finish(0, "security> security> ")
assert arguments[0] == "find-generic-password", arguments
values = dict(zip(arguments[1:5:2], arguments[2:5:2]))
found = items.get(values["-s"] + "|" + values["-a"])
if found is None or found["keychain"] != arguments[-1]:
    sys.stderr.write("security: The specified item could not be found in the keychain.\\n")
    finish(44)
if command == "read":
    finish(0, found["secret"] + "\\n")
finish(0, 'keychain: "' + arguments[-1] + '"\\nclass: "genp"\\n')
"""


@dataclass
class FakeSecurity:
    """A fake ``security`` in ``tmp_path`` and its recorded state."""

    tool: Path
    state: Path
    keychain: Path

    def client(self, *, timeout: float = 30.0) -> SecurityCliKeychain:
        return SecurityCliKeychain(security=self.tool, keychain=self.keychain, timeout=timeout)

    def calls(self) -> list[dict[str, Any]]:
        path = self.state / "calls.json"
        return json.loads(path.read_text()) if path.exists() else []

    def items(self) -> dict[str, dict[str, str]]:
        path = self.state / "items.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def behave(self, command: str, **rule: object) -> None:
        path = self.state / "behaviour.json"
        behaviour = json.loads(path.read_text()) if path.exists() else {}
        behaviour[command] = rule
        path.write_text(json.dumps(behaviour))

    def plant(
        self, service: str, account: str, secret: str, *, keychain: Path | None = None
    ) -> None:
        items = self.items()
        items[f"{service}|{account}"] = {
            "label": service,
            "secret": secret,
            "keychain": os.fspath(keychain or self.keychain),
        }
        (self.state / "items.json").write_text(json.dumps(items))


@pytest.fixture
def fake(base: Path) -> FakeSecurity:
    tools = base / "fake-tool"
    state = base / "fake-state"
    tools.mkdir(mode=0o700)
    state.mkdir(mode=0o700)
    tool = tools / "security"
    tool.write_text(FAKE_SECURITY.format(python=sys.executable, state=os.fspath(state)))
    tool.chmod(0o700)
    return FakeSecurity(tool=tool, state=state, keychain=base / "keychains" / "test.keychain-db")


def assert_disciplined(fake: FakeSecurity, launches: Launches, secret: str) -> None:
    """No secret in any argv or environment; every run is the fake with the fixed env."""
    assert launches.runs, "the code under test ran nothing"
    for launch in launches.runs:
        assert launch.arguments[0] == os.fspath(fake.tool)
        assert launch.options["env"] == {"PATH": "/usr/bin:/bin", "HOME": HOME}
        assert launch.options["cwd"] == "/"
        assert launch.options["check"] is False and launch.options["capture_output"] is True
        assert launch.options["timeout"] > 0
        assert_secret_absent(secret, *launch.arguments, *launch.options["env"].values())
    for call in fake.calls():
        assert_secret_absent(secret, *call["argv"], *call["env"].values())
        assert set(call["env"]) - INTERPRETER_ENVIRONMENT == {"PATH", "HOME"}
        assert call["cwd"] == "/"


# -- import closure and error construction ----------------------------------------------


def test_the_sealing_module_imports_only_the_standard_library_cryptography_and_storage() -> None:
    allowed_package = {"storage_contracts", "storage_ports"}
    allowed_external = set(sys.stdlib_module_names) | {"__future__", "cryptography"}
    tree = ast.parse((SOURCE / "storage_sealing.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            assert node.level == 1 and node.module in allowed_package, (node.lineno, node.module)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").partition(".")[0])
        elif isinstance(node, ast.Import):
            imported.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.Call):
            called = getattr(node.func, "id", getattr(node.func, "attr", None))
            assert called not in {"import_module", "__import__", "run_module"}, node.lineno
    assert imported <= allowed_external and "cryptography" in imported


def test_every_storage_error_is_built_by_the_port_factory() -> None:
    tree = ast.parse((SOURCE / "storage_sealing.py").read_text(encoding="utf-8"))
    constructed = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "storage_error" in constructed
    assert not constructed & {"StorageError", "StorageIntegrityError"}


def test_the_service_and_the_composition_root_load_no_encryption_at_import() -> None:
    probe = (
        "import sys, cole_voice_lab.storage_binding, cole_voice_lab.recording_service, "
        "cole_voice_lab.recording_finetune_jobs, cole_voice_lab.cli; "
        "print(sorted(name for name in sys.modules "
        "if name.startswith(('cryptography', 'cole_voice_lab.storage_sealing'))))"
    )
    completed = subprocess.run(
        (sys.executable, "-I", "-B", "-c", probe),
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
        env={"PATH": "/usr/bin:/bin", "HOME": HOME},
    )
    assert completed.stdout.strip() == "[]"


def test_the_module_states_its_threat_model() -> None:
    docstring = " ".join((storage_sealing.__doc__ or "").split())
    assert "@canonical storage_sealing --" in docstring
    for claim in (
        "The vault host is not trusted with plaintext",
        "its ref is the digest and size of the ciphertext",
        "gives equal plaintexts unrelated ciphertext digests",
        "Only the digests are unrelated.",
        "Lengths are not hidden: a sealed blob is exactly ``SEALING_OVERHEAD`` bytes longer than "
        "its plaintext, so the vault host learns every blob's plaintext size",
        "Blobs with the same plaintext size can therefore be linked to each other as probably "
        "the same file, and a distinctive size makes that link strong",
        "can likewise tell that a file of exactly that size is probably stored",
        "Sealed format v1 accepts that leak",
        "The key never leaves the Mac except as the recovery sheet the operator stores offline",
    ):
        assert claim in docstring, claim
    # A ref carries the blob's size, so equal plaintexts are linkable by size, and a host can
    # match a file it already holds by its size: neither may be denied.
    for overclaim in ("unlinkable", "cannot confirm", "no two refs"):
        assert overclaim not in docstring.lower(), overclaim


# -- the dependency lock ------------------------------------------------------------------


def test_the_vault_lock_pins_exactly_the_sealing_closure_with_hashes() -> None:
    lines = (REPOSITORY / "requirements/vault-macos.txt").read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("# Library vault encryption only. Reviewed PyPI macOS arm64 wheels")
    pins: dict[str, tuple[str, str]] = {}
    for line in lines:
        if line.startswith("#"):
            continue
        requirement, hash_option = line.split(" ")
        name, version = requirement.split("==")
        assert hash_option.startswith("--hash=sha256:") and len(hash_option) == 14 + 64
        pins[name] = (version, hash_option.removeprefix("--hash=sha256:"))
    assert pins == {
        "cryptography": (
            "50.0.1",
            "b8f852c65863251b9e3a1b8c150ce21e59b522dbb6a7d4bc80e680d38388e986",
        ),
        "cffi": ("2.1.1", "f81b3b8f3d4e343550fa4baa0e479bba9f2d29ce9c2e9b51d1ce1718d7442fcf"),
        "pycparser": ("3.0", "b727414169a36b7d524c1c3e31839a521725078d7b2ff038656844266160a992"),
    }
    project = tomllib.loads((REPOSITORY / "pyproject.toml").read_text(encoding="utf-8"))
    extras = project["project"]["optional-dependencies"]
    # No marker: the capture kit's bootstrap accepts only `extra == ...` markers.
    assert extras["vault"] == ["cryptography==50.0.1"]
    assert "cryptography==50.0.1" in extras["dev"]
    assert not any("cryptography" in item for item in project["project"]["dependencies"])


def test_the_installed_sealing_closure_is_the_locked_one() -> None:
    from importlib.metadata import version

    assert (version("cryptography"), version("cffi"), version("pycparser")) == (
        "50.0.1",
        "2.1.1",
        "3.0",
    )


# -- the sealed format --------------------------------------------------------------------


def test_the_sealed_format_leaves_room_for_its_overhead_in_a_blob() -> None:
    assert SEALING_OVERHEAD == 4 + 1 + 12 + 16 == 33
    assert MAX_SEALED_PLAINTEXT_BYTES == MAX_BLOB_BYTES - 33
    assert SEALED_BLOB_AAD_DOMAIN == b"cole-voice-lab.sealed-blob.v1\x00"


def test_a_sealed_blob_is_the_documented_aes_gcm_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nonce = bytes(range(200, 212))
    monkeypatch.setattr(storage_sealing.os, "urandom", lambda size: nonce[:size])
    sealed = seal_blob(key(), LIBRARY, PAYLOAD)
    associated = b"cole-voice-lab.sealed-blob.v1\x00" + LIBRARY.encode("ascii")
    assert sealed == b"CVLS\x01" + nonce + AESGCM(KEY_BYTES).encrypt(nonce, PAYLOAD, associated)
    assert len(sealed) == len(PAYLOAD) + SEALING_OVERHEAD
    assert open_sealed_blob(key(), LIBRARY, sealed, label="known") == PAYLOAD


def test_a_sealed_blob_round_trips_and_its_ref_names_only_ciphertext(
    local: LocalBlobStore, sealed: SealedBlobStore
) -> None:
    ref = sealed.put(PAYLOAD)
    stored = local.get(ref)
    assert ref == digest_ref(stored) != digest_ref(PAYLOAD)
    assert stored.startswith(b"CVLS\x01") and PAYLOAD not in stored
    assert len(stored) == len(PAYLOAD) + SEALING_OVERHEAD
    assert sealed.has(ref) and sealed.get(ref) == PAYLOAD
    reopened = SealedBlobStore(LocalBlobStore(local.root), key=key(), library_id=LIBRARY)
    assert reopened.get(ref) == PAYLOAD
    assert sealed.library_id == LIBRARY


def test_equal_plaintexts_get_unrelated_digests_but_the_same_size(
    local: LocalBlobStore, sealed: SealedBlobStore
) -> None:
    first = sealed.put(PAYLOAD)
    second = sealed.put(PAYLOAD)
    # The size in the ref is the leak the module's threat model states.
    assert first.sha256 != second.sha256 and first.size == second.size
    assert local.get(first)[5:17] != local.get(second)[5:17]
    assert sealed.get(first) == sealed.get(second) == PAYLOAD
    assert sealed.put(b"") != sealed.put(b"")


def test_seal_blob_and_open_sealed_blob_round_trip() -> None:
    sealed = seal_blob(key(), LIBRARY, PAYLOAD)
    assert open_sealed_blob(key(), LIBRARY, sealed, label="direct") == PAYLOAD
    assert open_sealed_blob(key(), LIBRARY, seal_blob(key(), LIBRARY, b""), label="empty") == b""
    with pytest.raises(StorageNameError):
        seal_blob(key(), "Not A Library", PAYLOAD)
    with pytest.raises(StorageNameError):
        open_sealed_blob(key(), "Not A Library", sealed, label="direct")


def refused_open(
    store: SealedBlobStore, local: LocalBlobStore, stored: bytes, *, match: str
) -> StorageError:
    """Store ``stored`` as its own valid blob and read it through ``store``."""
    ref = local.put(stored)
    error = failure("STORAGE.INTEGRITY", lambda: store.get(ref), match=match, integrity=True)
    assert ref.sha256 in str(error)
    assert error.__cause__ is None and error.__context__ is None
    assert_secret_absent(KEY_BYTES, *texts_of(error))
    assert_secret_absent(OTHER_KEY_BYTES, *texts_of(error))
    for text in texts_of(error):
        assert "synthetic sealed payload" not in text
    return error


AUTHENTICATION = "does not authenticate under this library's key and id"


def test_a_blob_sealed_under_another_key_is_refused(
    local: LocalBlobStore, sealed: SealedBlobStore
) -> None:
    ref = sealed.put(PAYLOAD)
    stranger = SealedBlobStore(local, key=other_key(), library_id=LIBRARY)
    refused_open(stranger, local, local.get(ref), match=AUTHENTICATION)


def test_a_blob_sealed_for_another_library_is_refused(
    local: LocalBlobStore, sealed: SealedBlobStore
) -> None:
    ref = sealed.put(PAYLOAD)
    other_library = SealedBlobStore(local, key=key(), library_id=OTHER_LIBRARY)
    refused_open(other_library, local, local.get(ref), match=AUTHENTICATION)
    # And the other way round: the library id is bound in both directions.
    foreign = other_library.put(PAYLOAD)
    refused_open(sealed, local, local.get(foreign), match=AUTHENTICATION)


@pytest.mark.parametrize("length", [0, 1, 32])
def test_a_blob_shorter_than_the_sealed_format_is_refused(
    local: LocalBlobStore, sealed: SealedBlobStore, length: int
) -> None:
    stored = local.get(sealed.put(PAYLOAD))
    refused_open(sealed, local, stored[:length], match=f"is {length} bytes, shorter")


@pytest.mark.parametrize("cut", [1, 16, len(PAYLOAD)])
def test_a_truncated_blob_is_refused(
    local: LocalBlobStore, sealed: SealedBlobStore, cut: int
) -> None:
    stored = local.get(sealed.put(PAYLOAD))
    refused_open(sealed, local, stored[:-cut], match=AUTHENTICATION)


def test_a_blob_with_another_blobs_nonce_is_refused(
    local: LocalBlobStore, sealed: SealedBlobStore
) -> None:
    first = local.get(sealed.put(PAYLOAD))
    second = local.get(sealed.put(PAYLOAD))
    swapped = second[:5] + first[5:17] + second[17:]
    assert swapped != second
    refused_open(sealed, local, swapped, match=AUTHENTICATION)


@pytest.mark.parametrize(
    "position", [5, 16, 17, 17 + len(PAYLOAD) - 1, -16, -1], ids=lambda value: f"byte{value}"
)
def test_a_changed_nonce_ciphertext_or_tag_byte_is_refused(
    local: LocalBlobStore, sealed: SealedBlobStore, position: int
) -> None:
    stored = bytearray(local.get(sealed.put(PAYLOAD)))
    stored[position] ^= 0x01
    refused_open(sealed, local, bytes(stored), match=AUTHENTICATION)


def test_a_blob_with_another_magic_or_version_is_refused(
    local: LocalBlobStore, sealed: SealedBlobStore
) -> None:
    stored = local.get(sealed.put(PAYLOAD))
    refused_open(sealed, local, b"CVLT" + stored[4:], match="does not start with the sealed-blob")
    refused_open(sealed, local, b"cvls" + stored[4:], match="does not start with the sealed-blob")
    for version in (0, 2, 255):
        refused_open(
            sealed,
            local,
            stored[:4] + bytes((version,)) + stored[5:],
            match=f"has sealed format version {version}, not 1",
        )


def test_an_oversize_plaintext_is_refused_before_anything_is_encrypted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inner = MemoryBlobStore()
    store = SealedBlobStore(inner, key=key(), library_id=LIBRARY)

    def no_nonce(size: int) -> bytes:
        raise AssertionError("a nonce was drawn for an oversize plaintext")

    monkeypatch.setattr(storage_sealing.os, "urandom", no_nonce)
    oversize = b"\0" * (MAX_SEALED_PLAINTEXT_BYTES + 1)
    error = failure("STORAGE.TOO_LARGE", lambda: store.put(oversize))
    assert str(MAX_SEALED_PLAINTEXT_BYTES) in str(error)
    assert str(MAX_SEALED_PLAINTEXT_BYTES + 1) in str(error)
    failure("STORAGE.TOO_LARGE", lambda: seal_blob(key(), LIBRARY, oversize))
    assert inner.puts == []


def test_the_largest_plaintext_fills_one_blob_exactly(local: LocalBlobStore) -> None:
    store = SealedBlobStore(local, key=key(), library_id=LIBRARY)
    ref = store.put(b"\x33" * MAX_SEALED_PLAINTEXT_BYTES)
    assert ref.size == MAX_BLOB_BYTES


class MemoryBlobStore:
    """A ``BlobStore`` in memory that records what it was given."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.puts: list[bytes] = []
        self.failure: StorageError | None = None

    def put(self, data: bytes) -> BlobRef:
        self.puts.append(data)
        ref = digest_ref(data)
        self.blobs[ref.sha256] = data
        return ref

    def has(self, ref: BlobRef) -> bool:
        return ref.sha256 in self.blobs

    def get(self, ref: BlobRef) -> bytes:
        if self.failure is not None:
            raise self.failure
        if ref.sha256 not in self.blobs:
            raise storage_error("STORAGE.NOT_FOUND", f"no blob {ref.sha256}")
        return self.blobs[ref.sha256]


def test_the_inner_stores_answers_and_failures_pass_through_unchanged() -> None:
    inner = MemoryBlobStore()
    store = SealedBlobStore(inner, key=key(), library_id=LIBRARY)
    ref = store.put(PAYLOAD)
    assert inner.puts == [inner.blobs[ref.sha256]]
    assert store.has(ref) and not store.has(digest_ref(PAYLOAD))
    missing = digest_ref(b"never stored")
    error = failure("STORAGE.NOT_FOUND", lambda: store.get(missing))
    assert missing.sha256 in str(error)
    inner.failure = storage_error("STORAGE.UNAVAILABLE", "the inner store is away")
    with pytest.raises(StorageError) as caught:
        store.get(ref)
    assert caught.value is inner.failure


def test_a_sealed_store_needs_a_library_key_and_a_valid_library_id() -> None:
    with pytest.raises(TypeError, match="needs a LibraryKey"):
        SealedBlobStore(MemoryBlobStore(), key=KEY_BYTES, library_id=LIBRARY)  # type: ignore[arg-type]
    for library_id in ("", "Upper", "a/b", "x" * 65, "-leading"):
        with pytest.raises(StorageNameError):
            SealedBlobStore(MemoryBlobStore(), key=key(), library_id=library_id)


def test_a_sealed_store_never_shows_or_hands_over_its_key(sealed: SealedBlobStore) -> None:
    assert repr(sealed) == f"SealedBlobStore(library_id={LIBRARY!r})"
    assert_secret_absent(KEY_BYTES, repr(sealed), str(sealed))
    with pytest.raises(TypeError, match="never pickled"):
        pickle.dumps(sealed)
    with pytest.raises(TypeError, match="never pickled"):
        copy.copy(sealed)


# -- the library key ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "secret",
    [b"", bytes(31), bytes(33), bytearray(32), memoryview(bytes(32)), "0" * 32, "0" * 64, None],
    ids=["empty", "short", "long", "bytearray", "memoryview", "text32", "hex64", "none"],
)
def test_a_library_key_is_exactly_32_bytes(secret: Any) -> None:
    with pytest.raises(ValueError, match="exactly 32 bytes") as caught:
        LibraryKey(secret)
    assert "0000" not in str(caught.value)


def test_a_library_key_shows_only_its_fingerprint() -> None:
    library_key = key()
    fingerprint = hashlib.sha256(
        b"cole-voice-lab.library-key-fingerprint/v1\x00" + KEY_BYTES
    ).hexdigest()[:16]
    assert library_key.fingerprint() == fingerprint
    assert (
        repr(library_key)
        == str(library_key)
        == f"LibraryKey(<redacted>, fingerprint={fingerprint})"
    )
    assert f"{library_key}" == str(library_key)
    assert_secret_absent(KEY_BYTES, repr(library_key), str(library_key))
    assert other_key().fingerprint() != fingerprint
    with pytest.raises(TypeError):
        vars(library_key)


def test_a_library_key_is_never_pickled_or_copied() -> None:
    for action in (pickle.dumps, copy.copy, copy.deepcopy):
        with pytest.raises(TypeError, match="never pickled or copied") as caught:
            action(key())
        assert_secret_absent(KEY_BYTES, *texts_of(caught.value))


def test_library_keys_compare_by_their_bytes_and_are_unhashable() -> None:
    assert key() == key()
    assert key() != other_key()
    assert key() != KEY_BYTES
    assert (key() == object()) is False
    with pytest.raises(TypeError):
        hash(key())


def test_a_generated_key_comes_from_the_secure_random_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = LibraryKey.generate()
    assert first != LibraryKey.generate()
    drawn: list[int] = []

    def token_bytes(size: int) -> bytes:
        drawn.append(size)
        return OTHER_KEY_BYTES

    monkeypatch.setattr(storage_sealing.secrets, "token_bytes", token_bytes)
    assert LibraryKey.generate() == other_key() and drawn == [32]


# -- the Keychain tool --------------------------------------------------------------------


def test_the_default_keychain_is_the_login_keychain_of_the_password_database(
    launches: Launches, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", "/nonexistent-home")
    client = SecurityCliKeychain()
    assert client.security == SECURITY_TOOL == Path("/usr/bin/security")
    assert client.keychain == Path(HOME) / "Library/Keychains/login.keychain-db"
    assert client.timeout == 30.0
    assert client.environment == {"PATH": "/usr/bin:/bin", "HOME": HOME}
    assert launches.runs == []


@pytest.mark.parametrize(
    "options",
    [
        {"security": Path("usr/bin/security")},
        {"security": Path("/usr/bin/../bin/security")},
        {"security": Path("/usr/local/bin/my security")},
        {"keychain": Path("login.keychain-db")},
        {"keychain": Path("/Users/x/Library/Keychains/login keychain")},
        {"keychain": Path("/")},
        {"keychain": Path("/Users/x/caf\u00e9.keychain-db")},
        {"timeout": 0},
        {"timeout": -1.0},
        {"timeout": 600.5},
        {"timeout": float("nan")},
    ],
    ids=lambda options: repr(options),
)
def test_a_keychain_client_refuses_unsafe_settings(options: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="Keychain"):
        SecurityCliKeychain(**options)


def test_a_new_item_is_added_through_standard_input_and_read_back(
    fake: FakeSecurity, launches: Launches
) -> None:
    secret = KEY_BYTES.hex()
    client = fake.client()
    assert not client.has_generic_password(service=SERVICE, account=LIBRARY_KEY_ACCOUNT)
    client.add_generic_password(
        service=SERVICE, account=LIBRARY_KEY_ACCOUNT, label="vault-key:sealing", secret=secret
    )
    keychain = os.fspath(fake.keychain)
    looked = ["find-generic-password", "-a", LIBRARY_KEY_ACCOUNT, "-s", SERVICE, keychain]
    read = ["find-generic-password", "-a", LIBRARY_KEY_ACCOUNT, "-s", SERVICE, "-w", keychain]
    calls = fake.calls()
    assert [call["argv"][1:] for call in calls] == [looked, looked, ["-i"], read]
    assert [call["stdin"] for call in calls] == [
        "",
        "",
        f"add-generic-password -a {LIBRARY_KEY_ACCOUNT} -s {SERVICE} -l vault-key:sealing "
        f"-w {secret} {keychain}\n",
        "",
    ]
    assert fake.items() == {
        f"{SERVICE}|{LIBRARY_KEY_ACCOUNT}": {
            "label": "vault-key:sealing",
            "secret": secret,
            "keychain": keychain,
        }
    }
    assert client.has_generic_password(service=SERVICE, account=LIBRARY_KEY_ACCOUNT)
    assert client.find_generic_password(service=SERVICE, account=LIBRARY_KEY_ACCOUNT) == secret
    assert_disciplined(fake, launches, secret)
    # Only the add carries input, and only on standard input; the lookups read nothing.
    stdins = [launch.options.get("input") for launch in launches.runs]
    assert stdins.count(None) == len(stdins) - 1
    assert [launch.options.get("stdin") for launch in launches.runs].count(
        subprocess.DEVNULL
    ) == len(stdins) - 1


def test_an_existing_item_is_never_replaced(fake: FakeSecurity, launches: Launches) -> None:
    fake.plant(SERVICE, LIBRARY_KEY_ACCOUNT, "a" * 64)
    error = failure(
        "STORAGE.CONFLICT",
        lambda: fake.client().add_generic_password(
            service=SERVICE, account=LIBRARY_KEY_ACCOUNT, label=SERVICE, secret="b" * 64
        ),
        match="already holds item .* never replaced",
    )
    assert_secret_absent("b" * 64, *texts_of(error))
    assert [call["argv"][1] for call in fake.calls()] == ["find-generic-password"]
    assert fake.items()[f"{SERVICE}|{LIBRARY_KEY_ACCOUNT}"]["secret"] == "a" * 64
    assert all("-U" not in launch.arguments for launch in launches.runs)


def test_an_item_in_another_keychain_file_is_not_this_keychains(
    fake: FakeSecurity, base: Path
) -> None:
    fake.plant(SERVICE, LIBRARY_KEY_ACCOUNT, "c" * 64, keychain=base / "other.keychain-db")
    client = fake.client()
    assert not client.has_generic_password(service=SERVICE, account=LIBRARY_KEY_ACCOUNT)
    failure(
        "STORAGE.NOT_BOUND",
        lambda: client.find_generic_password(service=SERVICE, account=LIBRARY_KEY_ACCOUNT),
    )


def test_a_missing_item_is_not_bound_and_named(fake: FakeSecurity) -> None:
    error = failure(
        "STORAGE.NOT_BOUND",
        lambda: fake.client().find_generic_password(service=SERVICE, account="someone"),
    )
    assert SERVICE in str(error) and "someone" in str(error) and str(fake.keychain) in str(error)
    assert "recovery sheet" in str(error) and "binding the vault again" in str(error)
    assert error.__context__ is None
    assert KEYCHAIN_ITEM_NOT_FOUND_STATUS == 44


@pytest.mark.parametrize(
    ("command", "call"),
    [
        ("look", lambda client: client.has_generic_password(service=SERVICE, account="a")),
        ("read", lambda client: client.find_generic_password(service=SERVICE, account="a")),
        (
            "add",
            lambda client: client.add_generic_password(
                service=SERVICE, account="a", label="l", secret="d" * 64
            ),
        ),
    ],
)
@pytest.mark.parametrize("status", [1, 7, 45, 51])
def test_any_other_exit_status_is_a_protocol_failure(
    fake: FakeSecurity, command: str, call: Callable[[SecurityCliKeychain], object], status: int
) -> None:
    fake.behave(command, exit=status, stdout="d" * 64 + "\n")
    error = failure("STORAGE.PROTOCOL", lambda: call(fake.client()))
    assert f"exited with status {status}" in str(error) and SERVICE in str(error)
    assert str(fake.tool) in str(error)
    assert_secret_absent("d" * 64, *texts_of(error))
    assert error.__context__ is None and error.__cause__ is None


@pytest.mark.parametrize(
    "output",
    [
        "",
        "\n",
        "no-newline",
        "two\nlines\n",
        "with space\n",
        "tab\there\n",
        "caf\u00e9\n",
        "x" * 1025 + "\n",
        "value\r\n",
    ],
    ids=["empty", "blank", "unterminated", "two-lines", "space", "tab", "latin", "long", "crlf"],
)
def test_an_answer_that_is_not_one_plain_secret_is_a_protocol_failure(
    fake: FakeSecurity, output: str
) -> None:
    fake.behave("read", stdout=output)
    error = failure(
        "STORAGE.PROTOCOL",
        lambda: fake.client().find_generic_password(service=SERVICE, account="a"),
        match="answered with something other than one plain secret",
    )
    for text in texts_of(error):
        assert output.strip() == "" or output.strip() not in text


def test_the_longest_plain_secret_is_read_back(fake: FakeSecurity) -> None:
    fake.plant(SERVICE, "a", "Z" * 1024)
    assert fake.client().find_generic_password(service=SERVICE, account="a") == "Z" * 1024


@pytest.mark.parametrize("command", ["look", "read", "add"])
def test_a_keychain_tool_that_hangs_times_out_without_leaking(
    fake: FakeSecurity, command: str
) -> None:
    fake.behave(command, sleep=30)
    client = fake.client(timeout=0.5)
    calls = {
        "look": lambda: client.has_generic_password(service=SERVICE, account="a"),
        "read": lambda: client.find_generic_password(service=SERVICE, account="a"),
        "add": lambda: client.add_generic_password(
            service=SERVICE, account="a", label="l", secret="e" * 64
        ),
    }
    if command == "add":
        # The lookup before the add answers "missing" at once.
        fake.behave("look", exit=44)
    error = failure("STORAGE.PROTOCOL", calls[command], match=r"did not finish within 0\.5 s while")
    assert error.__context__ is None and error.__cause__ is None
    assert_secret_absent("e" * 64, *texts_of(error))


def test_a_keychain_tool_that_cannot_start_is_a_protocol_failure(base: Path) -> None:
    missing = SecurityCliKeychain(security=base / "absent" / "security", keychain=base / "k")
    error = failure(
        "STORAGE.PROTOCOL",
        lambda: missing.has_generic_password(service=SERVICE, account="a"),
        match=r"could not be started \(No such file or directory\)",
    )
    assert error.__context__ is None
    unexecutable = base / "security"
    unexecutable.write_text("#!/bin/sh\nexit 0\n")
    unexecutable.chmod(0o600)
    client = SecurityCliKeychain(security=unexecutable, keychain=base / "k")
    failure(
        "STORAGE.PROTOCOL",
        lambda: client.find_generic_password(service=SERVICE, account="a"),
        match=r"could not be started \(Permission denied\)",
    )


def test_a_start_failure_without_an_error_text_is_named_by_its_kind(
    fake: FakeSecurity, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_arguments: object, **_options: object) -> None:
        raise ChildProcessError()

    monkeypatch.setattr(subprocess, "run", refuse)
    failure(
        "STORAGE.PROTOCOL",
        lambda: fake.client().has_generic_password(service=SERVICE, account="a"),
        match=r"could not be started \(ChildProcessError\)",
    )


def test_an_add_that_the_keychain_did_not_keep_is_a_protocol_failure(
    fake: FakeSecurity, launches: Launches
) -> None:
    fake.behave("add", ignore=True)
    error = failure(
        "STORAGE.PROTOCOL",
        lambda: fake.client().add_generic_password(
            service=SERVICE, account="a", label="l", secret="f" * 64
        ),
        match="kept no item",
    )
    assert error.__context__ is None
    assert_secret_absent("f" * 64, *texts_of(error))
    assert_disciplined(fake, launches, "f" * 64)


def test_an_add_that_kept_another_secret_is_a_protocol_failure(fake: FakeSecurity) -> None:
    fake.behave("add", replace="0" * 64)
    error = failure(
        "STORAGE.PROTOCOL",
        lambda: fake.client().add_generic_password(
            service=SERVICE, account="a", label="l", secret="1" * 64
        ),
        match="holds a different secret",
    )
    assert_secret_absent("1" * 64, *texts_of(error))
    assert_secret_absent("0" * 64, *texts_of(error))


def test_an_add_whose_read_back_fails_reports_that_failure(fake: FakeSecurity) -> None:
    fake.behave("read", exit=9)
    failure(
        "STORAGE.PROTOCOL",
        lambda: fake.client().add_generic_password(
            service=SERVICE, account="a", label="l", secret="2" * 64
        ),
        match="exited with status 9 while reading Keychain item",
    )
    assert f"{SERVICE}|a" in fake.items()


@pytest.mark.parametrize(
    ("words", "problem"),
    [
        ({"service": "has space"}, "service"),
        ({"service": ""}, "service"),
        ({"service": "a" * 256}, "service"),
        ({"account": "quote'd"}, "account"),
        ({"account": "semi;colon"}, "account"),
        ({"label": "new\nline"}, "label"),
        ({"label": "-l x -A"}, "label"),
    ],
)
def test_a_word_that_would_need_quoting_is_refused_before_the_tool_runs(
    fake: FakeSecurity, launches: Launches, words: dict[str, str], problem: str
) -> None:
    client = fake.client()
    item = {"service": SERVICE, "account": "a"} | {
        name: value for name, value in words.items() if name != "label"
    }
    added = item | {"label": words.get("label", "l"), "secret": "3" * 64}
    with pytest.raises(ValueError, match=f"a Keychain {problem} is"):
        client.add_generic_password(**added)
    if problem != "label":
        with pytest.raises(ValueError, match=f"a Keychain {problem} is"):
            client.has_generic_password(**item)
        with pytest.raises(ValueError, match=f"a Keychain {problem} is"):
            client.find_generic_password(**item)
    assert launches.runs == []


@pytest.mark.parametrize(
    "secret",
    ["", "has space", "x" * 1025, "new\nline", "quote'", b"4" * 64],
    ids=["empty", "space", "long", "newline", "quote", "bytes"],
)
def test_a_secret_that_would_need_quoting_is_refused_without_showing_it(
    fake: FakeSecurity, launches: Launches, secret: Any
) -> None:
    with pytest.raises(ValueError, match="a Keychain secret is") as caught:
        fake.client().add_generic_password(service=SERVICE, account="a", label="l", secret=secret)
    if secret:
        assert_secret_absent(secret if isinstance(secret, str) else "4" * 64, str(caught.value))
    assert launches.runs == []


# -- the library key's lifecycle ----------------------------------------------------------


class MemoryKeychain:
    """A ``KeychainClient`` in memory; ``keep`` replaces what an add stores."""

    def __init__(self, *, keep: str | None = None, find_failure: StorageError | None = None):
        self.items: dict[tuple[str, str], str] = {}
        self.calls: list[str] = []
        self.keep = keep
        self.find_failure = find_failure

    def add_generic_password(self, *, service: str, account: str, label: str, secret: str) -> None:
        self.calls.append("add")
        self.items[(service, account)] = secret if self.keep is None else self.keep

    def find_generic_password(self, *, service: str, account: str) -> str:
        self.calls.append("find")
        if self.find_failure is not None:
            raise self.find_failure
        if (service, account) not in self.items:
            raise storage_error("STORAGE.NOT_BOUND", f"no item {service}")
        return self.items[(service, account)]

    def has_generic_password(self, *, service: str, account: str) -> bool:
        self.calls.append("has")
        return (service, account) in self.items


def test_a_created_key_lives_in_the_keychain_and_loads_back(
    fake: FakeSecurity, launches: Launches
) -> None:
    client = fake.client()
    created = create_library_key(client, LIBRARY)
    stored = fake.items()[f"{SERVICE}|{LIBRARY_KEY_ACCOUNT}"]
    assert stored["label"] == SERVICE == f"private.cole.voice-lab.vault.{LIBRARY}.key"
    assert LibraryKey(bytes.fromhex(stored["secret"])) == created
    assert load_library_key(client, LIBRARY) == created
    assert_disciplined(fake, launches, stored["secret"])
    assert create_library_key(MemoryKeychain(), LIBRARY) != created


def test_a_second_key_for_a_library_is_refused_and_changes_nothing(fake: FakeSecurity) -> None:
    client = fake.client()
    first = create_library_key(client, LIBRARY)
    before = fake.items()
    calls = len(fake.calls())
    error = failure(
        "STORAGE.CONFLICT",
        lambda: create_library_key(client, LIBRARY),
        match="already holds the library key item .* never replaced",
    )
    assert fake.items() == before and len(fake.calls()) == calls + 1
    assert_secret_absent(before[f"{SERVICE}|{LIBRARY_KEY_ACCOUNT}"]["secret"], *texts_of(error))
    assert load_library_key(client, LIBRARY) == first
    # A key for another library is its own item.
    assert create_library_key(client, OTHER_LIBRARY) != first


def test_a_missing_key_names_its_item_and_the_recovery_sheet(fake: FakeSecurity) -> None:
    error = failure(
        "STORAGE.NOT_BOUND",
        lambda: load_library_key(fake.client(), LIBRARY),
        match="restore it from the library's recovery sheet",
    )
    assert SERVICE in str(error) and LIBRARY in str(error)
    assert error.__context__ is None and error.__cause__ is None


def test_a_keychain_failure_while_loading_is_reported_as_it_is() -> None:
    broken = storage_error("STORAGE.PROTOCOL", "the Keychain tool failed")
    with pytest.raises(StorageError) as caught:
        load_library_key(MemoryKeychain(find_failure=broken), LIBRARY)
    assert caught.value is broken


@pytest.mark.parametrize(
    "content",
    ["A" * 64, "a" * 63, "a" * 65, "g" * 64, "0x" + "a" * 62, "secret"],
    ids=["upper", "short", "long", "not-hex", "prefixed", "word"],
)
def test_an_item_that_does_not_hold_a_key_is_refused(fake: FakeSecurity, content: str) -> None:
    fake.plant(SERVICE, LIBRARY_KEY_ACCOUNT, content)
    error = failure(
        "STORAGE.INTEGRITY",
        lambda: load_library_key(fake.client(), LIBRARY),
        match="does not hold a library key",
        integrity=True,
    )
    assert_secret_absent(content, *texts_of(error))


def test_a_key_the_keychain_did_not_keep_is_a_protocol_failure() -> None:
    keychain = MemoryKeychain(keep="5" * 64)
    error = failure(
        "STORAGE.PROTOCOL",
        lambda: create_library_key(keychain, LIBRARY),
        match="does not hold the key that was just written",
    )
    assert_secret_absent("5" * 64, *texts_of(error))
    assert keychain.calls == ["has", "add", "find"]


def test_a_key_is_never_kept_for_an_invalid_library_id() -> None:
    keychain = MemoryKeychain()
    for action in (
        lambda: create_library_key(keychain, "Bad Library"),
        lambda: load_library_key(keychain, "bad/library"),
        lambda: import_library_key(keychain, "", render_recovery_sheet(LIBRARY, key(), CREATED)),
    ):
        with pytest.raises(StorageNameError):
            action()
    assert keychain.calls == []


def test_a_recovery_sheet_restores_the_key_into_an_empty_keychain(
    fake: FakeSecurity, launches: Launches
) -> None:
    client = fake.client()
    sheet = render_recovery_sheet(LIBRARY, key(), CREATED)
    assert import_library_key(client, LIBRARY, sheet) == key()
    assert load_library_key(client, LIBRARY) == key()
    assert_disciplined(fake, launches, KEY_BYTES.hex())
    error = failure("STORAGE.CONFLICT", lambda: import_library_key(client, LIBRARY, sheet))
    assert_secret_absent(KEY_BYTES, *texts_of(error))


def test_a_bad_sheet_is_refused_before_the_keychain_is_touched() -> None:
    keychain = MemoryKeychain()
    sheet = render_recovery_sheet(OTHER_LIBRARY, key(), CREATED)
    failure(
        "STORAGE.INTEGRITY",
        lambda: import_library_key(keychain, LIBRARY, sheet),
        integrity=True,
    )
    assert keychain.calls == []


# -- the recovery sheet -------------------------------------------------------------------


def test_a_recovery_sheet_names_the_library_the_key_and_its_checks() -> None:
    sheet = render_recovery_sheet(
        LIBRARY, key(), datetime(2026, 9, 17, 5, 30, 45, tzinfo=timezone(timedelta(hours=-7)))
    )
    lines = sheet.splitlines()
    assert lines[0] == "COLE VOICE LAB - LIBRARY VAULT KEY RECOVERY SHEET"
    assert f"Format: {RECOVERY_SHEET_FORMAT}" in lines
    assert f"Library id: {LIBRARY}" in lines
    assert "Created: 2026-09-17T12:30:45Z" in lines
    assert f"Key fingerprint: {key().fingerprint()}" in lines
    checksum = hashlib.sha256(
        b"cole-voice-lab.library-key-checksum/v1\x00" + LIBRARY.encode() + KEY_BYTES
    ).hexdigest()[:8]
    assert library_key_checksum(LIBRARY, key()) == checksum
    assert f"Checksum: {checksum}" in lines
    start = lines.index("Key:") + 1
    rows = lines[start : start + 4]
    assert all(row.startswith("  ") and len(row.split()) == 4 for row in rows)
    assert all(len(group) == 4 for row in rows for group in row.split())
    assert "".join("".join(rows).split()) == KEY_BYTES.hex()
    assert lines[start + 4] == f"Checksum: {checksum}"
    assert "Keep it offline" in sheet and "To restore" in sheet
    assert sheet.endswith("\n") and sheet.isascii()
    assert parse_recovery_sheet(sheet, LIBRARY) == key()


def test_a_recovery_sheet_needs_an_aware_time_and_a_valid_library() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        render_recovery_sheet(LIBRARY, key(), datetime(2026, 9, 17, 12, 0))
    with pytest.raises(StorageNameError):
        render_recovery_sheet("Bad", key(), CREATED)
    with pytest.raises(StorageNameError):
        library_key_checksum("Bad", key())
    with pytest.raises(StorageNameError):
        parse_recovery_sheet(render_recovery_sheet(LIBRARY, key(), CREATED), "Bad")


def retyped(sheet: str, change: Callable[[list[str]], list[str]]) -> str:
    return "\n".join(change(sheet.splitlines())) + "\n"


def key_rows(lines: list[str]) -> tuple[int, int]:
    start = lines.index("Key:")
    return start, next(index for index, line in enumerate(lines) if line.startswith("Checksum"))


def uppercase_groups(lines: list[str]) -> list[str]:
    start, end = key_rows(lines)
    return lines[: start + 1] + [row.upper() for row in lines[start + 1 : end]] + lines[end:]


def regrouped(lines: list[str]) -> list[str]:
    start, end = key_rows(lines)
    digits = "".join("".join(lines[start + 1 : end]).split())
    rows = [f"    {digits[index : index + 8]}   " for index in range(0, 64, 8)]
    return lines[: start + 1] + rows[:3] + ["", "\t"] + rows[3:] + lines[end:]


def digits_on_the_key_line(lines: list[str]) -> list[str]:
    start, end = key_rows(lines)
    digits = " ".join(lines[start + 1 : end])
    return lines[:start] + [f"  Key: {digits}"] + lines[end:]


def upper_checks(lines: list[str]) -> list[str]:
    return [
        line.replace(line.split(": ")[1], line.split(": ")[1].upper())
        if line.startswith(("Checksum:", "Key fingerprint:"))
        else line
        for line in lines
    ]


@pytest.mark.parametrize(
    "change",
    [uppercase_groups, regrouped, digits_on_the_key_line, upper_checks],
    ids=lambda change: change.__name__,
)
def test_a_retyped_sheet_reads_back(change: Callable[[list[str]], list[str]]) -> None:
    sheet = retyped(render_recovery_sheet(LIBRARY, key(), CREATED), change)
    assert parse_recovery_sheet(sheet, LIBRARY) == key()
    windows = sheet.replace("\n", "\r\n")
    assert parse_recovery_sheet(windows, LIBRARY) == key()


def mistyped_group(lines: list[str]) -> list[str]:
    start, _ = key_rows(lines)
    row = lines[start + 2]
    changed = row[:3] + ("0" if row[3] != "0" else "1") + row[4:]
    return lines[: start + 2] + [changed] + lines[start + 3 :]


def missing_group(lines: list[str]) -> list[str]:
    start, _ = key_rows(lines)
    return lines[: start + 1] + [lines[start + 1].rsplit(" ", 1)[0]] + lines[start + 2 :]


def extra_group(lines: list[str]) -> list[str]:
    start, _ = key_rows(lines)
    return lines[: start + 1] + [lines[start + 1] + " abcd"] + lines[start + 2 :]


def non_hex_group(lines: list[str]) -> list[str]:
    start, _ = key_rows(lines)
    return lines[: start + 1] + ["  zzzz" + lines[start + 1][6:]] + lines[start + 2 :]


def checksum_before_key(lines: list[str]) -> list[str]:
    start, end = key_rows(lines)
    return lines[:start] + [lines[end]] + lines[start:end] + lines[end + 1 :]


def other_fingerprint(lines: list[str]) -> list[str]:
    return [
        f"Key fingerprint: {other_key().fingerprint()}"
        if line.startswith("Key fingerprint")
        else line
        for line in lines
    ]


def without(prefix: str) -> Callable[[list[str]], list[str]]:
    def change(lines: list[str]) -> list[str]:
        return [line for line in lines if not line.startswith(prefix)]

    change.__name__ = f"without {prefix}"
    return change


def doubled(prefix: str) -> Callable[[list[str]], list[str]]:
    def change(lines: list[str]) -> list[str]:
        return lines + [line for line in lines if line.startswith(prefix)]

    change.__name__ = f"doubled {prefix}"
    return change


def replaced(prefix: str, value: str) -> Callable[[list[str]], list[str]]:
    def change(lines: list[str]) -> list[str]:
        return [f"{prefix} {value}" if line.startswith(prefix) else line for line in lines]

    change.__name__ = f"{prefix} {value}"
    return change


@pytest.mark.parametrize(
    ("change", "match"),
    [
        (mistyped_group, "checksum does not match its key"),
        (missing_group, "key is not 16 groups"),
        (extra_group, "key is not 16 groups"),
        (non_hex_group, "key is not 16 groups"),
        (checksum_before_key, "key is not 16 groups"),
        (other_fingerprint, "fingerprint does not match"),
        (replaced("Checksum:", "00000000"), "checksum does not match"),
        (replaced("Library id:", OTHER_LIBRARY), f"is for library '{OTHER_LIBRARY}', not"),
        (replaced("Library id:", LIBRARY.upper()), "is for library"),
        (replaced("Format:", "cole-voice-lab.library-key-recovery/v2"), "not in format"),
        (without("Format:"), "exactly one 'Format:' line, not 0"),
        (doubled("Format:"), "exactly one 'Format:' line, not 2"),
        (without("Library id:"), "exactly one 'Library id:' line"),
        (without("Key fingerprint:"), "exactly one 'Key fingerprint:' line"),
        (without("Checksum:"), "exactly one 'Checksum:' line"),
        (without("Key:"), "exactly one 'Key:' line"),
        (doubled("Key:"), "exactly one 'Key:' line, not 2"),
        (lambda lines: lines + ["x" * 70_000], "at most 65536 characters"),
    ],
    ids=lambda value: getattr(value, "__name__", None) or value,
)
def test_a_damaged_sheet_is_refused_without_showing_the_key(
    change: Callable[[list[str]], list[str]], match: str
) -> None:
    sheet = retyped(render_recovery_sheet(LIBRARY, key(), CREATED), change)
    error = failure(
        "STORAGE.INTEGRITY",
        lambda: parse_recovery_sheet(sheet, LIBRARY),
        match=match,
        integrity=True,
    )
    assert_secret_absent(KEY_BYTES, *texts_of(error))


def test_a_sheet_naming_another_library_does_not_echo_unbounded_text() -> None:
    sheet = retyped(
        render_recovery_sheet(LIBRARY, key(), CREATED), replaced("Library id:", "y" * 500)
    )
    error = failure(
        "STORAGE.INTEGRITY", lambda: parse_recovery_sheet(sheet, LIBRARY), integrity=True
    )
    assert "y" * 100 not in str(error)


# -- writing the recovery sheet -----------------------------------------------------------


@pytest.fixture
def sheet() -> str:
    return render_recovery_sheet(LIBRARY, key(), CREATED)


@pytest.fixture
def roots(base: Path) -> tuple[Path, Path, Path]:
    """A library, a repository and a private destination directory, each 0700."""
    made = (base / "library-root", base / "repository", base / "offline")
    for directory in made:
        directory.mkdir(mode=0o700)
    return made


def test_a_sheet_is_written_to_a_new_private_file(
    roots: tuple[Path, Path, Path], sheet: str
) -> None:
    library, repository, offline = roots
    target = offline / "vault-key.txt"
    write_recovery_sheet(target, sheet, forbidden_roots=(library, repository))
    metadata = target.lstat()
    assert stat.S_ISREG(metadata.st_mode) and stat.S_IMODE(metadata.st_mode) == 0o600
    assert metadata.st_nlink == 1 and metadata.st_uid == os.getuid()
    assert target.read_text(encoding="utf-8") == sheet
    assert sorted(os.listdir(offline)) == ["vault-key.txt"]
    assert parse_recovery_sheet(target.read_text(encoding="utf-8"), LIBRARY) == key()


def test_a_sheet_never_replaces_anything(roots: tuple[Path, Path, Path], sheet: str) -> None:
    library, _, offline = roots
    existing = offline / "existing.txt"
    existing.write_text("keep me")
    dangling = offline / "dangling.txt"
    dangling.symlink_to(offline / "nowhere.txt")
    directory = offline / "directory"
    directory.mkdir()
    for target in (existing, dangling, directory):
        failure(
            "STORAGE.CONFLICT",
            lambda target=target: write_recovery_sheet(target, sheet, forbidden_roots=(library,)),
            match="already exists there",
        )
    assert existing.read_text() == "keep me"
    assert dangling.is_symlink() and not (offline / "nowhere.txt").exists()
    assert sorted(os.listdir(offline)) == ["dangling.txt", "directory", "existing.txt"]


def nfd(path: Path) -> Path:
    return Path(unicodedata.normalize("NFD", os.fspath(path)))


@macos_only.CASE_INSENSITIVE_VOLUME
def test_a_sheet_is_never_written_inside_a_forbidden_root(
    base: Path, roots: tuple[Path, Path, Path], sheet: str
) -> None:
    library, repository, offline = roots
    (library / "provenance").mkdir(mode=0o700)
    linked = offline / "linked-library"
    linked.symlink_to(library / "provenance")
    root_link = base / "repository-link"
    root_link.symlink_to(repository)
    accented = base / "caf\u00e9-library"
    accented.mkdir(mode=0o700)
    (accented / "inner").mkdir(mode=0o700)
    cases = [
        (library / "sheet.txt", (library,)),
        (library / "provenance" / "sheet.txt", (repository, library)),
        (linked / "sheet.txt", (library,)),
        (repository / "sheet.txt", (root_link,)),
        (Path(os.fspath(library).upper()) / "sheet.txt", (library,)),
        # APFS names are normalization-blind: another spelling is the same directory, which
        # the identity check finds where the path comparison cannot.
        (accented / "sheet.txt", (nfd(accented),)),
        (accented / "inner" / "sheet.txt", (nfd(accented),)),
    ]
    for target, forbidden in cases:
        error = failure(
            "STORAGE.INTEGRITY",
            lambda target=target, forbidden=forbidden: write_recovery_sheet(
                target, sheet, forbidden_roots=forbidden
            ),
            match="is never written inside",
            integrity=True,
        )
        assert os.fspath(target) in str(error)
        assert not target.exists()
    assert_secret_absent(KEY_BYTES, *texts_of(error))
    assert os.listdir(library) == ["provenance"] and os.listdir(repository) == []


def test_a_forbidden_root_that_does_not_exist_holds_nothing(
    base: Path, roots: tuple[Path, Path, Path], sheet: str
) -> None:
    _, _, offline = roots
    write_recovery_sheet(
        offline / "sheet.txt", sheet, forbidden_roots=(base / "not-created", base / "off")
    )
    assert (offline / "sheet.txt").read_text() == sheet


def test_a_sheet_goes_only_into_an_existing_directory_the_user_owns(
    base: Path, roots: tuple[Path, Path, Path], sheet: str
) -> None:
    library, _, offline = roots
    plain_file = offline / "plain-file"
    plain_file.write_text("not a directory")
    for target in (
        base / "missing" / "sheet.txt",
        plain_file / "sheet.txt",
        # Owned by root; nothing is attempted there.
        Path("/cole-voice-lab-recovery-sheet-test.txt"),
    ):
        failure(
            "STORAGE.INTEGRITY",
            lambda target=target: write_recovery_sheet(target, sheet, forbidden_roots=(library,)),
            match="existing directory owned by you",
            integrity=True,
        )
    assert not Path("/cole-voice-lab-recovery-sheet-test.txt").exists()


@pytest.mark.parametrize(
    ("target", "roots_given", "text", "match"),
    [
        ("relative/sheet.txt", True, True, "absolute, normalized file path"),
        ("{offline}/../offline/sheet.txt", True, True, "absolute, normalized file path"),
        ("/", True, True, "absolute, normalized file path"),
        ("{offline}/sheet.txt", False, True, "the roots it must stay out of"),
        ("{offline}/sheet.txt", True, False, "only a rendered recovery sheet"),
    ],
)
def test_a_sheet_write_refuses_bad_arguments_before_any_io(
    roots: tuple[Path, Path, Path],
    sheet: str,
    target: str,
    roots_given: bool,
    text: bool,
    match: str,
) -> None:
    library, _, offline = roots
    with pytest.raises(ValueError, match=match):
        write_recovery_sheet(
            Path(target.format(offline=offline)),
            sheet if text else f"{sheet}".replace("Format:", "Formatted:"),
            forbidden_roots=(library,) if roots_given else (),
        )
    assert os.listdir(offline) == []


@pytest.mark.parametrize(
    ("failing", "error", "code", "match"),
    [
        ("write", OSError(errno.ENOSPC, "No space left on device"), "STORAGE.QUOTA", "No space"),
        ("file-sync", OSError(errno.EDQUOT, "Disc quota exceeded"), "STORAGE.QUOTA", "quota"),
        ("file-sync", OSError(errno.EIO, "Input/output error"), "STORAGE.UNAVAILABLE", "Input"),
        ("directory-sync", OSError(errno.EIO, "Input/output error"), "STORAGE.UNAVAILABLE", "I"),
        ("chmod", PermissionError(), "STORAGE.UNAVAILABLE", r"\(PermissionError\)"),
    ],
)
def test_a_sheet_that_could_not_be_written_is_removed(
    monkeypatch: pytest.MonkeyPatch,
    roots: tuple[Path, Path, Path],
    sheet: str,
    failing: str,
    error: OSError,
    code: str,
    match: str,
) -> None:
    library, _, offline = roots
    real_fsync = os.fsync
    real_fdopen = os.fdopen
    synced: list[int] = []

    def fsync(descriptor: int) -> None:
        synced.append(descriptor)
        is_directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
        if (failing == "file-sync" and not is_directory) or (
            failing == "directory-sync" and is_directory
        ):
            raise error
        real_fsync(descriptor)

    class FailingStream:
        def __init__(self, stream: Any) -> None:
            self.stream = stream

        def __enter__(self) -> FailingStream:
            return self

        def __exit__(self, *_details: object) -> None:
            self.stream.close()

        def write(self, data: bytes) -> int:
            self.stream.write(data[:10])
            self.stream.flush()
            raise error

    def fdopen(descriptor: int, *arguments: Any, **options: Any) -> Any:
        stream = real_fdopen(descriptor, *arguments, **options)
        # Only the sheet writer opens a borrowed descriptor.
        return FailingStream(stream) if options.get("closefd") is False else stream

    def fchmod(descriptor: int, mode: int) -> None:
        raise error

    monkeypatch.setattr(storage_sealing.os, "fsync", fsync)
    if failing == "write":
        monkeypatch.setattr(storage_sealing.os, "fdopen", fdopen)
    if failing == "chmod":
        monkeypatch.setattr(storage_sealing.os, "fchmod", fchmod)
    target = offline / "sheet.txt"
    raised = failure(
        code,
        lambda: write_recovery_sheet(target, sheet, forbidden_roots=(library,)),
        match=f"was not written to {re.escape(str(target))}: the filesystem refused it .*{match}",
    )
    assert raised.__context__ is None
    assert os.listdir(offline) == []


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (OSError(errno.EIO, "Input/output error"), "STORAGE.UNAVAILABLE"),
        (OSError(errno.ENOSPC, "No space left on device"), "STORAGE.QUOTA"),
    ],
)
def test_a_sheet_that_could_not_be_removed_is_reported_as_left_behind(
    monkeypatch: pytest.MonkeyPatch,
    roots: tuple[Path, Path, Path],
    sheet: str,
    error: OSError,
    code: str,
) -> None:
    library, _, offline = roots
    target = offline / "sheet.txt"

    def fsync(descriptor: int) -> None:
        raise error

    def unlink(*_arguments: object, **_options: object) -> None:
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(storage_sealing.os, "fsync", fsync)
    monkeypatch.setattr(storage_sealing.os, "unlink", unlink)
    raised = failure(
        code,
        lambda: write_recovery_sheet(target, sheet, forbidden_roots=(library,)),
        match=(
            f"^the recovery sheet at {re.escape(str(target))} was not written completely: the "
            rf"filesystem refused it \({re.escape(str(error.strerror))}\); the file could not be "
            "removed and may hold the whole key, so delete it by hand before writing the sheet "
            "again$"
        ),
    )
    monkeypatch.undo()
    assert raised.__context__ is None
    assert_secret_absent(KEY_BYTES, *texts_of(raised))
    # The file the error warns about is really there, private, and holds the whole key.
    assert os.listdir(offline) == ["sheet.txt"]
    assert stat.S_IMODE(target.lstat().st_mode) == 0o600
    assert parse_recovery_sheet(target.read_text(encoding="utf-8"), LIBRARY) == key()


def test_a_sheet_removed_by_someone_else_is_not_reported_as_left_behind(
    monkeypatch: pytest.MonkeyPatch, roots: tuple[Path, Path, Path], sheet: str
) -> None:
    library, _, offline = roots
    target = offline / "sheet.txt"
    real_unlink = os.unlink

    def fsync(descriptor: int) -> None:
        raise OSError(errno.EIO, "Input/output error")

    def unlink(name: str, *, dir_fd: int) -> None:
        real_unlink(name, dir_fd=dir_fd)
        raise FileNotFoundError(errno.ENOENT, "No such file or directory")

    monkeypatch.setattr(storage_sealing.os, "fsync", fsync)
    monkeypatch.setattr(storage_sealing.os, "unlink", unlink)
    failure(
        "STORAGE.UNAVAILABLE",
        lambda: write_recovery_sheet(target, sheet, forbidden_roots=(library,)),
        match=(
            f"^the recovery sheet was not written to {re.escape(str(target))}: the filesystem "
            r"refused it \(Input/output error\)$"
        ),
    )
    assert os.listdir(offline) == []


@dataclass
class FailingSheetClose:
    """Fails ``os.close`` of the sheet's own descriptor (after releasing it, as close does)."""

    target: Path
    fail_sheet_sync: bool
    removable: bool
    opened: list[int] = field(default_factory=list)
    closed: list[int] = field(default_factory=list)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_open, real_close = os.open, os.close
        real_fsync, real_unlink = os.fsync, os.unlink

        def open_(path: Any, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
            descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
            if path == self.target.name:
                self.opened.append(descriptor)
            return descriptor

        def close(descriptor: int) -> None:
            self.closed.append(descriptor)
            real_close(descriptor)
            if descriptor in self.opened:
                raise OSError(errno.EIO, "Input/output error")

        def fsync(descriptor: int) -> None:
            if self.fail_sheet_sync and descriptor in self.opened:
                raise OSError(errno.ENOSPC, "No space left on device")
            real_fsync(descriptor)

        def unlink(name: str, *, dir_fd: int) -> None:
            if not self.removable:
                raise OSError(errno.EACCES, "Permission denied")
            real_unlink(name, dir_fd=dir_fd)

        monkeypatch.setattr(storage_sealing.os, "open", open_)
        monkeypatch.setattr(storage_sealing.os, "close", close)
        monkeypatch.setattr(storage_sealing.os, "fsync", fsync)
        monkeypatch.setattr(storage_sealing.os, "unlink", unlink)


@pytest.mark.parametrize("removable", [True, False])
@pytest.mark.parametrize(
    ("fail_sheet_sync", "code", "reason"),
    [
        # The sheet was written and synced completely; only closing it failed.
        (False, "STORAGE.UNAVAILABLE", "Input/output error"),
        # Closing failed while an earlier failure was on its way; the earlier one is reported.
        (True, "STORAGE.QUOTA", "No space left on device"),
    ],
)
def test_a_sheet_whose_close_fails_is_removed_or_reported_as_left_behind(
    monkeypatch: pytest.MonkeyPatch,
    roots: tuple[Path, Path, Path],
    sheet: str,
    fail_sheet_sync: bool,
    code: str,
    reason: str,
    removable: bool,
) -> None:
    library, _, offline = roots
    target = offline / "sheet.txt"
    failing = FailingSheetClose(target, fail_sheet_sync=fail_sheet_sync, removable=removable)
    failing.install(monkeypatch)
    raised = failure(code, lambda: write_recovery_sheet(target, sheet, forbidden_roots=(library,)))
    monkeypatch.undo()
    # The sheet's descriptor was closed exactly once, never again after close() failed.
    assert len(failing.opened) == 1
    assert failing.closed.count(failing.opened[0]) == 1
    assert raised.__context__ is None
    assert_secret_absent(KEY_BYTES, *texts_of(raised))
    location = re.escape(str(target))
    if removable:
        assert re.fullmatch(
            f"the recovery sheet was not written to {location}: the filesystem refused it "
            rf"\({reason}\)",
            str(raised),
        ), str(raised)
        assert os.listdir(offline) == []
    else:
        assert re.fullmatch(
            f"the recovery sheet at {location} was not written completely: the filesystem "
            rf"refused it \({reason}\); the file could not be removed and may hold the whole "
            "key, so delete it by hand before writing the sheet again",
            str(raised),
        ), str(raised)
        # The file the error warns about is really there and holds the whole key.
        assert os.listdir(offline) == ["sheet.txt"]
        assert stat.S_IMODE(target.lstat().st_mode) == 0o600
        assert parse_recovery_sheet(target.read_text(encoding="utf-8"), LIBRARY) == key()


@pytest.mark.parametrize("close_fails", [False, True])
@pytest.mark.parametrize("removable", [True, False])
def test_an_interrupted_sheet_write_names_a_file_it_could_not_remove(
    monkeypatch: pytest.MonkeyPatch,
    roots: tuple[Path, Path, Path],
    sheet: str,
    removable: bool,
    close_fails: bool,
) -> None:
    library, _, offline = roots
    target = offline / "sheet.txt"
    real_close = os.close
    failing = FailingSheetClose(target, fail_sheet_sync=False, removable=removable)
    failing.install(monkeypatch)

    def fsync(descriptor: int) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(storage_sealing.os, "fsync", fsync)
    if not close_fails:
        monkeypatch.setattr(storage_sealing.os, "close", real_close)
    with pytest.raises(KeyboardInterrupt) as caught:
        write_recovery_sheet(target, sheet, forbidden_roots=(library,))
    monkeypatch.undo()
    notes = getattr(caught.value, "__notes__", [])
    expected = ["closing the file also failed (Input/output error)"] if close_fails else []
    if removable:
        assert notes == expected
        assert os.listdir(offline) == []
    else:
        expected.append(f"an incompletely written file could not be removed: delete {target}")
        assert notes == expected
        assert os.listdir(offline) == ["sheet.txt"]


@dataclass
class FailingDirectoryClose:
    """Fails ``os.close`` of the sheet's parent-directory descriptor (after releasing it)."""

    directory: Path
    opened: list[int] = field(default_factory=list)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_open, real_close = os.open, os.close

        def open_(path: Any, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
            descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
            if dir_fd is None and path == os.path.realpath(self.directory):
                self.opened.append(descriptor)
            return descriptor

        def close(descriptor: int) -> None:
            real_close(descriptor)
            if descriptor in self.opened:
                raise OSError(errno.EIO, "Input/output error")

        monkeypatch.setattr(storage_sealing.os, "open", open_)
        monkeypatch.setattr(storage_sealing.os, "close", close)


def test_a_written_sheet_whose_directory_close_fails_is_reported_as_written(
    monkeypatch: pytest.MonkeyPatch, roots: tuple[Path, Path, Path], sheet: str
) -> None:
    library, _, offline = roots
    target = offline / "sheet.txt"
    failing = FailingDirectoryClose(offline)
    failing.install(monkeypatch)
    raised = failure(
        "STORAGE.UNAVAILABLE",
        lambda: write_recovery_sheet(target, sheet, forbidden_roots=(library,)),
        match=(
            f"^the recovery sheet was written completely to {re.escape(str(target))}, but "
            r"closing its directory then failed \(Input/output error\); check the file before "
            "relying on it$"
        ),
    )
    monkeypatch.undo()
    assert len(failing.opened) == 1
    assert raised.__context__ is None
    assert_secret_absent(KEY_BYTES, *texts_of(raised))
    assert parse_recovery_sheet(target.read_text(encoding="utf-8"), LIBRARY) == key()


def test_a_failed_sheet_keeps_its_leftover_warning_when_the_directory_close_fails(
    monkeypatch: pytest.MonkeyPatch, roots: tuple[Path, Path, Path], sheet: str
) -> None:
    library, _, offline = roots
    target = offline / "sheet.txt"
    FailingSheetClose(target, fail_sheet_sync=True, removable=False).install(monkeypatch)
    FailingDirectoryClose(offline).install(monkeypatch)
    raised = failure(
        "STORAGE.QUOTA",
        lambda: write_recovery_sheet(target, sheet, forbidden_roots=(library,)),
        match=(
            f"^the recovery sheet at {re.escape(str(target))} was not written completely: the "
            r"filesystem refused it \(No space left on device\); closing its directory also "
            r"failed \(Input/output error\); the file could not be removed and may hold the "
            "whole key, so delete it by hand before writing the sheet again$"
        ),
    )
    monkeypatch.undo()
    assert raised.__context__ is None
    assert_secret_absent(KEY_BYTES, *texts_of(raised))
    assert os.listdir(offline) == ["sheet.txt"]


def test_a_refused_sheet_keeps_its_refusal_when_the_directory_close_fails(
    monkeypatch: pytest.MonkeyPatch, roots: tuple[Path, Path, Path], sheet: str
) -> None:
    library, _, _ = roots
    target = library / "sheet.txt"
    FailingDirectoryClose(library).install(monkeypatch)
    raised = failure(
        "STORAGE.INTEGRITY",
        lambda: write_recovery_sheet(target, sheet, forbidden_roots=(library,)),
        match="a recovery sheet is never written inside",
        integrity=True,
    )
    monkeypatch.undo()
    assert getattr(raised, "__notes__", []) == ["closing the file also failed (Input/output error)"]
    assert not target.exists()
