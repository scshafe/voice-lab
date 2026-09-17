"""Storage ports and vault contracts: errors, names, the port shapes and every record.

Scenario tests for ``src/cole_voice_lab/storage_ports.py`` and
``src/cole_voice_lab/storage_contracts.py``. Contract payloads are validated in JSON mode (the
mode stored records and wire values arrive in), and each invalid case breaks exactly one
rule, which its ``match=`` names. Every fixture lives in memory.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import inspect
import json
import os
import pickle
import re
import subprocess
import sys
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import TypeAdapter, ValidationError

from cole_voice_lab import storage_contracts, storage_ports
from cole_voice_lab.canonical import canonical_json_bytes
from cole_voice_lab.errors import ContractError, SecurityError, VoiceLabError
from cole_voice_lab.models import ContractModel, StableId
from cole_voice_lab.schema import SCHEMA_MODELS, schema_bytes
from cole_voice_lab.storage_contracts import (
    EMPTY_SHA256,
    LIBRARY_ID_PATTERN,
    MANIFEST_PAYLOAD_DOMAIN,
    MAX_MANIFEST_BYTES,
    MAX_STORAGE_COUNTER,
    STORAGE_SCHEMA_MODELS,
    LedgerDestination,
    LedgerEntry,
    LibraryManifest,
    ManifestEntry,
    ManifestEnvelope,
    ManifestPlan,
    PlannedManifestEntry,
    ReplicationState,
    StorageBinding,
    UtcTimestamp,
    keychain_item_names,
    ledger_entry_digest,
    manifest_digest,
    manifest_path_key,
    manifest_payload_digest,
    manifest_plan,
)
from cole_voice_lab.storage_ports import (
    MAX_BLOB_BYTES,
    STORAGE_ERROR_CLASSES,
    STORAGE_RETRYABLE,
    BlobRef,
    BlobStore,
    RefStore,
    RefValue,
    StorageError,
    StorageErrorCode,
    StorageIntegrityError,
    StorageNameError,
    library_head_ref,
    storage_error,
    validate_library_id,
    validate_ref_name,
)

REPOSITORY = Path(__file__).resolve().parents[1]
SOURCE = REPOSITORY / "src" / "cole_voice_lab"
LIBRARY = "cole-self"
TIME = "2026-09-17T12:00:00Z"
FIXED = datetime(2026, 9, 17, 12, tzinfo=UTC)
HELLO_SHA256 = hashlib.sha256(b"hello").hexdigest()
NAMES = keychain_item_names(LIBRARY)
TAILNET_ENDPOINT = "voice-lab-vault.colobus-stargazer.ts.net"
LOCAL_ENDPOINT = "/Volumes/Library Copy/voice-lab-vault"
PATTERN = "String should match pattern"
EXTRA = "Extra inputs are not permitted"
TOO_LONG_REF = "/".join(("a" * 64, "b" * 64, "c" * 64, "d" * 62))  # 257 characters
LONGEST_REF = "/".join(("a" * 63, "b" * 64, "c" * 64, "d" * 62))  # 256 characters
LONGEST_DNS = ".".join(("a" * 63, "b" * 63, "c" * 63, "d" * 61))  # 253 characters

type Payload = dict[str, Any]
type Build = Callable[[], Payload]


def validate[M: ContractModel](model: type[M], payload: Payload) -> M:
    """Validate ``payload`` the way a stored record or a wire value arrives: as JSON."""
    return model.model_validate_json(json.dumps(payload))


# -- import closure -----------------------------------------------------------------------

STORAGE_MODULES = ("storage_contracts", "storage_ports")
ALLOWED_PACKAGE_MODULES = frozenset(
    {
        "cole_voice_lab",
        "cole_voice_lab.canonical",
        "cole_voice_lab.errors",
        "cole_voice_lab.models",
        "cole_voice_lab.paths",
        "cole_voice_lab.storage_contracts",
        "cole_voice_lab.storage_ports",
    }
)


def test_loading_the_storage_modules_loads_no_recording_catalog_or_schema_module(
    tmp_path: Path,
) -> None:
    probe = (
        "import sys\n"
        "import cole_voice_lab.storage_ports, cole_voice_lab.storage_contracts\n"
        "print('\\n'.join(sorted(name for name in sys.modules"
        " if name.partition('.')[0] == 'cole_voice_lab')))\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", probe],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    loaded = set(result.stdout.split())
    assert {"cole_voice_lab.storage_ports", "cole_voice_lab.storage_contracts"} <= loaded
    assert loaded <= ALLOWED_PACKAGE_MODULES, sorted(loaded - ALLOWED_PACKAGE_MODULES)


@pytest.mark.parametrize("name", STORAGE_MODULES)
def test_the_storage_modules_import_only_the_standard_library_pydantic_and_base_modules(
    name: str,
) -> None:
    allowed_package = {"errors", "models", "paths", "canonical", "storage_contracts"} - {name}
    allowed_external = set(sys.stdlib_module_names) | {"__future__", "pydantic"}
    tree = ast.parse((SOURCE / f"{name}.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            assert node.level == 1 and node.module in allowed_package, (node.lineno, node.module)
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").partition(".")[0] in allowed_external, node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.partition(".")[0] in allowed_external, alias.name
        elif isinstance(node, ast.Call):
            called = getattr(node.func, "id", getattr(node.func, "attr", None))
            assert called not in {"import_module", "__import__", "run_module"}, node.lineno


# -- errors -------------------------------------------------------------------------------

ALL_CODES: tuple[StorageErrorCode, ...] = get_args(StorageErrorCode.__value__)
INTEGRITY_CODES = {"STORAGE.UNAUTHORIZED", "STORAGE.INTEGRITY", "STORAGE.IDENTITY_MISMATCH"}
# The plan's twelve codes, spelled out: the server, the HTTP client, the CLI and the Swift
# twins send and match these exact strings.
PLANNED_CODES = {
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
}


def test_every_storage_code_has_one_class_and_one_retry_rule() -> None:
    assert len(ALL_CODES) == len(set(ALL_CODES)) == 12
    assert set(ALL_CODES) == PLANNED_CODES
    assert set(STORAGE_ERROR_CLASSES) == set(ALL_CODES) == set(STORAGE_RETRYABLE)
    retryable = {code for code, value in STORAGE_RETRYABLE.items() if value}
    assert retryable == {"STORAGE.UNAVAILABLE", "STORAGE.TIMEOUT"}
    integrity = {
        code for code, cls in STORAGE_ERROR_CLASSES.items() if cls is StorageIntegrityError
    }
    assert integrity == INTEGRITY_CODES
    assert set(STORAGE_ERROR_CLASSES.values()) == {StorageError, StorageIntegrityError}
    for code in ALL_CODES:
        assert re.fullmatch(r"STORAGE\.[A-Z][A-Z0-9_]*", code), code


@pytest.mark.parametrize("code", ALL_CODES)
def test_each_code_builds_its_own_class_and_survives_pickling(code: StorageErrorCode) -> None:
    error = storage_error(code, "probe message")

    assert type(error) is STORAGE_ERROR_CLASSES[code]
    assert isinstance(error, VoiceLabError)
    assert not isinstance(error, ContractError)
    assert isinstance(error, SecurityError) is (code in INTEGRITY_CODES)
    assert (error.code, error.retryable, str(error)) == (
        code,
        STORAGE_RETRYABLE[code],
        "probe message",
    )
    restored = pickle.loads(pickle.dumps(error))
    assert type(restored) is type(error)
    assert (restored.code, restored.retryable, str(restored)) == (
        code,
        error.retryable,
        "probe message",
    )


def test_a_class_refuses_a_code_that_belongs_to_the_other_class() -> None:
    with pytest.raises(ValueError, match="INTEGRITY is raised as StorageIntegrityError, not St"):
        StorageError("STORAGE.INTEGRITY", "digest mismatch")
    with pytest.raises(ValueError, match="CONFLICT is raised as StorageError, not StorageInt"):
        StorageIntegrityError("STORAGE.CONFLICT", "version moved")


@pytest.mark.parametrize("code", ["STORAGE.GONE", "CORPUS.STORE_MISSING", "storage.timeout", ""])
def test_an_unknown_code_is_refused_by_every_constructor(code: str) -> None:
    with pytest.raises(ValueError, match="unknown storage error code"):
        storage_error(code, "probe")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown storage error code"):
        StorageError(code, "probe")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown storage error code"):
        StorageIntegrityError(code, "probe")  # type: ignore[arg-type]


# -- ref names and library ids ------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "libraries/cole-self/head",
        "a",
        "0",
        "a/b/c/d/e/f/g/h",
        "a" * 64,
        "a..b/x_y.z-1",
        LONGEST_REF,
    ],
)
def test_a_valid_ref_name_is_returned_unchanged(name: str) -> None:
    assert validate_ref_name(name) == name
    assert validate(RefValue, ref_value() | {"name": name}).name == name


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("", id="empty"),
        pytest.param("/libraries/x", id="leading-slash"),
        pytest.param("libraries/x/", id="trailing-slash"),
        pytest.param("libraries//head", id="empty-segment"),
        pytest.param("../head", id="parent-first"),
        pytest.param("libraries/../head", id="parent-inside"),
        pytest.param("./head", id="dot-first"),
        pytest.param("libraries/.", id="dot-last"),
        pytest.param("libraries/.hidden", id="dot-leading-segment"),
        pytest.param("libraries/-x", id="dash-leading-segment"),
        pytest.param("Libraries/x", id="uppercase"),
        pytest.param("libraries/x y", id="space"),
        pytest.param("libraries/x\n", id="trailing-newline"),
        pytest.param("libraries\\x", id="backslash"),
        pytest.param("libraries/caf\u00e9", id="non-ascii"),
        pytest.param("a" * 65, id="segment-over-64"),
        pytest.param("a/b/c/d/e/f/g/h/i", id="nine-segments"),
        pytest.param(TOO_LONG_REF, id="over-256"),
    ],
)
def test_an_invalid_ref_name_is_refused_before_any_store_is_asked(name: str) -> None:
    with pytest.raises(StorageNameError, match="a ref name is 1-8") as caught:
        validate_ref_name(name)
    assert isinstance(caught.value, ContractError)
    assert isinstance(caught.value, ValueError)
    assert not isinstance(caught.value, StorageError)
    with pytest.raises(ValidationError, match=r"String should (match|have at most 256)"):
        validate(RefValue, ref_value() | {"name": name})


def test_a_library_head_ref_is_one_segment_under_libraries() -> None:
    longest = "a" + "b" * 62 + "c"

    assert library_head_ref(LIBRARY) == "libraries/cole-self/head"
    assert library_head_ref(longest) == f"libraries/{longest}/head"
    assert validate_ref_name(library_head_ref(longest))


@pytest.mark.parametrize(
    "library_id",
    ["", "Cole", "cole/self", "-cole", "cole-", "cole.", "..", "a" * 65, "cole self", "caf\u00e9"],
)
def test_a_library_id_that_cannot_be_one_ref_segment_is_refused(library_id: str) -> None:
    with pytest.raises(StorageNameError, match="a library id is a stable id"):
        validate_library_id(library_id)
    with pytest.raises(StorageNameError, match="a library id is a stable id"):
        library_head_ref(library_id)
    with pytest.raises(StorageNameError, match="a library id is a stable id"):
        keychain_item_names(library_id)


@pytest.mark.parametrize("library_id", ["cole-self", "0", "a.b_c-d", "a" * 64])
def test_every_library_id_is_a_stable_id(library_id: str) -> None:
    assert validate_library_id(library_id) == library_id
    assert TypeAdapter(StableId).validate_python(library_id) == library_id


def test_the_library_id_rule_is_the_stable_id_rule_cut_to_one_ref_segment() -> None:
    (stable,) = StableId.__value__.__metadata__
    assert stable.max_length == 128
    assert LIBRARY_ID_PATTERN == stable.pattern.replace("{0,126}", "{0,62}")


def test_keychain_item_names_are_fixed_by_the_library_id() -> None:
    names = keychain_item_names(LIBRARY)

    assert names == (
        "private.cole.voice-lab.vault.cole-self.key",
        "private.cole.voice-lab.vault.cole-self.token",
    )
    assert (names.key, names.token) == names
    assert keychain_item_names("cole-other") != names


# -- the ports ----------------------------------------------------------------------------


def public_members(protocol: type) -> set[str]:
    return {name for name in vars(protocol) if not name.startswith("_")}


def test_the_ports_have_no_list_or_delete_and_take_no_namespace() -> None:
    assert public_members(BlobStore) == {"put", "has", "get"}
    assert public_members(RefStore) == {"read", "swap"}
    for protocol in (BlobStore, RefStore):
        for member in public_members(protocol):
            assert "namespace" not in inspect.signature(getattr(protocol, member)).parameters
    swap = inspect.signature(RefStore.swap).parameters
    assert {swap["expected_version"].kind, swap["target"].kind} == {inspect.Parameter.KEYWORD_ONLY}


def test_one_blob_size_limit_and_one_set_of_name_rules_serve_ports_and_contracts() -> None:
    assert MAX_BLOB_BYTES == storage_contracts.MAX_BLOB_BYTES == 16 * 1024 * 1024
    assert BlobRef.model_json_schema()["properties"]["size"]["maximum"] == MAX_BLOB_BYTES
    for name in (
        "BlobRef",
        "RefValue",
        "StorageNameError",
        "library_head_ref",
        "validate_library_id",
        "validate_ref_name",
    ):
        assert getattr(storage_ports, name) is getattr(storage_contracts, name), name
    assert set(storage_ports.__all__) <= set(vars(storage_ports))


class InMemoryBlobStore:
    """A trivial ``BlobStore`` that keeps bytes as given, so equal bytes share one ref."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    def put(self, data: bytes) -> BlobRef:
        if len(data) > MAX_BLOB_BYTES:
            raise storage_error("STORAGE.TOO_LARGE", f"a blob holds at most {MAX_BLOB_BYTES}")
        ref = BlobRef(sha256=hashlib.sha256(data).hexdigest(), size=len(data))
        if self.blobs.setdefault(ref.sha256, bytes(data)) != data:
            raise storage_error("STORAGE.INTEGRITY", "the stored blob is corrupt")
        return ref

    def has(self, ref: BlobRef) -> bool:
        stored = self.blobs.get(ref.sha256)
        return stored is not None and len(stored) == ref.size

    def get(self, ref: BlobRef) -> bytes:
        stored = self.blobs.get(ref.sha256)
        if stored is None:
            raise storage_error("STORAGE.NOT_FOUND", "no blob has this ref")
        if len(stored) != ref.size or hashlib.sha256(stored).hexdigest() != ref.sha256:
            raise storage_error("STORAGE.INTEGRITY", "the stored blob does not match its ref")
        return stored


