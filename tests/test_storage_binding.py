"""The composition root: which vault a library is bound to, and what it refuses (Step 9).

Scenario tests for ``src/cole_voice_lab/storage_binding.py``: the records under
``provenance/vault`` (the library identity, the binding, destination approvals and the request
log), the library key and the vault tokens in a fake Keychain, the local-directory endpoint
rules, and every refusal ``open_library_vault`` makes before a byte can leave the Mac. The
tailnet path runs against a vault server this process starts on the loopback, reached through
the test wrapper in ``tests/vault_binding_fixtures.py``; the identity check itself is the real
client's, driven by a fake resolver. Nothing here touches the login Keychain, a tailnet peer,
a network or the real library.
"""

from __future__ import annotations

import ast
import json
import stat
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from vault_binding_fixtures import (
    ALL_CLASSES,
    APPROVAL_ID,
    COPIED_CLASSES,
    ENDPOINT,
    LIBRARY_ID,
    NAMESPACE,
    NODE_STABLE_ID,
    TAILNET_SUFFIX,
    FakeResolver,
    MemoryKeychain,
    approval_file_digest,
    approval_record,
    approved_peer,
    initialized_library,
    install_vault_seams,
    running_vault,
    store_library_key,
    store_vault_token,
    write_approval,
)

import cole_voice_lab.storage_binding as binding_module
from cole_voice_lab.canonical import canonical_json_bytes, write_canonical_json
from cole_voice_lab.errors import SecurityError
from cole_voice_lab.library_generations import (
    PushRunningError,
    VaultRecords,
    push_generation,
    verify_generation,
)
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.storage_binding import (
    VAULT_TOKEN_ACCOUNTS,
    LibraryVault,
    VaultRequestLog,
    approval_path,
    audit_vault_access,
    bind_library,
    binding_path,
    catalog_store_build_resources,
    catalog_store_location,
    check_recovery_sheet_target,
    create_library_vault_key,
    create_vault_tokens,
    import_library_vault_key,
    import_vault_token,
    initialize_library,
    keychain_client,
    library_identity_path,
    library_key_fingerprint,
    open_library_vault,
    open_vault_for_restore,
    read_access_records,
    read_destination_approval,
    read_library_identity,
    read_request_records,
    read_storage_binding,
    require_library_identity,
    tailnet_resolver,
    validate_local_endpoint,
    vault_connection,
    vault_token_present,
    write_destination_approval,
    write_library_recovery_sheet,
)
from cole_voice_lab.storage_contracts import (
    VAULT_NODE_TAG,
    LibraryIdentity,
    StorageBinding,
    VaultAccessRecord,
    VaultRequestRecord,
    keychain_item_names,
)
from cole_voice_lab.storage_http_client import (
    ExpectedIdentity,
    TailscaleCliResolver,
    VaultConnection,
)
from cole_voice_lab.storage_ports import (
    BlobRef,
    StorageError,
    StorageIntegrityError,
    library_head_ref,
    storage_error,
)

NAMES = keychain_item_names(LIBRARY_ID)
FIXED = datetime(2026, 9, 17, 12, tzinfo=UTC)


# -- fixtures --------------------------------------------------------------------------------


@pytest.fixture
def library(tmp_path: Path) -> AssetLayout:
    return initialized_library(tmp_path.resolve() / "assets")


@pytest.fixture
def elsewhere(tmp_path: Path) -> Path:
    """A private directory outside the library, where a local vault or a sheet may go."""
    directory = tmp_path.resolve() / "outside"
    directory.mkdir(mode=0o700)
    return directory


def bind_local(layout: AssetLayout, endpoint: Path, **changes: Any) -> StorageBinding:
    arguments: dict[str, Any] = {
        "backend": "local-directory",
        "endpoint": str(endpoint),
        "namespace": NAMESPACE,
        "classes": COPIED_CLASSES,
    }
    return bind_library(layout, **(arguments | changes)).binding


def bind_tailnet(layout: AssetLayout, digest: str, **changes: Any) -> StorageBinding:
    arguments: dict[str, Any] = {
        "backend": "tailnet-vault",
        "endpoint": ENDPOINT,
        "namespace": NAMESPACE,
        "classes": COPIED_CLASSES,
        "approval_id": APPROVAL_ID,
        "approval_sha256": digest,
    }
    return bind_library(layout, **(arguments | changes)).binding


# -- the library identity --------------------------------------------------------------------


def test_a_library_is_named_once_and_the_name_is_never_replaced(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    assert read_library_identity(layout) is None
    with pytest.raises(StorageError, match="run `voice-lab vault init` first") as missing:
        require_library_identity(layout)
    assert missing.value.code == "STORAGE.NOT_BOUND"

    path, identity = initialize_library(layout, clock=lambda: FIXED)

    assert path == library_identity_path(layout)
    assert identity.library_id.startswith("lib-20260917-")
    assert identity.created_at == FIXED
    assert path.read_bytes() == canonical_json_bytes(identity) + b"\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert require_library_identity(layout) == identity
    with pytest.raises(StorageError, match="already") as again:
        initialize_library(layout, library_id="lib-20260918-0123456789ab")
    assert again.value.code == "STORAGE.CONFLICT"


def test_another_mac_adopts_the_library_id_it_is_restoring(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")

    _, identity = initialize_library(layout, library_id=LIBRARY_ID)

    assert identity.library_id == LIBRARY_ID
    with pytest.raises(ValueError, match="a library id is a stable id"):
        initialize_library(
            AssetLayout.initialize(tmp_path.resolve() / "other"), library_id="Cole Library"
        )


def test_an_unreadable_library_record_is_a_refusal_not_a_guess(library: AssetLayout) -> None:
    path = library_identity_path(library)
    path.chmod(0o600)
    path.write_bytes(b"{}\n")
    with pytest.raises(StorageError, match="is not a usable library record") as invalid:
        read_library_identity(library)
    assert invalid.value.code == "STORAGE.NOT_BOUND"

    write_canonical_json(path, LibraryIdentity(library_id=LIBRARY_ID, created_at=FIXED))
    path.chmod(0o600)
    reformatted = json.dumps(json.loads(path.read_bytes()), indent=1).encode() + b"\n"
    path.write_bytes(reformatted)
    with pytest.raises(StorageError, match="not canonical"):
        read_library_identity(library)


# -- approvals -------------------------------------------------------------------------------


def test_an_approval_is_written_once_read_only_and_read_back_by_its_digest(
    library: AssetLayout,
) -> None:
    approval = approval_record()

    path, digest = write_destination_approval(library, approval)

    assert path == approval_path(library, APPROVAL_ID)
    assert stat.S_IMODE(path.stat().st_mode) == 0o400
    assert digest == approval_file_digest(approval)
    assert read_destination_approval(library, APPROVAL_ID) == (approval, digest)
    with pytest.raises(StorageError, match="never replaced") as conflict:
        write_destination_approval(library, approval)
    assert conflict.value.code == "STORAGE.CONFLICT"


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        ("missing", "does not exist"),
        ("invalid", "is not a usable destination approval"),
        ("other-id", "records approval vault-2026-09-18, not vault-2026-09-17"),
    ],
)
def test_an_approval_that_cannot_be_trusted_is_not_approved(
    library: AssetLayout, damage: str, message: str
) -> None:
    if damage != "missing":
        approval = approval_record(
            **({"approval_id": "vault-2026-09-18"} if damage == "other-id" else {})
        )
        path, _ = write_destination_approval(library, approval)
        if damage == "other-id":
            path.rename(approval_path(library, APPROVAL_ID))
        else:
            path.chmod(0o600)
            path.write_bytes(b'{"approved": true}\n')

    with pytest.raises(StorageError, match=message) as refused:
        read_destination_approval(library, APPROVAL_ID)
    assert refused.value.code == "STORAGE.NOT_APPROVED"