class InMemoryRefStore:
    """A trivial ``RefStore`` with compare-and-set versions and an injected clock."""

    def __init__(self, clock: Callable[[], datetime]) -> None:
        self.refs: dict[str, RefValue] = {}
        self.clock = clock

    def read(self, name: str) -> RefValue | None:
        return self.refs.get(validate_ref_name(name))

    def swap(self, name: str, *, expected_version: int | None, target: BlobRef) -> RefValue:
        current = self.refs.get(validate_ref_name(name))
        version = None if current is None else current.version
        if version != expected_version:
            raise storage_error("STORAGE.CONFLICT", f"{name} is at {version}")
        value = RefValue(
            name=name, version=(version or 0) + 1, target=target, updated_at=self.clock()
        )
        self.refs[name] = value
        return value


CONTRACTS_MODULE = "cole_voice_lab.storage_contracts"
# The ports' exact shapes, with annotations resolved: no namespace (under any name), no
# optional or extra parameters, and the planned return types.
PORT_SIGNATURES = {
    "BlobStore.put": f"(self, data: bytes) -> {CONTRACTS_MODULE}.BlobRef",
    "BlobStore.has": f"(self, ref: {CONTRACTS_MODULE}.BlobRef) -> bool",
    "BlobStore.get": f"(self, ref: {CONTRACTS_MODULE}.BlobRef) -> bytes",
    "RefStore.read": f"(self, name: str) -> {CONTRACTS_MODULE}.RefValue | None",
    "RefStore.swap": (
        f"(self, name: str, *, expected_version: int | None, target: {CONTRACTS_MODULE}.BlobRef)"
        f" -> {CONTRACTS_MODULE}.RefValue"
    ),
}


def resolved_signatures(cls: type) -> dict[str, inspect.Signature]:
    return {
        name: inspect.signature(getattr(cls, name), eval_str=True) for name in public_members(cls)
    }


def test_the_port_methods_have_exactly_the_planned_signatures() -> None:
    actual = {
        f"{port.__name__}.{name}": str(signature)
        for port in (BlobStore, RefStore)
        for name, signature in resolved_signatures(port).items()
    }
    assert actual == PORT_SIGNATURES


@pytest.mark.parametrize(
    ("fake", "port"),
    [(InMemoryBlobStore, BlobStore), (InMemoryRefStore, RefStore)],
    ids=["blobs", "refs"],
)
def test_the_in_memory_stores_have_the_port_signatures(fake: type, port: type) -> None:
    """The check that runs in pytest: mypy is not run over this file by any verify command."""
    assert resolved_signatures(fake) == resolved_signatures(port)


def publish_head(blobs: BlobStore, refs: RefStore, library_id: str, manifest: bytes) -> RefValue:
    """What a replicator does with the two ports: store a manifest, then move the head to it.

    Typed against the ports only. ``test_the_in_memory_stores_have_the_port_signatures``
    proves in pytest that the in-memory stores have the ports' exact signatures (mypy proves
    it too, but only when it is run over this file).
    """
    head = library_head_ref(library_id)
    current = refs.read(head)
    target = blobs.put(manifest)
    expected = None if current is None else current.version
    return refs.swap(head, expected_version=expected, target=target)