# -- the local-directory endpoint -------------------------------------------------------------


def test_a_local_endpoint_is_a_private_directory_away_from_the_library(
    library: AssetLayout, elsewhere: Path, tmp_path: Path
) -> None:
    assert validate_local_endpoint(library, str(elsewhere)) == elsewhere

    for endpoint, problem in (
        ("relative/vault", "is not an absolute, normalized path"),
        (f"{elsewhere}/..", "is not an absolute, normalized path"),
        ("/Volumes", "is not specific enough"),
        (str(Path.home()), "is this user's home directory"),
        (str(Path.home().resolve()), "is this user's home directory"),
        (str(library.root), "is the library"),
        (str(library.root / "tmp" / "vault"), "is the library"),
        (str(library.root.parent), "is the library"),
        (str(Path(__file__).resolve().parents[1] / "build" / "vault"), "is the repository"),
    ):
        with pytest.raises(StorageError, match=problem) as refused:
            validate_local_endpoint(library, endpoint)
        assert refused.value.code == "STORAGE.NOT_BOUND"
    assert not (tmp_path / "never-made").exists()


# -- binding -----------------------------------------------------------------------------------


def test_a_local_binding_names_its_directory_and_creates_the_store(
    library: AssetLayout, elsewhere: Path
) -> None:
    result = bind_library(
        library,
        backend="local-directory",
        endpoint=str(elsewhere),
        namespace=NAMESPACE,
        classes=("records", "recordings", "records"),
        clock=lambda: FIXED,
    )

    assert result.path == binding_path(library)
    assert not result.replaced and result.superseded_state is None
    assert result.binding.classes == ("recordings", "records")
    assert result.binding.library_id == LIBRARY_ID
    assert result.binding.key_item == NAMES.key
    assert result.binding.token_item is None and result.binding.approval_id is None
    assert (elsewhere / "blobs").is_dir() and (elsewhere / "refs").is_dir()
    assert stat.S_IMODE(binding_path(library).stat().st_mode) == 0o600
    assert read_storage_binding(library) == result.binding


def test_a_tailnet_binding_needs_the_approval_it_names(
    library: AssetLayout, elsewhere: Path
) -> None:
    approval = approval_record()
    _, digest = write_approval(library, approval)

    binding = bind_tailnet(library, digest)

    assert binding.approval_sha256 == digest and binding.token_item == NAMES.token
    assert read_storage_binding(library) == binding
    # An approval written for another library never binds this one, whatever its digest says.
    foreign = approval_record(approval_id="vault-foreign", library_id="lib-20260918-ffffffffffff")
    _, foreign_digest = write_approval(library, foreign)
    with pytest.raises(StorageError, match="approves library lib-20260918") as other:
        bind_tailnet(library, foreign_digest, approval_id="vault-foreign", replace=True)
    assert other.value.code == "STORAGE.NOT_APPROVED"


@pytest.mark.parametrize(
    ("changes", "code", "message"),
    [
        ({"approval_id": None}, "STORAGE.NOT_APPROVED", "needs --approval-id"),
        ({"approval_sha256": None}, "STORAGE.NOT_APPROVED", "needs --approval-id"),
        ({"approval_sha256": "0" * 64}, "STORAGE.NOT_APPROVED", "the approval changed"),
        ({"approval_id": "vault-other"}, "STORAGE.NOT_APPROVED", "does not exist"),
        ({"namespace": "spare"}, "STORAGE.NOT_APPROVED", "approves namespace cole, not spare"),
        (
            {"endpoint": f"other.{TAILNET_SUFFIX}"},
            "STORAGE.NOT_APPROVED",
            f"approves {ENDPOINT}, not other",
        ),
        (
            {"classes": ALL_CLASSES},
            "STORAGE.NOT_APPROVED",
            "does not cover training",
        ),
    ],
)
def test_a_tailnet_binding_the_approval_does_not_cover_is_refused(
    library: AssetLayout, changes: dict[str, Any], code: str, message: str
) -> None:
    _, digest = write_approval(library, approval_record())

    with pytest.raises(StorageError, match=message) as refused:
        bind_tailnet(library, digest, **changes)
    assert refused.value.code == code
    assert read_storage_binding(library) is None


@pytest.mark.parametrize("when", ["early", "expired"])
def test_a_binding_needs_an_approval_that_stands_right_now(library: AssetLayout, when: str) -> None:
    now = datetime.now(UTC)
    approved_at = now + timedelta(days=1) if when == "early" else now - timedelta(days=200)
    approval = approval_record(
        approved_at=approved_at, expires_at=approved_at + timedelta(days=100)
    )
    _, digest = write_approval(library, approval)

    with pytest.raises(StorageError, match="not yet effective" if when == "early" else "expired"):
        bind_tailnet(library, digest)


def test_an_existing_binding_is_replaced_only_on_purpose(
    library: AssetLayout, elsewhere: Path, tmp_path: Path
) -> None:
    bind_local(library, elsewhere)
    other = tmp_path.resolve() / "second-vault"
    other.mkdir(mode=0o700)

    with pytest.raises(StorageError, match="pass --replace") as conflict:
        bind_local(library, other)
    assert conflict.value.code == "STORAGE.CONFLICT"
    assert read_storage_binding(library) is not None
    assert read_storage_binding(library).endpoint == str(elsewhere)

    result = bind_library(
        library,
        backend="local-directory",
        endpoint=str(other),
        namespace=NAMESPACE,
        classes=COPIED_CLASSES,
        replace=True,
    )
    assert result.replaced and result.binding.endpoint == str(other)