def test_in_memory_stores_satisfy_both_ports() -> None:
    ticks = iter(FIXED + timedelta(seconds=second) for second in range(10))
    blobs = InMemoryBlobStore()
    refs = InMemoryRefStore(ticks.__next__)

    first = publish_head(blobs, refs, LIBRARY, b"generation 1")
    second = publish_head(blobs, refs, LIBRARY, b"generation 2")

    assert (first.version, second.version) == (1, 2)
    assert second.updated_at > first.updated_at
    assert refs.read(library_head_ref(LIBRARY)) == second
    assert refs.read(library_head_ref("cole-other")) is None
    assert blobs.get(second.target) == b"generation 2"
    assert blobs.put(b"generation 2") == second.target
    assert len(blobs.blobs) == 2
    assert blobs.has(first.target)
    assert not blobs.has(BlobRef(sha256=EMPTY_SHA256, size=0))
    assert blobs.put(b"\x00" * MAX_BLOB_BYTES).size == MAX_BLOB_BYTES


def test_in_memory_stores_refuse_with_the_port_errors() -> None:
    blobs = InMemoryBlobStore()
    refs = InMemoryRefStore(lambda: FIXED)
    head = library_head_ref(LIBRARY)
    ref = blobs.put(b"hello")

    with pytest.raises(StorageError, match="at most") as too_large:
        blobs.put(b"\x00" * (MAX_BLOB_BYTES + 1))
    assert (too_large.value.code, too_large.value.retryable) == ("STORAGE.TOO_LARGE", False)
    with pytest.raises(StorageError, match="no blob") as missing:
        blobs.get(BlobRef(sha256=EMPTY_SHA256, size=0))
    assert missing.value.code == "STORAGE.NOT_FOUND"
    blobs.blobs[ref.sha256] = b"jello"
    with pytest.raises(StorageIntegrityError, match="does not match") as corrupt:
        blobs.get(ref)
    assert isinstance(corrupt.value, SecurityError)
    with pytest.raises(StorageIntegrityError, match="corrupt"):
        blobs.put(b"hello")
    refs.swap(head, expected_version=None, target=ref)
    for stale in (None, 2):
        with pytest.raises(StorageError, match="is at 1") as conflict:
            refs.swap(head, expected_version=stale, target=ref)
        assert conflict.value.code == "STORAGE.CONFLICT"
    with pytest.raises(StorageNameError):
        refs.read("Libraries/cole-self/head")


# -- contract fixtures --------------------------------------------------------------------


def blob_ref(**changes: object) -> Payload:
    return {"sha256": "a" * 64, "size": 44} | changes


def ref_value() -> Payload:
    return {
        "name": library_head_ref(LIBRARY),
        "version": 1,
        "target": blob_ref(),
        "updated_at": TIME,
    }


def file_entry() -> Payload:
    return {
        "path": "provenance/recording-takes/take-a/head.json",
        "file_class": "records",
        "kind": "file",
        "sha256": HELLO_SHA256,
        "size": 5,
        "mode": 0o600,
        "chunks": [blob_ref()],
        "pin_source": None,
    }


def entry(path: str, file_class: str = "records") -> Payload:
    return file_entry() | {"path": path, "file_class": file_class}


def empty_entry() -> Payload:
    return file_entry() | {
        "path": "provenance/recording-takes/take-a/reservation.json",
        "sha256": EMPTY_SHA256,
        "size": 0,
        "chunks": [],
    }


def pin_entry() -> Payload:
    return {
        "path": "derived/finetune/smoke-1/checkpoints/pretrained_model_1250000.safetensors",
        "file_class": "training",
        "kind": "pin",
        "sha256": "b" * 64,
        "size": 1_350_000_000,
        "mode": 0o400,
        "chunks": [],
        "pin_source": "finetune-run.runtime.pretrained_sha256",
    }


def database_entry() -> Payload:
    return entry("provenance/corpus/corpus.sqlite3", "prompt-database") | {
        "sha256": "c" * 64,
        "size": MAX_BLOB_BYTES + 1,
        "chunks": [blob_ref(size=MAX_BLOB_BYTES), blob_ref(sha256="d" * 64, size=29)],
    }


def manifest() -> Payload:
    return {
        "schema_version": "cole-voice-lab.library-manifest/v1",
        "library_id": LIBRARY,
        "generation": 2,
        "previous_manifest_sha256": "e" * 64,
        "created_at": TIME,
        "classes": ["prompt-database", "records", "training"],
        "entries": [pin_entry(), database_entry(), file_entry(), empty_entry()],
    }


def unchunked(payload: Payload) -> Payload:
    """The planned form of an entry or manifest payload, or of a change to one.

    It is a copy without ``schema_version`` and without ``chunks`` (on the payload itself and
    on each of its ``entries``): what is known before any chunk is stored.
    """
    planned = copy.deepcopy(payload)
    planned.pop("schema_version", None)
    for item in (planned, *planned.get("entries", ())):
        item.pop("chunks", None)
    return planned


def planned(build: Build) -> Build:
    return lambda: unchunked(build())


def planned_file_entry() -> Payload:
    return unchunked(file_entry())


def plan() -> Payload:
    return unchunked(manifest())


def envelope() -> Payload:
    return {
        "schema_version": "cole-voice-lab.library-manifest-envelope/v1",
        "library_id": LIBRARY,
        "generation": 2,
        "manifest_sha256": "e" * 64,
        "size": MAX_BLOB_BYTES + 29,
        "chunks": [blob_ref(size=MAX_BLOB_BYTES), blob_ref(sha256="d" * 64, size=62)],
    }


def tailnet_binding() -> Payload:
    return {
        "schema_version": "cole-voice-lab.storage-binding/v1",
        "backend": "tailnet-vault",
        "endpoint": TAILNET_ENDPOINT,
        "namespace": "cole",
        "library_id": LIBRARY,
        "classes": ["derived-edits", "prompt-database", "recordings", "records"],
        "approval_id": "vault-2026-09-17",
        "key_item": NAMES.key,
        "token_item": NAMES.token,
        "created_at": TIME,
    }


def local_binding() -> Payload:
    return tailnet_binding() | {
        "backend": "local-directory",
        "endpoint": LOCAL_ENDPOINT,
        "approval_id": None,
        "token_item": None,
    }


def fresh_state() -> Payload:
    return {
        "schema_version": "cole-voice-lab.replication-state/v1",
        "library_id": LIBRARY,
        "generation": 0,
        "manifest_sha256": None,
        "head": None,
        "updated_at": TIME,
    }


def pushed_state() -> Payload:
    return fresh_state() | {
        "generation": 3,
        "manifest_sha256": "f" * 64,
        "head": ref_value() | {"version": 3},
    }


def tailnet_destination() -> Payload:
    return {
        "backend": "tailnet-vault",
        "endpoint": TAILNET_ENDPOINT,
        "namespace": "cole",
        "node_stable_id": "nQRJBE7CNTRL",
        "tag": "tag:voice-lab-vault",
    }


def local_destination() -> Payload:
    return tailnet_destination() | {
        "backend": "local-directory",
        "endpoint": LOCAL_ENDPOINT,
        "node_stable_id": None,
        "tag": None,
    }


def started_entry() -> Payload:
    return {
        "schema_version": "cole-voice-lab.replication-ledger-entry/v1",
        "sequence": 1,
        "previous_entry_sha256": None,
        "kind": "upload-started",
        "library_id": LIBRARY,
        "generation": 1,
        "approval_sha256": "1" * 64,
        "destination": tailnet_destination(),
        "payload_sha256": "4" * 64,
        "manifest_sha256": None,
        "classes": ["prompt-database", "records"],
        "byte_count": 2048,
        "blob_count": 3,
        "head_version": None,
        "recorded_at": TIME,
    }


def completed_entry() -> Payload:
    return started_entry() | {
        "sequence": 2,
        "previous_entry_sha256": "3" * 64,
        "kind": "upload-completed",
        "manifest_sha256": "2" * 64,
        "head_version": 1,
    }


def local_started_entry() -> Payload:
    return started_entry() | {"destination": local_destination(), "approval_sha256": None}


def local_completed_entry() -> Payload:
    return completed_entry() | {"destination": local_destination(), "approval_sha256": None}


# One valid payload per registered schema name, plus the models that are never stored on
# their own: the ledger's embedded destination and a generation's plan.
SYNTHETIC: dict[str, Build] = {
    "storage-blob-ref": blob_ref,
    "storage-ref-value": ref_value,
    "library-manifest-entry": file_entry,
    "library-manifest": manifest,
    "library-manifest-envelope": envelope,
    "storage-binding": tailnet_binding,
    "replication-state": pushed_state,
    "replication-ledger-entry": completed_entry,
}
CONTRACTS: dict[type[ContractModel], Build] = {
    **{STORAGE_SCHEMA_MODELS[name]: build for name, build in SYNTHETIC.items()},
    LedgerDestination: tailnet_destination,
    PlannedManifestEntry: planned_file_entry,
    ManifestPlan: plan,
}
UNREGISTERED = {LedgerDestination, PlannedManifestEntry, ManifestPlan}
TIMESTAMP_FIELDS = [
    (RefValue, "updated_at"),
    (ManifestPlan, "created_at"),
    (LibraryManifest, "created_at"),
    (StorageBinding, "created_at"),
    (ReplicationState, "updated_at"),
    (LedgerEntry, "recorded_at"),
]


def cases(model: type[ContractModel], *invalid: tuple[Build, Payload, str]) -> list[Any]:
    """``pytest.param`` rows of (model, base payload builder, one change, expected message)."""
    return [
        pytest.param(model, build, changes, message, id=f"{model.__name__}-{index}")
        for index, (build, changes, message) in enumerate(invalid)
    ]


# -- registration and generic rules -------------------------------------------------------


def test_the_synthetic_set_covers_every_storage_contract() -> None:
    assert set(SYNTHETIC) == set(STORAGE_SCHEMA_MODELS)
    assert set(CONTRACTS) - set(STORAGE_SCHEMA_MODELS.values()) == UNREGISTERED
    assert not UNREGISTERED & set(SCHEMA_MODELS.values())
    timestamp: object = UtcTimestamp
    stamped = [
        (model, field)
        for model in CONTRACTS
        for field, info in model.model_fields.items()
        if info.annotation is timestamp
    ]
    assert sorted(stamped, key=repr) == sorted(TIMESTAMP_FIELDS, key=repr)


@pytest.mark.parametrize("name", sorted(STORAGE_SCHEMA_MODELS))
def test_every_storage_contract_round_trips_and_is_registered_once(name: str) -> None:
    model = STORAGE_SCHEMA_MODELS[name]
    record = validate(model, SYNTHETIC[name]())

    assert model.model_validate_json(record.model_dump_json()) == record
    canonical = canonical_json_bytes(record)
    assert canonical_json_bytes(model.model_validate_json(canonical)) == canonical
    assert SCHEMA_MODELS[name] is model
    assert list(SCHEMA_MODELS.values()).count(model) == 1
    schema = model.model_json_schema()
    assert isinstance(schema["required"], list)
    assert (REPOSITORY / "schemas" / f"{name}.schema.json").read_bytes() == schema_bytes(model)
    version = model.model_fields.get("schema_version")
    assert version is None or version.default == f"cole-voice-lab.{name}/v1"


def test_storage_contracts_are_neither_swift_wire_nor_corpus_contracts() -> None:
    from cole_voice_lab.recording_contracts import CORPUS_SCHEMA_MODELS, RECORDING_SCHEMA_MODELS

    storage = set(STORAGE_SCHEMA_MODELS.values())
    for registry in (RECORDING_SCHEMA_MODELS, CORPUS_SCHEMA_MODELS):
        assert not set(registry) & set(STORAGE_SCHEMA_MODELS)
        assert not set(registry.values()) & storage


@pytest.mark.parametrize(
    ("model", "field"),
    [
        pytest.param(model, field, id=f"{model.__name__}-{field}")
        for model in CONTRACTS
        for field, info in model.model_fields.items()
        if info.is_required()
    ],
)
def test_every_required_field_must_be_present(model: type[ContractModel], field: str) -> None:
    payload = CONTRACTS[model]()
    del payload[field]
    with pytest.raises(ValidationError, match=rf"\n{field}\n  Field required"):
        validate(model, payload)


@pytest.mark.parametrize("model", CONTRACTS, ids=lambda model: model.__name__)
def test_every_storage_contract_refuses_an_unknown_field(model: type[ContractModel]) -> None:
    with pytest.raises(ValidationError, match=EXTRA):
        validate(model, CONTRACTS[model]() | {"token": "not-a-field"})


@pytest.mark.parametrize(
    "name",
    [
        name
        for name, model in STORAGE_SCHEMA_MODELS.items()
        if "schema_version" in model.model_fields
    ],
)
def test_a_record_of_another_schema_version_is_refused(name: str) -> None:
    model = STORAGE_SCHEMA_MODELS[name]
    record = validate(
        model, {key: value for key, value in SYNTHETIC[name]().items() if key != "schema_version"}
    )
    assert canonical_json_bytes(record) == canonical_json_bytes(validate(model, SYNTHETIC[name]()))
    with pytest.raises(ValidationError, match=f"Input should be 'cole-voice-lab.{name}/v1'"):
        validate(model, SYNTHETIC[name]() | {"schema_version": f"cole-voice-lab.{name}/v2"})


@pytest.mark.parametrize(
    ("model", "field"),
    [
        pytest.param(model, field, id=f"{model.__name__}-{field}")
        for model, field in TIMESTAMP_FIELDS
    ],
)
def test_every_storage_timestamp_is_an_explicit_instant_kept_in_utc(
    model: type[ContractModel], field: str
) -> None:
    payload = CONTRACTS[model]()
    shifted = validate(model, payload | {field: "2026-09-17T14:00:00+02:00"})
    assert getattr(shifted, field) == FIXED
    assert getattr(shifted, field).tzinfo is UTC
    assert canonical_json_bytes(shifted) == canonical_json_bytes(validate(model, payload))
    with pytest.raises(ValidationError, match="storage timestamps require an explicit timezone"):
        validate(model, payload | {field: "2026-09-17T12:00:00"})
    with pytest.raises(ValidationError, match="Input should be a valid datetime"):
        validate(model, payload | {field: "yesterday"})


# -- BlobRef and RefValue -----------------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [{}, {"size": 0, "sha256": EMPTY_SHA256}, {"size": MAX_BLOB_BYTES}, {"size": 1}],
)
def test_a_blob_ref_names_a_stored_size_up_to_the_blob_limit(changes: Payload) -> None:
    assert validate(BlobRef, blob_ref(**changes)).size == (blob_ref() | changes)["size"]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"sha256": "A" * 64}, PATTERN),
        ({"sha256": "a" * 63}, PATTERN),
        ({"size": -1}, "greater than or equal to 0"),
        ({"size": MAX_BLOB_BYTES + 1}, "less than or equal to 16777216"),
        ({"size": 44.0}, "Input should be a valid integer"),
        ({"size": True}, "Input should be a valid integer"),
        ({"size": "44"}, "Input should be a valid integer"),
        ({"size": 0}, "the empty SHA-256 belongs to size 0"),
        ({"sha256": EMPTY_SHA256}, "the empty SHA-256 belongs to size 0"),
    ],
)
def test_an_invalid_blob_ref_is_refused(changes: Payload, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        validate(BlobRef, blob_ref(**changes))


def test_a_ref_value_counts_versions_up_to_the_largest_signed_64_bit_integer() -> None:
    record = validate(RefValue, ref_value() | {"version": MAX_STORAGE_COUNTER})
    assert record.version == 2**63 - 1
    assert record.target == BlobRef(sha256="a" * 64, size=44)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"name": "Libraries/cole-self/head"}, PATTERN),
        ({"name": "libraries/../head"}, PATTERN),
        ({"name": TOO_LONG_REF}, "at most 256 characters"),
        ({"version": 0}, "greater than or equal to 1"),
        ({"version": MAX_STORAGE_COUNTER + 1}, "less than or equal to 9223372036854775807"),
        ({"version": 1.0}, "Input should be a valid integer"),
        ({"target": blob_ref(size=-1)}, "greater than or equal to 0"),
        ({"target": "a" * 64}, "Input should be an object"),
    ],
)
def test_an_invalid_ref_value_is_refused(changes: Payload, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        validate(RefValue, ref_value() | changes)


# -- ManifestEntry ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "build",
    [
        file_entry,
        empty_entry,
        pin_entry,
        database_entry,
        lambda: file_entry() | {"size": 1},
        lambda: file_entry() | {"mode": 0o400},
        lambda: pin_entry() | {"mode": 0o600, "size": 0, "sha256": EMPTY_SHA256},
        lambda: entry("provenance/vaults/binding.json"),
        lambda: entry("provenance/vault-old/state.json"),
        lambda: entry("vault/provenance/state.json"),
        lambda: entry("provenance/" + "x" * 1013),
        lambda: entry("raw/caf\u00e9 take.wav"),
    ],
)
def test_a_valid_manifest_entry_is_accepted(build: Build) -> None:
    payload = build()
    record = validate(ManifestEntry, payload)
    assert (record.path, record.kind, len(record.chunks)) == (
        payload["path"],
        payload["kind"],
        len(payload["chunks"]),
    )
    planned_record = validate(PlannedManifestEntry, unchunked(payload))
    assert planned_record.model_dump() == record.model_dump(exclude={"chunks"})


def test_a_planned_file_is_valid_before_any_of_its_chunks_is_stored() -> None:
    take = {
        "path": "raw/take-1.wav",
        "file_class": "recordings",
        "kind": "file",
        "sha256": hashlib.sha256(b"0123456789").hexdigest(),
        "size": 10,
        "mode": 0o400,
        "pin_source": None,
    }

    assert validate(PlannedManifestEntry, take).size == 10
    with pytest.raises(ValidationError, match=EXTRA):
        validate(PlannedManifestEntry, take | {"chunks": []})
    with pytest.raises(ValidationError, match=r"\nchunks\n  Field required"):
        validate(ManifestEntry, take)
    with pytest.raises(ValidationError, match="a file entry has chunks exactly when its size"):
        validate(ManifestEntry, take | {"chunks": []})