def push_into(layout: AssetLayout, vault: LibraryVault) -> None:
    push_generation(
        layout,
        blob_store=vault.blob_store,
        ref_store=vault.ref_store,
        library_id=vault.library_id,
        destination=vault.destination,
        classes=vault.classes,
        chunk_bytes=vault.chunk_bytes,
    )


def opened(layout: AssetLayout) -> LibraryVault:
    vault = open_library_vault(layout)
    assert vault is not None
    return vault


def test_moving_a_copied_library_sets_its_records_aside_and_never_removes_them(
    library: AssetLayout, elsewhere: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seams = install_vault_seams(monkeypatch)
    store_library_key(seams.keychain)
    bind_local(library, elsewhere)
    push_into(library, opened(library))
    records = VaultRecords(library)
    assert records.read_state(LIBRARY_ID) is not None
    kept = records.manifest_copy_path(1).read_bytes()
    second = tmp_path.resolve() / "second-vault"
    second.mkdir(mode=0o700)

    with pytest.raises(StorageError, match="--reset-state") as conflict:
        bind_local(library, second, replace=True)
    assert conflict.value.code == "STORAGE.CONFLICT"
    assert records.read_state(LIBRARY_ID) is not None

    result = bind_library(
        library,
        backend="local-directory",
        endpoint=str(second),
        namespace=NAMESPACE,
        classes=COPIED_CLASSES,
        replace=True,
        reset_state=True,
    )

    assert result.superseded_state == library.root / "provenance/vault/state-superseded-1.json"
    assert result.superseded_manifests == library.root / "provenance/vault/manifests-superseded-1"
    assert records.read_state(LIBRARY_ID) is None
    assert (result.superseded_manifests / "00000001.json").read_bytes() == kept
    assert not (library.root / "provenance/vault/manifests").exists()

    # The second vault starts this library's chain again, and a third move keeps both records.
    push_into(library, opened(library))
    third = tmp_path.resolve() / "third-vault"
    third.mkdir(mode=0o700)
    moved = bind_library(
        library,
        backend="local-directory",
        endpoint=str(third),
        namespace=NAMESPACE,
        classes=COPIED_CLASSES,
        replace=True,
        reset_state=True,
    )
    assert moved.superseded_state == library.root / "provenance/vault/state-superseded-2.json"
    assert (library.root / "provenance/vault/state-superseded-1.json").exists()
    assert (library.root / "provenance/vault/manifests-superseded-2/00000001.json").exists()


def test_a_reset_moves_only_the_records_that_are_still_there(
    library: AssetLayout, elsewhere: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator who set the kept manifests aside by hand still gets a clean reset."""
    seams = install_vault_seams(monkeypatch)
    store_library_key(seams.keychain)
    bind_local(library, elsewhere)
    push_into(library, opened(library))
    manifests = library.root / "provenance/vault/manifests"
    manifests.rename(library.root / "provenance/vault/manifests-elsewhere")
    second = tmp_path.resolve() / "second-vault"
    second.mkdir(mode=0o700)

    result = bind_library(
        library,
        backend="local-directory",
        endpoint=str(second),
        namespace=NAMESPACE,
        classes=COPIED_CLASSES,
        replace=True,
        reset_state=True,
    )

    assert result.superseded_state == library.root / "provenance/vault/state-superseded-1.json"
    assert result.superseded_manifests is None
    assert VaultRecords(library).read_state(LIBRARY_ID) is None


def test_resetting_a_library_that_has_copied_nothing_moves_nothing(
    library: AssetLayout, elsewhere: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(binding_module, "MAX_SUPERSEDED", 0)
    result = bind_library(
        library,
        backend="local-directory",
        endpoint=str(elsewhere),
        namespace=NAMESPACE,
        classes=COPIED_CLASSES,
        reset_state=True,
    )
    assert (result.superseded_state, result.superseded_manifests) == (None, None)


def test_a_rebind_refuses_while_a_push_is_running_instead_of_undoing_it(
    library: AssetLayout, elsewhere: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A push writes state.json and the kept manifests at its end; a reset must not race it."""
    seams = install_vault_seams(monkeypatch)
    store_library_key(seams.keychain)
    bind_local(library, elsewhere)
    push_into(library, opened(library))
    state = VaultRecords(library).read_state(LIBRARY_ID)
    second = tmp_path.resolve() / "second-vault"
    second.mkdir(mode=0o700)

    with VaultRecords(library).push_lock():
        with pytest.raises(PushRunningError, match="already running") as running:
            bind_library(
                library,
                backend="local-directory",
                endpoint=str(second),
                namespace=NAMESPACE,
                classes=COPIED_CLASSES,
                replace=True,
                reset_state=True,
            )
        assert running.value.code == "REPLICATION.PUSH_RUNNING"

    # Nothing moved and nothing was rebound; the same call works once the push is over.
    assert VaultRecords(library).read_state(LIBRARY_ID) == state
    bound = read_storage_binding(library)
    assert bound is not None and bound.endpoint == str(elsewhere)
    assert not (library.root / "provenance/vault/state-superseded-1.json").exists()
    moved = bind_library(
        library,
        backend="local-directory",
        endpoint=str(second),
        namespace=NAMESPACE,
        classes=COPIED_CLASSES,
        replace=True,
        reset_state=True,
    )
    assert moved.superseded_state is not None


def test_a_library_that_has_moved_a_thousand_times_refuses_to_lose_a_record(
    library: AssetLayout, elsewhere: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seams = install_vault_seams(monkeypatch)
    store_library_key(seams.keychain)
    bind_local(library, elsewhere)
    push_into(library, opened(library))
    other = tmp_path.resolve() / "second-vault"
    other.mkdir(mode=0o700)
    monkeypatch.setattr(binding_module, "MAX_SUPERSEDED", 0)

    with pytest.raises(StorageError, match="superseded 0 times") as full:
        bind_library(
            library,
            backend="local-directory",
            endpoint=str(other),
            namespace=NAMESPACE,
            classes=COPIED_CLASSES,
            replace=True,
            reset_state=True,
        )
    assert full.value.code == "STORAGE.QUOTA"


# -- opening the vault --------------------------------------------------------------------------


def test_a_library_with_no_binding_is_off_not_an_error(
    library: AssetLayout, tmp_path: Path
) -> None:
    assert open_library_vault(library) is None
    assert read_storage_binding(library) is None
    # A library that has never run a vault command has no provenance/vault directory at all.
    bare = AssetLayout.initialize(tmp_path.resolve() / "bare")
    assert not (bare.root / "provenance/vault").exists()
    assert read_storage_binding(bare) is None
    assert read_library_identity(bare) is None
    assert open_library_vault(bare) is None


def test_a_library_that_has_copied_somewhere_refuses_to_run_unbound(
    library: AssetLayout, elsewhere: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seams = install_vault_seams(monkeypatch)
    store_library_key(seams.keychain)
    bind_local(library, elsewhere)
    push_into(library, opened(library))
    binding_path(library).unlink()

    with pytest.raises(StorageError, match="records copies of this library") as refused:
        open_library_vault(library)
    assert refused.value.code == "STORAGE.NOT_BOUND"


def test_a_binding_of_an_unknown_backend_or_another_library_is_refused(
    library: AssetLayout, elsewhere: Path
) -> None:
    bind_local(library, elsewhere)
    path = binding_path(library)
    document = json.loads(path.read_bytes())

    path.write_bytes(json.dumps(document | {"backend": "s3"}).encode() + b"\n")
    with pytest.raises(StorageError, match="is not a usable vault binding") as unknown:
        open_library_vault(library)
    assert unknown.value.code == "STORAGE.NOT_BOUND"

    other = "lib-20260918-ffffffffffff"
    names = keychain_item_names(other)
    foreign_document = document | {"library_id": other, "key_item": names.key}
    write_canonical_json(
        path, StorageBinding.model_validate_json(json.dumps(foreign_document)), mode=0o600
    )
    with pytest.raises(StorageError, match=f"binds library {other}") as foreign:
        open_library_vault(library)
    assert foreign.value.code == "STORAGE.NOT_BOUND"


def test_a_local_vault_is_sealed_with_the_library_key_and_needs_no_token(
    library: AssetLayout, elsewhere: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seams = install_vault_seams(monkeypatch)
    bind_local(library, elsewhere)
    with pytest.raises(StorageError, match="has no library key item") as missing:
        open_library_vault(library)
    assert missing.value.code == "STORAGE.NOT_BOUND"
    store_library_key(seams.keychain)

    vault = open_library_vault(library)

    assert vault is not None
    assert vault.approval is None and vault.request_log is None
    assert vault.library_id == LIBRARY_ID
    assert vault.classes == COPIED_CLASSES
    assert vault.destination.backend == "local-directory"
    assert vault.destination.endpoint == str(elsewhere)
    assert vault.destination.approval_sha256 is None
    assert vault.chunk_bytes == 16 * 1024 * 1024 - 33
    assert vault.key_fingerprint == library_key_fingerprint(library)
    ref = vault.blob_store.put(b"a sealed blob")
    assert vault.blob_store.get(ref) == b"a sealed blob"
    stored = (elsewhere / "blobs" / ref.sha256[:2] / ref.sha256).read_bytes()
    assert stored.startswith(b"CVLS") and b"a sealed blob" not in stored
    assert seams.connections == []


def test_a_tailnet_vault_is_reached_with_the_approved_identity_and_the_write_token(
    library: AssetLayout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running_vault(tmp_path) as server:
        seams = install_vault_seams(monkeypatch, port=server.port)
        store_library_key(seams.keychain)
        approval = approval_record()
        _, digest = write_approval(library, approval)
        bind_tailnet(library, digest)
        with pytest.raises(StorageError, match="has no write vault token item") as missing:
            open_library_vault(library)
        assert missing.value.code == "STORAGE.NOT_BOUND"
        store_vault_token(seams.keychain, "write", token=server.write_token)

        vault = open_library_vault(library)

        assert vault is not None and vault.approval == approval
        assert vault.destination.node_stable_id == NODE_STABLE_ID
        assert vault.destination.tag == VAULT_NODE_TAG
        assert vault.destination.approval_sha256 == digest
        assert seams.suffixes == [TAILNET_SUFFIX]
        (request,) = seams.connections
        assert request.endpoint == ENDPOINT and request.namespace == NAMESPACE
        assert request.identity == ExpectedIdentity(
            node_stable_id=NODE_STABLE_ID, tag=VAULT_NODE_TAG, tailnet_suffix=TAILNET_SUFFIX
        )
        assert request.resolver is seams.resolver
        assert request.token() == server.write_token

        ref = vault.blob_store.put(b"over the tailnet")
        assert vault.blob_store.get(ref) == b"over the tailnet"
        records = read_request_records(library)
        assert {record.operation for record in records} == {"put-blob", "get-blob"}
        assert all(record.namespace == NAMESPACE for record in records)


def test_the_token_is_read_once_per_request_and_never_held(
    library: AssetLayout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The callable reads the Keychain each time, and a whitespace-padded item still works."""
    with running_vault(tmp_path) as server:
        seams = install_vault_seams(monkeypatch, port=server.port)
        store_library_key(seams.keychain)
        _, digest = write_approval(library, approval_record())
        bind_tailnet(library, digest)
        service = (NAMES.token, VAULT_TOKEN_ACCOUNTS["write"])
        seams.keychain.items[service] = f"  {server.write_token}\n"

        vault = open_library_vault(library)
        assert vault is not None
        before = seams.keychain.calls.count(f"find {NAMES.token}")
        vault.blob_store.put(b"one")
        vault.blob_store.put(b"two")
        assert seams.keychain.calls.count(f"find {NAMES.token}") == before + 2

        seams.keychain.items[service] = "not-a-vault-token"
        with pytest.raises(StorageIntegrityError, match="does not hold a vault token") as bad:
            vault.blob_store.put(b"three")
        assert bad.value.code == "STORAGE.INTEGRITY"
        assert "not-a-vault-token" not in str(bad.value)


def test_the_real_client_refuses_a_node_that_is_not_the_approved_one(
    library: AssetLayout, tmp_path: Path
) -> None:
    """The identity the binding hands the client is the one that refuses a substitute node."""
    approval = approval_record()
    identity = ExpectedIdentity(
        node_stable_id=approval.node_stable_id,
        tag=approval.tag,
        tailnet_suffix=approval.tailnet_suffix,
    )
    ref = BlobRef(sha256="a" * 64, size=1)
    for peer, problem in (
        (approved_peer(node_stable_id="nOTHER"), "the node answering is"),
        (approved_peer(tags=("tag:other",)), "does not carry tag:voice-lab-vault"),
        (approved_peer(dns_name="other.colobus-stargazer.ts.net."), "calls itself"),
    ):
        connection = VaultConnection(
            approval.endpoint, NAMESPACE, lambda: "cvlv_" + "a" * 43, FakeResolver(peer), identity
        )
        with pytest.raises(StorageIntegrityError) as mismatch:
            connection.blob_store().has(ref)
        assert mismatch.value.code == "STORAGE.IDENTITY_MISMATCH"
        assert problem in str(mismatch.value)

    other_tailnet = ExpectedIdentity(
        node_stable_id=approval.node_stable_id, tag=approval.tag, tailnet_suffix="other.ts.net"
    )
    with pytest.raises(StorageIntegrityError, match="is not a name in the tailnet"):
        VaultConnection(
            approval.endpoint,
            NAMESPACE,
            lambda: "cvlv_" + "a" * 43,
            FakeResolver(approved_peer()),
            other_tailnet,
        ).blob_store().has(ref)


def test_an_approval_that_lapses_after_the_binding_closes_the_vault(
    library: AssetLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    seams = install_vault_seams(monkeypatch)
    store_library_key(seams.keychain)
    store_vault_token(seams.keychain, "write")
    path, digest = write_approval(library, approval_record())
    bind_tailnet(library, digest)

    # The approval file is edited (an operator would have to chmod it first).
    path.chmod(0o600)
    path.write_bytes(path.read_bytes().replace(b'"Cole"', b'"Someone else"'))
    with pytest.raises(StorageError, match="the approval changed") as changed:
        open_library_vault(library)
    assert changed.value.code == "STORAGE.NOT_APPROVED"

    path.unlink()
    with pytest.raises(StorageError, match="does not exist") as gone:
        open_library_vault(library)
    assert gone.value.code == "STORAGE.NOT_APPROVED"

    # The same approval, but time has passed beyond its expiry.
    expired_at = datetime.now(UTC) - timedelta(days=400)
    expired = approval_record(approved_at=expired_at, expires_at=expired_at + timedelta(days=366))
    _, expired_digest = write_approval(library, expired)
    binding_path(library).chmod(0o600)
    write_canonical_json(
        binding_path(library),
        StorageBinding.model_validate_json(
            json.dumps(
                json.loads(binding_path(library).read_bytes()) | {"approval_sha256": expired_digest}
            )
        ),
        mode=0o600,
    )
    with pytest.raises(StorageError, match="expired") as lapsed:
        open_library_vault(library)
    assert lapsed.value.code == "STORAGE.NOT_APPROVED"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"classes": list(ALL_CLASSES)}, "does not cover training"),
        ({"namespace": "spare"}, "approves namespace cole, not spare"),
        ({"endpoint": f"other.{TAILNET_SUFFIX}"}, f"approves {ENDPOINT}, not other"),
    ],
)
def test_a_binding_edited_past_its_approval_is_refused_when_the_vault_opens(
    library: AssetLayout,
    changes: dict[str, Any],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """binding.json carries no digest of itself, so the approval is checked again at open."""
    seams = install_vault_seams(monkeypatch)
    store_library_key(seams.keychain)
    store_vault_token(seams.keychain, "write")
    _, digest = write_approval(library, approval_record())
    bind_tailnet(library, digest)
    # As written, the approval covers the binding: opening gets as far as the connection.
    with pytest.raises(StorageError, match="no loopback vault"):
        open_library_vault(library)
    assert len(seams.connections) == 1

    # The file is edited in place; its approval digest still matches, so only this check stands
    # between the edit and copying a class (or a destination) Cole never approved.
    path = binding_path(library)
    path.chmod(0o600)
    edited = StorageBinding.model_validate_json(json.dumps(json.loads(path.read_bytes()) | changes))
    write_canonical_json(path, edited, mode=0o600)

    with pytest.raises(StorageError, match=message) as refused:
        open_library_vault(library)
    assert refused.value.code == "STORAGE.NOT_APPROVED"
    # Nothing was opened this time: the refusal comes before any adapter is built.
    assert len(seams.connections) == 1


def test_a_vault_that_is_open_exposes_its_stores_through_one_object(
    library: AssetLayout, elsewhere: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seams = install_vault_seams(monkeypatch)
    store_library_key(seams.keychain)
    bind_local(library, elsewhere)

    vault = open_library_vault(library)

    assert isinstance(vault, LibraryVault)
    assert vault.blob_store is vault.stores.blob_store
    assert vault.ref_store is vault.stores.ref_store
    assert vault.chunk_bytes == vault.stores.chunk_bytes
    assert vault.key_fingerprint == vault.stores.key_fingerprint
    assert vault.request_log is vault.stores.request_log
    assert vault.library_id == vault.stores.library_id
    assert "sheet" not in repr(vault)


# -- restoring onto another Mac -----------------------------------------------------------------


def test_another_mac_restores_with_the_library_id_the_key_and_the_read_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = initialized_library(tmp_path.resolve() / "source")
    seams = install_vault_seams(monkeypatch)
    store_library_key(seams.keychain)
    vault_root = tmp_path.resolve() / "vault"
    vault_root.mkdir(mode=0o700)
    bind_local(source, vault_root)
    push_into(source, opened(source))

    fresh = initialized_library(tmp_path.resolve() / "fresh")
    stores = open_vault_for_restore(
        fresh, backend="local-directory", endpoint=str(vault_root), namespace=NAMESPACE
    )

    assert stores.library_id == LIBRARY_ID
    assert stores.request_log is None
    head = stores.ref_store.read(library_head_ref(LIBRARY_ID))
    assert head is not None and head.version == 1
    with pytest.raises(StorageError, match="takes neither") as wrong:
        open_vault_for_restore(
            fresh,
            backend="local-directory",
            endpoint=str(vault_root),
            namespace=NAMESPACE,
            node_stable_id=NODE_STABLE_ID,
            tailnet_suffix=TAILNET_SUFFIX,
        )
    assert wrong.value.code == "STORAGE.PROTOCOL"
    with pytest.raises(StorageError, match="needs --node-stable-id"):
        open_vault_for_restore(
            fresh, backend="tailnet-vault", endpoint=ENDPOINT, namespace=NAMESPACE
        )
    # A tailnet restore with no namespace is a refusal here, never a fault inside the client.
    with pytest.raises(StorageError, match="needs --namespace") as nameless:
        open_vault_for_restore(
            fresh,
            backend="tailnet-vault",
            endpoint=ENDPOINT,
            node_stable_id=NODE_STABLE_ID,
            tailnet_suffix=TAILNET_SUFFIX,
        )
    assert nameless.value.code == "STORAGE.PROTOCOL"
    # A local directory is its own namespace, so it needs none.
    assert (
        open_vault_for_restore(
            fresh, backend="local-directory", endpoint=str(vault_root)
        ).library_id
        == LIBRARY_ID
    )


def test_another_mac_reaches_a_tailnet_vault_with_its_read_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fresh = initialized_library(tmp_path.resolve() / "fresh")
    with running_vault(tmp_path) as server:
        seams = install_vault_seams(monkeypatch, port=server.port)
        store_library_key(seams.keychain)
        with pytest.raises(StorageError, match="has no read vault token item"):
            open_vault_for_restore(
                fresh,
                backend="tailnet-vault",
                endpoint=ENDPOINT,
                namespace=NAMESPACE,
                node_stable_id=NODE_STABLE_ID,
                tailnet_suffix=TAILNET_SUFFIX,
            )
        store_vault_token(seams.keychain, "read", token=server.read_token)

        stores = open_vault_for_restore(
            fresh,
            backend="tailnet-vault",
            endpoint=ENDPOINT,
            namespace=NAMESPACE,
            node_stable_id=NODE_STABLE_ID,
            tailnet_suffix=TAILNET_SUFFIX,
        )

        (request,) = seams.connections
        assert request.identity.tag == VAULT_NODE_TAG
        assert request.token() == server.read_token
        assert stores.ref_store.read(library_head_ref(LIBRARY_ID)) is None


# -- the key and the tokens ----------------------------------------------------------------------


def test_a_library_key_is_created_once_and_its_sheet_is_the_only_copy(
    library: AssetLayout, elsewhere: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seams = install_vault_seams(monkeypatch)

    created = create_library_vault_key(library, clock=lambda: FIXED)

    assert created.library_id == LIBRARY_ID
    assert created.fingerprint == library_key_fingerprint(library)
    assert LIBRARY_ID in created.sheet and created.fingerprint in created.sheet
    assert created.sheet not in repr(created)
    secret = seams.keychain.items[(NAMES.key, "library-key")]
    assert secret not in repr(created)
    with pytest.raises(StorageError, match="never replaced") as again:
        create_library_vault_key(library)
    assert again.value.code == "STORAGE.CONFLICT"

    sheet = elsewhere / "recovery-sheet.txt"
    assert write_library_recovery_sheet(library, sheet, created.sheet) == sheet
    assert stat.S_IMODE(sheet.stat().st_mode) == 0o600
    restored = initialized_library(library.root.parent / "restored")
    install_vault_seams(monkeypatch)
    assert import_library_vault_key(restored, sheet.read_text()) == created.fingerprint


def test_a_recovery_sheet_never_lands_in_the_library_or_the_repository(
    library: AssetLayout, elsewhere: Path
) -> None:
    assert check_recovery_sheet_target(library, elsewhere / "sheet.txt") == elsewhere / "sheet.txt"
    for target in (
        library.root / "sheet.txt",
        library.root,
        Path(__file__).resolve().parents[1] / "sheet.txt",
    ):
        with pytest.raises(StorageError, match="never written inside the") as refused:
            check_recovery_sheet_target(library, target)
        assert refused.value.code == "STORAGE.NOT_APPROVED"


def test_a_missing_key_reports_nothing_and_a_broken_one_reports_the_problem(
    library: AssetLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    seams = install_vault_seams(monkeypatch)
    assert library_key_fingerprint(library) is None

    seams.keychain.items[(NAMES.key, "library-key")] = "not hex"
    with pytest.raises(StorageIntegrityError, match="does not hold a library key") as broken:
        library_key_fingerprint(library)
    assert broken.value.code == "STORAGE.INTEGRITY"

    install_vault_seams(
        monkeypatch,
        keychain=MemoryKeychain(failure=storage_error("STORAGE.PROTOCOL", "the tool failed")),
    )
    with pytest.raises(StorageError, match="the tool failed") as failed:
        library_key_fingerprint(library)
    assert failed.value.code == "STORAGE.PROTOCOL"


def test_creating_tokens_keeps_them_in_the_keychain_and_returns_only_hashes(
    library: AssetLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib

    seams = install_vault_seams(monkeypatch)

    tokens = create_vault_tokens(library, NAMESPACE)

    assert (tokens.library_id, tokens.namespace) == (LIBRARY_ID, NAMESPACE)
    assert tokens.write_token_sha256 != tokens.read_token_sha256
    write = seams.keychain.items[(NAMES.token, VAULT_TOKEN_ACCOUNTS["write"])]
    read = seams.keychain.items[(NAMES.read_token, VAULT_TOKEN_ACCOUNTS["read"])]
    assert write.startswith("cvlv_") and read.startswith("cvlv_") and write != read
    assert hashlib.sha256(write.encode("ascii")).hexdigest() == tokens.write_token_sha256
    assert hashlib.sha256(read.encode("ascii")).hexdigest() == tokens.read_token_sha256
    assert vault_token_present(library, "write") and vault_token_present(library, "read")
    import dataclasses

    for secret in (write, read):
        assert secret not in json.dumps(dataclasses.asdict(tokens))


@pytest.mark.parametrize("held", ["write", "read"])
def test_a_token_is_never_replaced_and_neither_is_created_when_one_exists(
    library: AssetLayout, monkeypatch: pytest.MonkeyPatch, held: str
) -> None:
    seams = install_vault_seams(monkeypatch)
    existing = store_vault_token(seams.keychain, held)

    with pytest.raises(StorageError, match=f"already holds the {held} token item") as conflict:
        create_vault_tokens(library, NAMESPACE)
    assert conflict.value.code == "STORAGE.CONFLICT"
    assert len(seams.keychain.items) == 1
    assert seams.keychain.secrets() == (existing,)


def test_a_token_that_could_not_be_stored_names_the_one_that_already_is(
    library: AssetLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Keychain that fails between the two adds must not strand a token with no hash."""
    import hashlib

    class HalfKeychain(MemoryKeychain):
        """Stores the write token, then refuses the read one."""

        def add_generic_password(
            self, *, service: str, account: str, label: str, secret: str
        ) -> None:
            if service == NAMES.read_token:
                raise storage_error("STORAGE.PROTOCOL", "the Keychain could not be reached")
            super().add_generic_password(
                service=service, account=account, label=label, secret=secret
            )

    seams = install_vault_seams(monkeypatch, keychain=HalfKeychain())

    with pytest.raises(StorageError, match="could not be reached") as failed:
        create_vault_tokens(library, NAMESPACE)

    assert failed.value.code == "STORAGE.PROTOCOL"
    stored = seams.keychain.items[(NAMES.token, VAULT_TOKEN_ACCOUNTS["write"])]
    message = str(failed.value)
    assert f"the write token item {NAMES.token} was stored first" in message
    assert hashlib.sha256(stored.encode("ascii")).hexdigest() in message
    assert f"security delete-generic-password -s {NAMES.token}" in message
    assert stored not in message


def test_a_token_this_mac_was_given_is_kept_and_anything_else_refused(
    library: AssetLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib

    from cole_voice_lab.storage_server import new_token

    seams = install_vault_seams(monkeypatch)
    token = new_token()

    digest = import_vault_token(library, "read", f"{token}\n")

    assert digest == hashlib.sha256(token.encode("ascii")).hexdigest()
    assert seams.keychain.items[(NAMES.read_token, VAULT_TOKEN_ACCOUNTS["read"])] == token
    assert not vault_token_present(library, "write")
    with pytest.raises(StorageError, match="43 base64url characters") as refused:
        import_vault_token(library, "write", "hunter2\n")
    assert refused.value.code == "STORAGE.PROTOCOL"
    assert "hunter2" not in str(refused.value)


# -- the request log --------------------------------------------------------------------------


def request_record(**changes: Any) -> VaultRequestRecord:
    fields: dict[str, Any] = {
        "time": FIXED,
        "endpoint": ENDPOINT,
        "namespace": NAMESPACE,
        "operation": "put-blob",
        "ref": "a" * 64,
        "bytes": 17,
        "status": 201,
        "code": None,
        "attempt": 1,
        "node_stable_id": NODE_STABLE_ID,
    }
    return VaultRequestRecord(**(fields | changes))


def test_requests_are_appended_one_json_line_per_utc_day(library: AssetLayout) -> None:
    log = VaultRequestLog(library)
    first = request_record()
    later = request_record(time=FIXED + timedelta(days=1), operation="get-blob")

    log(first)
    log(request_record(attempt=2))
    log(later)

    path = log.path(FIXED)
    assert path.name == "2026-09-17.jsonl"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert log.path(later.time).name == "2026-09-18.jsonl"
    assert read_request_records(library) == (first, request_record(attempt=2), later)
    assert path.read_bytes() == b"".join(
        canonical_json_bytes(record) + b"\n" for record in (first, request_record(attempt=2))
    )


def test_a_request_log_that_is_not_this_user_s_private_file_is_refused(
    library: AssetLayout,
) -> None:
    log = VaultRequestLog(library)
    log(request_record())
    path = log.path(FIXED)
    path.chmod(0o644)

    with pytest.raises(StorageIntegrityError, match="private regular file") as unsafe:
        log(request_record())
    assert unsafe.value.code == "STORAGE.INTEGRITY"
    path.chmod(0o600)

    path.write_bytes(b"{}\n")
    with pytest.raises(StorageError, match="line 1 is not a vault request record") as invalid:
        read_request_records(library)
    assert invalid.value.code == "STORAGE.INTEGRITY"


def test_a_library_that_has_sent_nothing_has_no_request_records(library: AssetLayout) -> None:
    assert read_request_records(library) == ()


# -- auditing ----------------------------------------------------------------------------------


def access_record(**changes: Any) -> VaultAccessRecord:
    fields: dict[str, Any] = {
        "time": FIXED,
        "namespace": NAMESPACE,
        "scope": "write",
        "operation": "put-blob",
        "ref": "a" * 64,
        "bytes": 17,
        "status": 201,
        "code": None,
        "forwarded_for": None,
        "user_login": None,
    }
    return VaultAccessRecord(**(fields | changes))


def test_an_audit_matches_every_access_the_mac_sent_and_names_the_rest() -> None:
    sent = [
        request_record(),
        request_record(operation="get-blob", status=200, bytes=17),
        # A refused upload the vault turned away before reading: it counts none of the bytes.
        request_record(status=401, code="STORAGE.UNAUTHORIZED", bytes=17),
        # An attempt with no answer: the vault may or may not have seen it.
        request_record(
            operation="read-ref",
            ref="libraries/x/head",
            status=None,
            bytes=0,
            code="STORAGE.UNAVAILABLE",
        ),
        request_record(namespace="spare", ref="b" * 64),
    ]
    served = [
        access_record(),
        access_record(operation="get-blob", status=200, scope="read"),
        access_record(status=401, bytes=0, code="STORAGE.UNAUTHORIZED", scope="none"),
        access_record(operation="read-ref", ref="libraries/x/head", bytes=0, status=200),
        access_record(operation="health", namespace=None, ref=None, bytes=0, scope="none"),
        access_record(operation="refused", status=400, bytes=0, code="STORAGE.PROTOCOL"),
        access_record(namespace="spare", ref="b" * 64),
    ]

    report = audit_vault_access(served, sent, NAMESPACE)

    assert (report.namespace, report.server_accesses, report.mac_requests) == (NAMESPACE, 4, 4)
    assert (report.matched, report.unanswered, report.refused_at_server) == (4, 0, 1)
    assert report.unexplained == () and report.exit_status == 0


def test_an_upload_the_vault_refuses_after_reading_it_is_still_the_mac_s_own(
    library: AssetLayout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full namespace refuses a put whose body it has already read, and counts those bytes."""
    with running_vault(tmp_path, quota=1024) as server:
        seams = install_vault_seams(monkeypatch, port=server.port)
        store_library_key(seams.keychain)
        store_vault_token(seams.keychain, "write", token=server.write_token)
        _, digest = write_approval(library, approval_record())
        bind_tailnet(library, digest)
        vault = opened(library)

        with pytest.raises(StorageError, match="STORAGE.QUOTA|quota|full") as refused:
            vault.blob_store.put(b"x" * 4096)
        assert refused.value.code == "STORAGE.QUOTA"
        served = server.access_records()

    # The vault counted the whole body of the upload it read before refusing it.
    (drained,) = [record for record in served if record.status >= 400]
    assert (drained.operation, drained.bytes) == ("put-blob", 4096 + 33)
    report = audit_vault_access(served, read_request_records(library), NAMESPACE)
    assert report.unexplained == () and report.exit_status == 0
    assert report.matched == report.server_accesses


def test_an_audit_fails_on_a_read_or_write_the_mac_never_sent() -> None:
    sent = [request_record()]
    served = [
        access_record(),
        access_record(operation="get-blob", status=200, scope="read", user_login="someone@ts"),
    ]

    report = audit_vault_access(served, sent, NAMESPACE)

    assert report.matched == 1 and report.exit_status == 1
    (unexplained,) = report.unexplained
    assert (unexplained.operation, unexplained.status) == ("get-blob", 200)
    assert unexplained.user_login == "someone@ts"
    # An attempt the vault never answered is counted, not blamed.
    quiet = audit_vault_access(
        [], [request_record(status=None, bytes=0, code="STORAGE.TIMEOUT")], NAMESPACE
    )
    assert (quiet.unanswered, quiet.matched, quiet.exit_status) == (1, 0, 0)


def test_an_access_log_is_read_line_by_line_and_refused_when_it_is_not_one(
    tmp_path: Path,
) -> None:
    path = tmp_path / "access.jsonl"
    records = (access_record(), access_record(operation="get-blob", status=200, scope="read"))
    path.write_bytes(b"".join(canonical_json_bytes(record) + b"\n" for record in records))

    assert read_access_records(path) == records

    path.write_bytes(canonical_json_bytes(records[0]) + b"\n{}\n")
    with pytest.raises(StorageError, match="line 2 is not a vault access record") as invalid:
        read_access_records(path)
    assert invalid.value.code == "STORAGE.INTEGRITY"
    path.write_bytes(b"")
    assert read_access_records(path) == ()
    with pytest.raises(SecurityError, match="regular non-link file"):
        read_access_records(tmp_path)


def test_a_push_and_a_verify_through_a_local_vault_leave_an_auditable_trail(
    library: AssetLayout, elsewhere: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local directory sends no request, so its audit has nothing on either side."""
    seams = install_vault_seams(monkeypatch)
    store_library_key(seams.keychain)
    bind_local(library, elsewhere)
    vault = open_library_vault(library)
    assert vault is not None

    push_generation(
        library,
        blob_store=vault.blob_store,
        ref_store=vault.ref_store,
        library_id=vault.library_id,
        destination=vault.destination,
        classes=vault.classes,
        chunk_bytes=vault.chunk_bytes,
    )
    report = verify_generation(
        library,
        blob_store=vault.blob_store,
        ref_store=vault.ref_store,
        library_id=vault.library_id,
    )

    assert report.ok and report.generation == 1
    assert read_request_records(library) == ()
    assert audit_vault_access((), read_request_records(library), NAMESPACE).exit_status == 0


# -- the catalog openers and the production seams ------------------------------------------------


def test_the_catalog_openers_still_name_the_local_store(library: AssetLayout) -> None:
    assert catalog_store_location(library) == "provenance/corpus/corpus.sqlite3"
    assert catalog_store_build_resources()
    from cole_voice_lab.catalog_ports import CorpusStoreError

    with pytest.raises(CorpusStoreError):
        binding_module.open_catalog_reader(library)
    with binding_module.open_catalog_editor(library, create=True) as catalog:
        assert catalog.list_catalogs() == ()


def test_the_production_seams_build_the_checked_client_and_the_real_keychain() -> None:
    """Nothing is run: a resolver and a connection are built, not used."""
    keychain = keychain_client()
    assert keychain.security == Path("/usr/bin/security")
    resolver = tailnet_resolver(TAILNET_SUFFIX)
    assert isinstance(resolver, TailscaleCliResolver)
    assert resolver.tailnet_suffix == TAILNET_SUFFIX
    assert resolver.binary == Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")

    connection = vault_connection(
        endpoint=ENDPOINT,
        namespace=NAMESPACE,
        token=lambda: "cvlv_" + "a" * 43,
        resolver=resolver,
        identity=ExpectedIdentity(
            node_stable_id=NODE_STABLE_ID, tag=VAULT_NODE_TAG, tailnet_suffix=TAILNET_SUFFIX
        ),
        request_log=lambda record: None,
    )
    assert isinstance(connection, VaultConnection)
    assert ENDPOINT in repr(connection) and "cvlv_" not in repr(connection)


def test_open_library_vault_takes_a_layout_and_nothing_else() -> None:
    """No flag, argument or environment variable can choose a backend or a way to connect."""
    import inspect

    parameters = inspect.signature(open_library_vault).parameters
    assert list(parameters) == ["layout"]
    assert parameters["layout"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD


def test_importing_the_binding_loads_neither_the_cipher_nor_the_vault_client() -> None:
    """The service and the fine-tune worker import this module only to open a catalog."""
    probe = (
        "import sys\n"
        "import cole_voice_lab.storage_binding as binding\n"
        "print('\\n'.join(sorted(sys.modules)))\n"
    )
    loaded = set(
        subprocess.run(
            [sys.executable, "-I", "-B", "-c", probe],
            check=True,
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
            timeout=60,
        ).stdout.split()
    )

    assert "cole_voice_lab.storage_binding" in loaded
    forbidden = {
        "cryptography",
        "cole_voice_lab.storage_sealing",
        "cole_voice_lab.storage_http_client",
        "cole_voice_lab.storage_local",
        "cole_voice_lab.storage_server",
        "cole_voice_lab.library_generations",
        "cole_voice_lab.library_replication",
        "http.client",
        "ssl",
    }
    assert forbidden & loaded == set()


def python_sources() -> Iterator[tuple[str, ast.AST]]:
    """Every module and script of the product, parsed (tests are not product code)."""
    repository = Path(__file__).resolve().parents[1]
    candidates = sorted((repository / "src").rglob("*.py"))
    candidates += sorted(path for path in (repository / "scripts").rglob("*") if path.is_file())
    for path in candidates:
        if "__pycache__" in path.parts or path.suffix not in ("", ".py"):
            continue
        try:
            tree = ast.parse(path.read_bytes(), filename=str(path))
        except (SyntaxError, ValueError):
            continue
        yield path.relative_to(repository).as_posix(), tree


def test_no_production_code_can_reach_the_loopback_client_or_replace_a_seam() -> None:
    """The three seams are read by the binding and written only by a test."""
    seams = {"keychain_client", "tailnet_resolver", "vault_connection"}
    # The client itself declares the loopback constructor and the trust seam it defends.
    allowed = {"src/cole_voice_lab/storage_http_client.py"}
    found: list[tuple[str, int, str]] = []
    for path, tree in python_sources():
        if path in allowed:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "loopback_for_tests":
                found.append((path, node.lineno, node.attr))
            if isinstance(node, ast.keyword) and node.arg == "tls_context":
                found.append((path, node.lineno, "tls_context"))
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Attribute) and target.attr in seams:
                        found.append((path, node.lineno, target.attr))

    assert found == []
    # The scan sees what it looks for, so an empty list means something.
    own = ast.parse(Path(__file__).read_bytes())
    assert any(
        isinstance(node, ast.Attribute) and node.attr == "loopback_for_tests"
        for node in ast.walk(ast.parse("VaultConnection.loopback_for_tests(1, 'n', t)"))
    )
    assert any(isinstance(node, ast.Assign) for node in ast.walk(own))
    assert {"src/cole_voice_lab/storage_binding.py", "src/cole_voice_lab/cli.py"} <= {
        path for path, _ in python_sources()
    }