ENTRY_CASES: list[tuple[Build, Payload, str]] = [
    (file_entry, {"path": ""}, "at least 1 character"),
    (file_entry, {"path": "/provenance/x.json"}, "absolute, empty, dot, and parent"),
    (file_entry, {"path": "provenance/../x.json"}, "absolute, empty, dot, and parent"),
    (file_entry, {"path": "provenance/./x.json"}, "canonical POSIX form"),
    (file_entry, {"path": "provenance//x.json"}, "canonical POSIX form"),
    (file_entry, {"path": "provenance/x.json/"}, "canonical POSIX form"),
    (file_entry, {"path": "provenance\\x.json"}, "non-empty POSIX relative paths"),
    (file_entry, {"path": "provenance/x\tjson"}, "cannot contain control characters"),
    (file_entry, {"path": "provenance/x\x7fjson"}, "hidden, control or separator"),
    (file_entry, {"path": "provenance/x\x85json"}, "hidden, control or separator"),
    (file_entry, {"path": "provenance/x\u200bjson"}, "hidden, control or separator"),
    (file_entry, {"path": "provenance/x\u2028json"}, "hidden, control or separator"),
    (file_entry, {"path": "provenance/" + "x" * 1014}, "at most 1024 characters"),
    (file_entry, {"path": "provenance/vault"}, "cannot name provenance/vault"),
    (file_entry, {"path": "provenance/vault/binding.json"}, "cannot name provenance/vault"),
    (file_entry, {"path": "Provenance/VAULT/state.json"}, "cannot name provenance/vault"),
    (file_entry, {"file_class": "speech"}, "Input should be 'records', 'prompt-database'"),
    (file_entry, {"kind": "directory"}, "Input should be 'file' or 'pin'"),
    (file_entry, {"mode": 0o644}, "Input should be 256 or 384"),
    (file_entry, {"mode": 0o700}, "Input should be 256 or 384"),
    (file_entry, {"mode": "0600"}, "Input should be 256 or 384"),
    (file_entry, {"sha256": "g" * 64}, PATTERN),
    (file_entry, {"size": -1}, "greater than or equal to 0"),
    (
        file_entry,
        {"size": MAX_STORAGE_COUNTER + 1},
        "less than or equal to 9223372036854775807",
    ),
    (file_entry, {"sha256": EMPTY_SHA256}, "the empty SHA-256 belongs to size 0"),
    (empty_entry, {"sha256": HELLO_SHA256}, "the empty SHA-256 belongs to size 0"),
    (file_entry, {"chunks": []}, "a file entry has chunks exactly when its size is above 0"),
    (
        file_entry,
        {"size": 1, "chunks": []},
        "a file entry has chunks exactly when its size is above 0",
    ),
    (empty_entry, {"chunks": [blob_ref()]}, "a file entry has chunks exactly when"),
    (file_entry, {"chunks": blob_ref()}, "Input should be a valid array"),
    (file_entry, {"chunks": [blob_ref(sha256="x" * 64)]}, PATTERN),
    (
        file_entry,
        {"pin_source": "finetune-run.runtime.pretrained_sha256"},
        "pin_source is required for a pin entry and forbidden for a file",
    ),
    (pin_entry, {"pin_source": None}, "pin_source is required for a pin entry"),
    (pin_entry, {"chunks": [blob_ref()]}, "a pin entry carries no chunks"),
    (pin_entry, {"pin_source": "Finetune-Run.runtime"}, PATTERN),
    (pin_entry, {"pin_source": "finetune-run..runtime"}, PATTERN),
    (pin_entry, {"pin_source": ".runtime"}, PATTERN),
    (pin_entry, {"pin_source": "run.json#runtime"}, PATTERN),
]


@pytest.mark.parametrize(
    ("model", "build", "changes", "message"),
    [
        *cases(ManifestEntry, *ENTRY_CASES),
        # Every rule but the chunk rules is a planned entry's rule too.
        *cases(
            PlannedManifestEntry,
            *(
                (planned(build), changes, message)
                for build, changes, message in ENTRY_CASES
                if "chunks" not in changes
            ),
        ),
    ],
)
def test_an_invalid_manifest_entry_is_refused(
    model: type[ContractModel], build: Build, changes: Payload, message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        validate(model, build() | changes)


def test_an_undecodable_file_name_is_refused_in_python_mode_too() -> None:
    """``os.listdir`` spells an undecodable name with a lone surrogate; neither mode takes one."""
    undecodable = os.fsdecode(b"raw/\xff.wav")
    assert unicodedata.category(undecodable[4]) == "Cs"

    with pytest.raises(ValidationError, match="unable to parse raw data as a unicode string"):
        ManifestEntry.model_validate(empty_entry() | {"path": undecodable, "chunks": ()})
    with pytest.raises(ValidationError, match="Invalid JSON"):
        validate(ManifestEntry, empty_entry() | {"path": undecodable})
    assert ManifestEntry.model_validate(empty_entry() | {"chunks": ()}).size == 0


def test_paths_that_differ_only_by_case_or_normalization_share_a_key() -> None:
    assert manifest_path_key("RAW/Caf\u00e9.WAV") == manifest_path_key("raw/cafe\u0301.wav")
    assert manifest_path_key("raw/Stra\u00dfe.wav") == manifest_path_key("raw/strasse.wav")
    assert manifest_path_key("raw/a.wav") != manifest_path_key("raw/b.wav")
    assert unicodedata.is_normalized("NFC", manifest_path_key("raw/cafe\u0301.wav"))


# -- LibraryManifest ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "build",
    [
        manifest,
        lambda: manifest() | {"generation": 1, "previous_manifest_sha256": None, "entries": []},
        lambda: (
            manifest()
            | {"classes": ["derived-edits", "prompt-database", "recordings", "records", "training"]}
        ),
        lambda: manifest() | {"generation": MAX_STORAGE_COUNTER},
        lambda: manifest() | {"library_id": "a" * 64},
        lambda: manifest() | {"entries": [entry("raw/Take.wav"), entry("raw/take-2.wav")]},
        lambda: (
            manifest()
            | {"entries": [entry("raw/take"), entry("raw/take.wav"), entry("raw/take2/a.wav")]}
        ),
    ],
)
def test_a_valid_manifest_is_accepted(build: Build) -> None:
    payload = build()
    record = validate(LibraryManifest, payload)
    assert [item.path for item in record.entries] == [item["path"] for item in payload["entries"]]
    assert manifest_plan(record) == validate(ManifestPlan, unchunked(payload))


MANIFEST_CASES: list[tuple[Build, Payload, str]] = [
    (manifest, {"library_id": "Cole-Self"}, PATTERN),
    (manifest, {"library_id": "cole-self-"}, PATTERN),
    (manifest, {"library_id": "a" * 65}, "at most 64 characters"),
    (manifest, {"generation": 0}, "greater than or equal to 1"),
    (
        manifest,
        {"generation": 1},
        "previous_manifest_sha256 is null exactly for generation 1",
    ),
    (
        manifest,
        {"previous_manifest_sha256": None},
        "previous_manifest_sha256 is null exactly for generation 1",
    ),
    (manifest, {"previous_manifest_sha256": "E" * 64}, PATTERN),
    (manifest, {"classes": []}, "at least 1 item"),
    (manifest, {"classes": ["records", "prompt-database", "training"]}, "must be sorted"),
    (
        manifest,
        {"classes": ["prompt-database", "records", "records", "training"]},
        "classes must not repeat",
    ),
    (
        manifest,
        {"classes": ["prompt-database", "records", "speech", "training"]},
        "Input should be 'records'",
    ),
    (
        manifest,
        {"classes": ["prompt-database", "records"]},
        "every manifest entry's class must be one of the manifest's classes",
    ),
    (manifest, {"entries": [file_entry(), pin_entry()]}, "sorted by path"),
    (manifest, {"entries": [file_entry(), file_entry()]}, "must be unique ignoring case"),
    (
        manifest,
        {"entries": [entry("raw/A.wav"), entry("raw/a.wav")]},
        "must be unique ignoring case",
    ),
    (
        manifest,
        {"entries": [entry("raw/cafe\u0301.wav"), entry("raw/caf\u00e9.wav")]},
        "must be unique ignoring case and Unicode normalization",
    ),
    (
        manifest,
        {"entries": [entry("raw/take"), entry("raw/take/a.wav")]},
        "cannot be both a file and a directory",
    ),
    (
        manifest,
        {"entries": [entry("raw/Take"), entry("raw/take/sub/a.wav")]},
        "cannot be both a file and a directory",
    ),
    (
        manifest,
        {"entries": [entry("provenance/vault/state.json")]},
        "cannot name provenance/vault",
    ),
    (manifest, {"entries": [file_entry() | {"size": -1}]}, "greater than or equal to 0"),
    (manifest, {"entries": [file_entry() | {"chunks": []}]}, "a file entry has chunks"),
]


@pytest.mark.parametrize(
    ("model", "build", "changes", "message"),
    [
        *cases(LibraryManifest, *MANIFEST_CASES),
        # A plan keeps every manifest rule; its entries are planned entries.
        *cases(
            ManifestPlan,
            *(
                (planned(build), unchunked(changes), message)
                for build, changes, message in MANIFEST_CASES
                if message != "a file entry has chunks"
            ),
            (plan, {"schema_version": "cole-voice-lab.library-manifest/v1"}, EXTRA),
            (plan, {"entries": [planned_file_entry() | {"chunks": [blob_ref()]}]}, EXTRA),
        ),
    ],
)
def test_an_invalid_manifest_is_refused(
    model: type[ContractModel], build: Build, changes: Payload, message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        validate(model, build() | changes)


def test_a_manifest_is_named_by_the_digest_of_its_canonical_json() -> None:
    record = validate(LibraryManifest, manifest())
    digest = manifest_digest(record)

    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert digest == hashlib.sha256(canonical_json_bytes(record)).hexdigest()
    assert b"\n" not in canonical_json_bytes(record)
    reordered = dict(reversed(list(manifest().items())))
    assert manifest_digest(validate(LibraryManifest, reordered)) == digest
    shifted = manifest() | {"created_at": "2026-09-17T14:00:00+02:00"}
    assert manifest_digest(validate(LibraryManifest, shifted)) == digest
    assert manifest_digest(LibraryManifest.model_validate_json(record.model_dump_json())) == digest
    changed = manifest()
    changed["entries"][2]["mode"] = 0o400
    assert manifest_digest(validate(LibraryManifest, changed)) != digest


def resealed(payload: Payload) -> Payload:
    """The same files with fresh chunk refs, as sealing the same bytes again gives."""
    for item in payload["entries"]:
        item["chunks"] = [chunk | {"sha256": "9" * 64} for chunk in item["chunks"]]
    return payload


def test_a_payload_digest_names_the_files_but_not_their_sealed_chunks() -> None:
    record = validate(LibraryManifest, manifest())
    digest = manifest_payload_digest(record)
    planned_json = unchunked(json.loads(canonical_json_bytes(record)))

    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert (
        digest
        == hashlib.sha256(
            canonical_json_bytes({"domain": MANIFEST_PAYLOAD_DOMAIN, "plan": planned_json})
        ).hexdigest()
    )
    assert manifest_payload_digest(validate(ManifestPlan, plan())) == digest
    assert digest != manifest_digest(record)
    again = validate(LibraryManifest, resealed(manifest()))
    assert manifest_digest(again) != manifest_digest(record)
    assert manifest_payload_digest(again) == digest
    shifted = manifest() | {"created_at": "2026-09-17T14:00:00+02:00"}
    assert manifest_payload_digest(validate(LibraryManifest, shifted)) == digest
    empty = validate(LibraryManifest, manifest() | {"entries": []})
    assert manifest_payload_digest(empty) != manifest_digest(empty)


def test_the_plan_of_a_manifest_is_exactly_the_plan_it_was_built_from() -> None:
    record = validate(LibraryManifest, manifest())
    expected = validate(ManifestPlan, plan())
    recovered = manifest_plan(record)

    assert recovered == expected
    assert type(recovered) is ManifestPlan
    assert {type(item) for item in recovered.entries} == {PlannedManifestEntry}
    assert manifest_plan(expected) == expected
    assert manifest_plan(recovered) is not recovered
    # A plan holding full entries (a Python-mode shortcut) still hashes as its plan.
    shortcut = ManifestPlan(
        library_id=record.library_id,
        generation=record.generation,
        previous_manifest_sha256=record.previous_manifest_sha256,
        created_at=record.created_at,
        classes=record.classes,
        entries=record.entries,
    )
    assert {type(item) for item in shortcut.entries} == {ManifestEntry}
    assert manifest_plan(shortcut) == expected
    assert manifest_payload_digest(shortcut) == manifest_payload_digest(expected)


def test_the_plan_of_an_unvalidated_manifest_is_checked() -> None:
    record = validate(LibraryManifest, manifest())
    unsorted = LibraryManifest.model_construct(
        **dict(record) | {"entries": tuple(reversed(record.entries))}
    )

    with pytest.raises(ValidationError, match="manifest entries must be sorted by path"):
        manifest_plan(unsorted)
    with pytest.raises(ValidationError, match="manifest entries must be sorted by path"):
        manifest_payload_digest(unsorted)


class ResealingBlobStore(InMemoryBlobStore):
    """Stores each put under a fresh prefix, as the sealed store's fresh nonce does."""

    def __init__(self, seed: bytes) -> None:
        super().__init__()
        self.seed = seed

    def put(self, data: bytes) -> BlobRef:
        return super().put(self.seed + len(self.blobs).to_bytes(8, "big") + data)


def upload(plan_record: ManifestPlan, contents: dict[str, bytes], blobs: BlobStore) -> Payload:
    """Store every planned file in four-byte chunks; return the manifest that names them."""
    entries = []
    for item in plan_record.entries:
        data = contents.get(item.path, b"")
        chunks = (
            [blobs.put(data[start : start + 4]) for start in range(0, len(data), 4)]
            if item.kind == "file"
            else []
        )
        entries.append(
            item.model_dump(mode="json")
            | {"chunks": [chunk.model_dump(mode="json") for chunk in chunks]}
        )
    return plan_record.model_dump(mode="json") | {"entries": entries}


def test_an_upload_names_its_plan_before_any_chunk_and_its_manifest_after() -> None:
    """The ledger's order, with nothing but the plan known when the started entry is written."""
    contents = {
        "provenance/corpus/corpus.sqlite3": b"SQLite format 3\x00",
        "provenance/recording-takes/take-a/reservation.json": b"",
        "raw/take-1.wav": b"0123456789",
    }
    files = [
        {
            "path": path,
            "file_class": "prompt-database" if path.endswith(".sqlite3") else "records",
            "kind": "file",
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "mode": 0o600,
            "pin_source": None,
        }
        for path, data in contents.items()
    ]
    files[2] |= {"file_class": "recordings", "mode": 0o400}
    classes = ["prompt-database", "recordings", "records", "training"]
    plan_record = validate(
        ManifestPlan,
        plan()
        | {
            "generation": 1,
            "previous_manifest_sha256": None,
            "classes": classes,
            "entries": [unchunked(pin_entry()), *files],
        },
    )
    blobs = ResealingBlobStore(b"first")
    refs = InMemoryRefStore(lambda: FIXED)

    started = validate(
        LedgerEntry,
        started_entry()
        | {"payload_sha256": manifest_payload_digest(plan_record), "classes": classes},
    )
    assert not blobs.blobs
    stored = validate(LibraryManifest, upload(plan_record, contents, blobs))
    head = publish_head(blobs, refs, LIBRARY, canonical_json_bytes(stored))
    completed = validate(
        LedgerEntry,
        completed_entry()
        | {
            "payload_sha256": started.payload_sha256,
            "manifest_sha256": manifest_digest(stored),
            "head_version": head.version,
            "classes": classes,
        },
    )

    assert [len(item.chunks) for item in stored.entries] == [0, 4, 0, 3]
    assert len(blobs.blobs) == 8
    assert manifest_plan(stored) == plan_record
    assert manifest_payload_digest(stored) == started.payload_sha256 == completed.payload_sha256
    assert completed.manifest_sha256 != completed.payload_sha256
    assert blobs.get(head.target) == b"first" + (7).to_bytes(8, "big") + canonical_json_bytes(
        stored
    )
    resent = validate(LibraryManifest, upload(plan_record, contents, ResealingBlobStore(b"again")))
    assert manifest_digest(resent) != completed.manifest_sha256
    assert manifest_payload_digest(resent) == started.payload_sha256


def changed_entry(index: int, **changes: object) -> Callable[[Payload], Payload]:
    def change(payload: Payload) -> Payload:
        payload["entries"][index] |= changes
        return payload

    return change


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda m: m | {"library_id": "cole-other"}, id="library_id"),
        pytest.param(lambda m: m | {"generation": 3}, id="generation"),
        pytest.param(lambda m: m | {"previous_manifest_sha256": "f" * 64}, id="previous"),
        pytest.param(lambda m: m | {"created_at": "2026-09-17T12:00:01Z"}, id="created_at"),
        pytest.param(
            lambda m: m | {"classes": ["prompt-database", "recordings", "records", "training"]},
            id="classes",
        ),
        pytest.param(lambda m: m | {"entries": m["entries"][:-1]}, id="entry-dropped"),
        pytest.param(
            changed_entry(2, path="provenance/recording-takes/take-a/head2.json"), id="path"
        ),
        pytest.param(changed_entry(2, file_class="prompt-database"), id="file_class"),
        pytest.param(changed_entry(2, sha256="9" * 64), id="sha256"),
        pytest.param(changed_entry(2, size=6), id="size"),
        pytest.param(changed_entry(2, mode=0o400), id="mode"),
        pytest.param(
            changed_entry(0, pin_source="finetune-run.runtime.other_sha256"), id="pin_source"
        ),
        pytest.param(
            changed_entry(3, kind="pin", pin_source="finetune-run.runtime.empty_sha256"),
            id="kind",
        ),
    ],
)
def test_every_field_but_the_chunks_changes_the_payload_digest(
    change: Callable[[Payload], Payload],
) -> None:
    digest = manifest_payload_digest(validate(LibraryManifest, manifest()))
    changed = validate(LibraryManifest, change(manifest()))

    assert manifest_payload_digest(changed) != digest
    assert manifest_payload_digest(validate(LibraryManifest, resealed(change(manifest())))) == (
        manifest_payload_digest(changed)
    )


# -- StorageBinding -----------------------------------------------------------------------


def test_a_binding_holds_names_but_no_secret() -> None:
    assert list(StorageBinding.model_fields) == [
        "schema_version",
        "backend",
        "endpoint",
        "namespace",
        "library_id",
        "classes",
        "approval_id",
        "key_item",
        "token_item",
        "created_at",
    ]
    tailnet = validate(StorageBinding, tailnet_binding())
    local = validate(StorageBinding, local_binding())
    assert (tailnet.key_item, tailnet.token_item) == NAMES
    assert (local.key_item, local.token_item) == (NAMES.key, None)


@pytest.mark.parametrize(
    "build",
    [
        tailnet_binding,
        local_binding,
        lambda: tailnet_binding() | {"endpoint": "vault.ts.net"},
        lambda: tailnet_binding() | {"endpoint": "a1.b-2.c3d"},
        lambda: tailnet_binding() | {"endpoint": LONGEST_DNS},
        lambda: tailnet_binding() | {"namespace": "a"},
        lambda: tailnet_binding() | {"namespace": "a" + "-" * 61 + "b"},
        lambda: local_binding() | {"endpoint": "/Volumes/caf\u00e9"},
        lambda: local_binding() | {"endpoint": "/" + "x" * 1023},
        lambda: local_binding() | {"classes": ["records"]},
        lambda: (
            local_binding()
            | {"classes": ["derived-edits", "prompt-database", "recordings", "records", "training"]}
        ),
        lambda: tailnet_binding() | {"classes": ["training"]},
        lambda: (
            tailnet_binding()
            | {
                "library_id": "a" * 64,
                "key_item": keychain_item_names("a" * 64).key,
                "token_item": keychain_item_names("a" * 64).token,
            }
        ),
    ],
)
def test_a_valid_binding_is_accepted(build: Build) -> None:
    payload = build()
    assert validate(StorageBinding, payload).endpoint == payload["endpoint"]


@pytest.mark.parametrize(
    ("model", "build", "changes", "message"),
    cases(
        StorageBinding,
        (tailnet_binding, {"backend": "s3"}, "'local-directory' or 'tailnet-vault'"),
        *(
            (tailnet_binding, {"endpoint": endpoint}, "a tailnet-vault endpoint is a lowercase DNS")
            for endpoint in (
                f"https://{TAILNET_ENDPOINT}",
                f"{TAILNET_ENDPOINT}:443",
                TAILNET_ENDPOINT.capitalize(),
                "voice-lab-vault",
                f"{TAILNET_ENDPOINT}.",
                "-vault.ts.net",
                "vault-.ts.net",
                "vault..ts.net",
                "vault_1.ts.net",
                "100.64.0.7",
                "vault.ts.123",
                "a" * 64 + ".ts.net",
                LONGEST_DNS[:-1] + "dd",
                LOCAL_ENDPOINT,
            )
        ),
        *(
            (local_binding, {"endpoint": endpoint}, "an absolute, normalized directory path")
            for endpoint in (
                "Volumes/Library Copy",
                "/Volumes/../private",
                "/Volumes/./Library",
                "/Volumes/Library Copy/",
                "//Volumes/Library",
                "/Volumes/Library\nCopy",
                TAILNET_ENDPOINT,
            )
        ),
        (local_binding, {"endpoint": "/Volumes/Library\u200bCopy"}, "hidden, control or separator"),
        (local_binding, {"endpoint": "/Volumes/Library\x7fCopy"}, "hidden, control or separator"),
        (local_binding, {"endpoint": "/Volumes/Library\u2029Copy"}, "hidden, control or separator"),
        (tailnet_binding, {"endpoint": ""}, "at least 1 character"),
        (local_binding, {"endpoint": "/" + "x" * 1024}, "at most 1024 characters"),
        (tailnet_binding, {"namespace": "Cole"}, PATTERN),
        (tailnet_binding, {"namespace": "1cole"}, PATTERN),
        (tailnet_binding, {"namespace": "cole_self"}, PATTERN),
        (tailnet_binding, {"namespace": "a" * 64}, PATTERN),
        (tailnet_binding, {"namespace": ""}, PATTERN),
        (tailnet_binding, {"library_id": "Cole-Self"}, PATTERN),
        (tailnet_binding, {"classes": []}, "at least 1 item"),
        (local_binding, {"classes": []}, "at least 1 item"),
        (tailnet_binding, {"classes": ["records", "prompt-database"]}, "classes must be sorted"),
        (local_binding, {"classes": ["training", "training"]}, "classes must not repeat"),
        (tailnet_binding, {"classes": ["records", "speech"]}, "Input should be 'records'"),
        (tailnet_binding, {"classes": "records"}, "Input should be a valid array"),
        (tailnet_binding, {"approval_id": None}, "approval_id is required for tailnet-vault"),
        (
            local_binding,
            {"approval_id": "vault-2026-09-17"},
            "approval_id is required for tailnet-vault and forbidden for local-directory",
        ),
        (tailnet_binding, {"approval_id": "Vault"}, PATTERN),
        (
            tailnet_binding,
            {"key_item": keychain_item_names("cole-other").key},
            "key_item must be private.cole.voice-lab.vault.cole-self.key",
        ),
        (tailnet_binding, {"key_item": NAMES.token}, "key_item must be"),
        (tailnet_binding, {"key_item": ""}, "at least 1 character"),
        (tailnet_binding, {"token_item": None}, "token_item must be .*cole-self.token"),
        (local_binding, {"token_item": NAMES.token}, "null for local-directory"),
        (tailnet_binding, {"token_item": NAMES.key}, "token_item must be"),
        (tailnet_binding, {"token_item": ""}, "at least 1 character"),
    ),
)
def test_an_invalid_binding_is_refused(
    model: type[ContractModel], build: Build, changes: Payload, message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        validate(model, build() | changes)


# -- ReplicationState ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "build",
    [
        fresh_state,
        pushed_state,
        lambda: pushed_state() | {"generation": 1},
        lambda: pushed_state() | {"generation": 1, "head": ref_value()},
        lambda: pushed_state() | {"generation": MAX_STORAGE_COUNTER},
    ],
)
def test_a_valid_replication_state_is_accepted(build: Build) -> None:
    record = validate(ReplicationState, build())
    assert (record.head is None) is (record.generation == 0)


@pytest.mark.parametrize(
    ("model", "build", "changes", "message"),
    cases(
        ReplicationState,
        (fresh_state, {"generation": -1}, "greater than or equal to 0"),
        (fresh_state, {"generation": MAX_STORAGE_COUNTER + 1}, "less than or equal to"),
        (fresh_state, {"manifest_sha256": "f" * 64}, "manifest_sha256 is null exactly for gen"),
        (fresh_state, {"head": ref_value()}, "head is null exactly for generation 0"),
        (pushed_state, {"manifest_sha256": None}, "manifest_sha256 is null exactly for gen"),
        (pushed_state, {"head": None}, "head is null exactly for generation 0"),
        (
            pushed_state,
            {"generation": 1, "manifest_sha256": None},
            "manifest_sha256 is null exactly for generation 0",
        ),
        (pushed_state, {"generation": 1, "head": None}, "head is null exactly for generation 0"),
        (
            pushed_state,
            {"head": ref_value() | {"name": library_head_ref("cole-other")}},
            "head must be this library's head ref",
        ),
        (
            pushed_state,
            {"head": ref_value() | {"name": "libraries/cole-self/tail"}},
            "head must be this library's head ref",
        ),
        (pushed_state, {"head": ref_value() | {"version": 0}}, "greater than or equal to 1"),
        (pushed_state, {"manifest_sha256": "F" * 64}, PATTERN),
        (pushed_state, {"library_id": "Cole"}, PATTERN),
    ),
)
def test_an_invalid_replication_state_is_refused(
    model: type[ContractModel], build: Build, changes: Payload, message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        validate(model, build() | changes)


# -- LedgerDestination and LedgerEntry ----------------------------------------------------


@pytest.mark.parametrize(
    "build",
    [
        tailnet_destination,
        local_destination,
        lambda: tailnet_destination() | {"tag": "tag:Voice-Lab-1", "node_stable_id": "n" * 64},
    ],
)
def test_a_valid_ledger_destination_is_accepted(build: Build) -> None:
    record = validate(LedgerDestination, build())
    assert (record.node_stable_id is None) is (record.backend == "local-directory")


@pytest.mark.parametrize(
    ("model", "build", "changes", "message"),
    cases(
        LedgerDestination,
        (tailnet_destination, {"node_stable_id": None}, "node_stable_id is required for tail"),
        (tailnet_destination, {"tag": None}, "tag is required for tailnet-vault"),
        (
            local_destination,
            {"node_stable_id": "nQRJBE7CNTRL"},
            "node_stable_id is required for tailnet-vault and forbidden for local-directory",
        ),
        (
            local_destination,
            {"tag": "tag:voice-lab-vault"},
            "tag is required for tailnet-vault and forbidden for local-directory",
        ),
        (tailnet_destination, {"node_stable_id": "n-QRJBE7"}, PATTERN),
        (tailnet_destination, {"node_stable_id": ""}, PATTERN),
        (tailnet_destination, {"node_stable_id": "n" * 65}, PATTERN),
        (tailnet_destination, {"tag": "voice-lab-vault"}, PATTERN),
        (tailnet_destination, {"tag": "tag:"}, PATTERN),
        (tailnet_destination, {"tag": "tag:1vault"}, PATTERN),
        (tailnet_destination, {"tag": "tag:voice_lab"}, PATTERN),
        (tailnet_destination, {"tag": "tag:" + "v" * 125}, PATTERN),
        (tailnet_destination, {"endpoint": "100.64.0.7"}, "a tailnet-vault endpoint is"),
        (local_destination, {"endpoint": "relative"}, "a local-directory endpoint is"),
        (tailnet_destination, {"namespace": "Cole"}, PATTERN),
        (tailnet_destination, {"backend": "s3"}, "'local-directory' or 'tailnet-vault'"),
    ),
)
def test_an_invalid_ledger_destination_is_refused(
    model: type[ContractModel], build: Build, changes: Payload, message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        validate(model, build() | changes)


@pytest.mark.parametrize(
    "build",
    [
        started_entry,
        completed_entry,
        local_started_entry,
        local_completed_entry,
        lambda: completed_entry() | {"manifest_sha256": "4" * 64},
        lambda: started_entry() | {"classes": ["training"]},
        lambda: started_entry() | {"byte_count": 3 * MAX_BLOB_BYTES},
        lambda: started_entry() | {"byte_count": 0, "blob_count": 0},
        lambda: started_entry() | {"byte_count": 0},
        lambda: completed_entry() | {"sequence": MAX_STORAGE_COUNTER, "head_version": 7},
    ],
)
def test_a_valid_ledger_entry_is_accepted(build: Build) -> None:
    record = validate(LedgerEntry, build())
    started = record.kind == "upload-started"
    assert (record.head_version is None) is started
    assert (record.manifest_sha256 is None) is started
    assert (record.approval_sha256 is None) is (record.destination.backend == "local-directory")


@pytest.mark.parametrize(
    ("model", "build", "changes", "message"),
    cases(
        LedgerEntry,
        (started_entry, {"sequence": 0}, "greater than or equal to 1"),
        (
            started_entry,
            {"previous_entry_sha256": "3" * 64},
            "previous_entry_sha256 is null exactly for sequence 1",
        ),
        (
            completed_entry,
            {"previous_entry_sha256": None},
            "previous_entry_sha256 is null exactly for sequence 1",
        ),
        (
            started_entry,
            {"kind": "upload-failed"},
            "Input should be 'upload-started' or 'upload-completed'",
        ),
        (started_entry, {"generation": 0}, "greater than or equal to 1"),
        (
            started_entry,
            {"head_version": 1},
            "head_version is required for upload-completed and forbidden for upload-started",
        ),
        (completed_entry, {"head_version": None}, "head_version is required for upload-comp"),
        (completed_entry, {"head_version": 0}, "greater than or equal to 1"),
        (started_entry, {"approval_sha256": "1" * 63}, PATTERN),
        (
            started_entry,
            {"approval_sha256": None},
            "approval_sha256 is required for tailnet-vault and forbidden for local-directory",
        ),
        (completed_entry, {"approval_sha256": None}, "approval_sha256 is required for tailnet"),
        (
            local_started_entry,
            {"approval_sha256": "1" * 64},
            "approval_sha256 is required for tailnet-vault and forbidden for local-directory",
        ),
        (local_completed_entry, {"approval_sha256": "1" * 64}, "forbidden for local-directory"),
        (local_started_entry, {"approval_sha256": "1" * 63}, PATTERN),
        (started_entry, {"payload_sha256": None}, "Input should be a valid string"),
        (completed_entry, {"payload_sha256": None}, "Input should be a valid string"),
        (started_entry, {"payload_sha256": "4" * 63}, PATTERN),
        (
            started_entry,
            {"manifest_sha256": "2" * 64},
            "manifest_sha256 is required for upload-completed and forbidden for upload-started",
        ),
        (
            completed_entry,
            {"manifest_sha256": None},
            "manifest_sha256 is required for upload-completed and forbidden for upload-started",
        ),
        (local_completed_entry, {"manifest_sha256": None}, "manifest_sha256 is required for up"),
        (completed_entry, {"manifest_sha256": "2" * 63}, PATTERN),
        (
            completed_entry,
            {"kind": "upload-started", "head_version": None},
            "manifest_sha256 is required for upload-completed and forbidden for upload-started",
        ),
        (started_entry, {"classes": []}, "at least 1 item"),
        (started_entry, {"classes": ["records", "prompt-database"]}, "classes must be sorted"),
        (started_entry, {"classes": ["records", "records"]}, "classes must not repeat"),
        (started_entry, {"byte_count": -1}, "greater than or equal to 0"),
        (started_entry, {"blob_count": -1}, "greater than or equal to 0"),
        (
            started_entry,
            {"byte_count": 1, "blob_count": 0},
            "byte_count cannot exceed blob_count blobs",
        ),
        (
            started_entry,
            {"byte_count": 3 * MAX_BLOB_BYTES + 1},
            "byte_count cannot exceed blob_count blobs",
        ),
        (
            started_entry,
            {"destination": tailnet_destination() | {"tag": None}},
            "tag is required for tailnet-vault",
        ),
        (started_entry, {"destination": TAILNET_ENDPOINT}, "Input should be an object"),
        (started_entry, {"library_id": "a" * 65}, "at most 64 characters"),
    ),
)
def test_an_invalid_ledger_entry_is_refused(
    model: type[ContractModel], build: Build, changes: Payload, message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        validate(model, build() | changes)


# -- ManifestEnvelope and the ledger digest ------------------------------------------------


@pytest.mark.parametrize(
    "build",
    [
        envelope,
        lambda: envelope() | {"size": 2},
        lambda: envelope() | {"size": 2 * MAX_BLOB_BYTES},
        lambda: envelope() | {"chunks": [blob_ref()], "size": 1},
        lambda: (
            envelope() | {"chunks": [blob_ref()] * 17, "size": MAX_MANIFEST_BYTES, "generation": 1}
        ),
    ],
)
def test_a_valid_manifest_envelope_is_accepted(build: Build) -> None:
    record = validate(ManifestEnvelope, build())
    assert len(record.chunks) <= record.size <= len(record.chunks) * MAX_BLOB_BYTES


@pytest.mark.parametrize(
    ("model", "build", "changes", "message"),
    cases(
        ManifestEnvelope,
        (envelope, {"chunks": []}, "at least 1 item"),
        (envelope, {"size": 0}, "greater than or equal to 1"),
        (envelope, {"size": MAX_MANIFEST_BYTES + 1}, "less than or equal to 268435456"),
        (envelope, {"size": 1}, "size is at least the chunk count"),
        (envelope, {"size": 2 * MAX_BLOB_BYTES + 1}, "at most the chunk count times"),
        (envelope, {"generation": 0}, "greater than or equal to 1"),
        (envelope, {"manifest_sha256": "E" * 64}, PATTERN),
        (envelope, {"library_id": "Cole"}, PATTERN),
        (envelope, {"chunks": [blob_ref(size=MAX_BLOB_BYTES + 1)]}, "less than or equal to"),
        (envelope, {"size": 1.0}, "Input should be a valid integer"),
    ),
)
def test_an_invalid_manifest_envelope_is_refused(
    model: type[ContractModel], build: Build, changes: Payload, message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        validate(model, build() | changes)


def test_a_ledger_entry_is_named_by_the_digest_of_its_canonical_json() -> None:
    record = validate(LedgerEntry, started_entry())
    digest = ledger_entry_digest(record)

    assert digest == hashlib.sha256(canonical_json_bytes(record)).hexdigest()
    assert ledger_entry_digest(validate(LedgerEntry, dict(reversed(started_entry().items())))) == (
        digest
    )
    shifted = started_entry() | {"recorded_at": "2026-09-17T14:00:00+02:00"}
    assert ledger_entry_digest(validate(LedgerEntry, shifted)) == digest
    changed = validate(LedgerEntry, started_entry() | {"byte_count": 2047})
    assert ledger_entry_digest(changed) != digest
    assert digest not in {manifest_digest(validate(LibraryManifest, manifest()))}
