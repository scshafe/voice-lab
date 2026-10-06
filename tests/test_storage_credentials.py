"""The Linux keychain: the library key and vault tokens as systemd credentials (D-034).

Scenario tests for ``storage_sealing.SystemdCredentialKeychain``. Every directory is under
``tmp_path`` and every secret is synthetic: no test reads a real credential, runs
``systemd-creds`` or starts a unit. A staging directory (0700, writable) plays the operator's
one-time creation; the same directory made read-only (0500, items 0400) plays what systemd
gives the vault unit as ``$CREDENTIALS_DIRECTORY``.
"""

from __future__ import annotations

import errno
import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cole_voice_lab import storage_sealing
from cole_voice_lab.storage_contracts import keychain_item_names, library_key_item
from cole_voice_lab.storage_ports import StorageError
from cole_voice_lab.storage_sealing import (
    CREDENTIALS_DIRECTORY_VARIABLE,
    LIBRARY_KEY_ACCOUNT,
    SystemdCredentialKeychain,
    create_library_key,
    import_library_key,
    load_library_key,
    render_recovery_sheet,
)

LIBRARY = "lib-20261006-0123456789ab"
KEY_ITEM = library_key_item(LIBRARY)
TOKENS = keychain_item_names(LIBRARY, "laptop")
ACCOUNT = "vault-write-token"
# Synthetic secrets only; never a real one.
SECRET = "cvlv_" + "s" * 43
CREATED = datetime(2026, 10, 6, 12, tzinfo=UTC)


@pytest.fixture
def staging(tmp_path: Path) -> Iterator[Path]:
    """A private staging directory, as the operator makes one (best on a tmpfs)."""
    directory = tmp_path.resolve() / "credentials"
    directory.mkdir(mode=0o700)
    yield directory
    directory.chmod(0o700)
    for item in directory.iterdir():
        if not item.is_symlink():
            item.chmod(0o600)


def keychain_for(directory: Path) -> SystemdCredentialKeychain:
    return SystemdCredentialKeychain(directory)


def as_systemd_gives_it(directory: Path) -> None:
    """Make the staging directory look like a unit's credential directory: read-only."""
    for item in directory.iterdir():
        item.chmod(0o400)
    directory.chmod(0o500)


def refused(code: str, call: Callable[[], object], *, match: str) -> StorageError:
    with pytest.raises(StorageError, match=match) as caught:
        call()
    assert caught.value.code == code
    assert SECRET not in str(caught.value)
    return caught.value


# -- creating the items once, in a staging directory ---------------------------------------


def test_an_item_is_a_new_private_file_named_by_its_service(staging: Path) -> None:
    keychain = keychain_for(staging)
    assert not keychain.has_generic_password(service=TOKENS.token, account=ACCOUNT)

    keychain.add_generic_password(
        service=TOKENS.token, account=ACCOUNT, label=TOKENS.token, secret=SECRET
    )

    item = staging / TOKENS.token
    assert item.read_bytes() == f"{SECRET}\n".encode("ascii")
    assert item.stat().st_mode & 0o777 == 0o600
    assert keychain.has_generic_password(service=TOKENS.token, account=ACCOUNT)
    assert keychain.find_generic_password(service=TOKENS.token, account=ACCOUNT) == SECRET
    assert repr(keychain) == f"SystemdCredentialKeychain({str(staging)!r})"


def test_an_existing_item_is_never_replaced(staging: Path) -> None:
    keychain = keychain_for(staging)
    keychain.add_generic_password(
        service=TOKENS.token, account=ACCOUNT, label=TOKENS.token, secret=SECRET
    )

    refused(
        "STORAGE.CONFLICT",
        lambda: keychain.add_generic_password(
            service=TOKENS.token, account=ACCOUNT, label=TOKENS.token, secret="cvlv_other"
        ),
        match="already holds item .* never replaced",
    )
    assert keychain.find_generic_password(service=TOKENS.token, account=ACCOUNT) == SECRET


def test_the_library_key_and_its_recovery_sheet_work_over_credentials(staging: Path) -> None:
    keychain = keychain_for(staging)
    key = create_library_key(keychain, LIBRARY)

    assert (staging / KEY_ITEM).stat().st_mode & 0o777 == 0o600
    as_systemd_gives_it(staging)
    assert load_library_key(keychain, LIBRARY) == key
    refused(
        "STORAGE.CONFLICT",
        lambda: create_library_key(keychain, LIBRARY),
        match="already exists; a library key is never replaced",
    )

    # Restoring onto a new host: the sheet becomes the item again, in a fresh staging dir.
    fresh = staging.parent / "fresh"
    fresh.mkdir(mode=0o700)
    sheet = render_recovery_sheet(LIBRARY, key, CREATED)
    assert import_library_key(keychain_for(fresh), LIBRARY, sheet) == key
    assert load_library_key(keychain_for(fresh), LIBRARY) == key


@pytest.mark.parametrize("mode", [0o750, 0o705, 0o770])
def test_no_secret_is_written_into_a_directory_others_can_reach(staging: Path, mode: int) -> None:
    staging.chmod(mode)
    refused(
        "STORAGE.PROTOCOL",
        lambda: keychain_for(staging).add_generic_password(
            service=KEY_ITEM, account=LIBRARY_KEY_ACCOUNT, label=KEY_ITEM, secret=SECRET
        ),
        match="is not a private directory of this user",
    )
    assert list(staging.iterdir()) == []


def test_nothing_can_add_an_item_to_a_units_read_only_credentials(staging: Path) -> None:
    keychain = keychain_for(staging)
    keychain.add_generic_password(
        service=TOKENS.token, account=ACCOUNT, label=TOKENS.token, secret=SECRET
    )
    as_systemd_gives_it(staging)

    refused(
        "STORAGE.PROTOCOL",
        lambda: keychain.add_generic_password(
            service=TOKENS.read_token, account="vault-read-token", label="x", secret=SECRET
        ),
        match=f"refused item {TOKENS.read_token}",
    )
    # Reading is what a unit does, and it still works.
    assert keychain.find_generic_password(service=TOKENS.token, account=ACCOUNT) == SECRET
    assert not keychain.has_generic_password(service=TOKENS.read_token, account=ACCOUNT)


@pytest.mark.parametrize(
    ("raised", "left", "message"),
    [
        (OSError(errno.ENOSPC, "No space left on device"), False, r"\(No space left on device\)$"),
        (OSError(errno.EIO, "Input/output error"), True, "could not be removed, delete it by hand"),
    ],
)
def test_a_failed_write_is_named_and_never_shows_the_secret(
    staging: Path, monkeypatch: pytest.MonkeyPatch, raised: OSError, left: bool, message: str
) -> None:
    def fail(directory: int, name: str, data: bytes, *, label: str) -> None:
        assert SECRET.encode("ascii") in data
        raise storage_sealing._LeftoverFileError(raised) if left else raised

    monkeypatch.setattr(storage_sealing, "_write_new_file", fail)
    refused(
        "STORAGE.PROTOCOL",
        lambda: keychain_for(staging).add_generic_password(
            service=KEY_ITEM, account=LIBRARY_KEY_ACCOUNT, label=KEY_ITEM, secret=SECRET
        ),
        match=message,
    )


def test_an_item_that_does_not_read_back_as_written_is_refused(
    staging: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keychain = keychain_for(staging)
    monkeypatch.setattr(keychain, "find_generic_password", lambda **_: "cvlv_different")
    refused(
        "STORAGE.PROTOCOL",
        lambda: keychain.add_generic_password(
            service=KEY_ITEM, account=LIBRARY_KEY_ACCOUNT, label=KEY_ITEM, secret=SECRET
        ),
        match="kept an item .* that holds a different secret",
    )


@pytest.mark.parametrize(
    ("field", "values"),
    [
        ("service", {"service": "a b"}),
        ("account", {"account": "a/b"}),
        ("label", {"label": ""}),
    ],
)
def test_names_are_plain_words(staging: Path, field: str, values: dict[str, str]) -> None:
    arguments = {"service": KEY_ITEM, "account": LIBRARY_KEY_ACCOUNT, "label": KEY_ITEM}
    with pytest.raises(ValueError, match=f"a Keychain {field} is 1-255 characters"):
        keychain_for(staging).add_generic_password(**(arguments | values), secret=SECRET)
    if field != "label":
        lookup = {key: (arguments | values)[key] for key in ("service", "account")}
        with pytest.raises(ValueError, match=f"a Keychain {field}"):
            keychain_for(staging).find_generic_password(**lookup)
        with pytest.raises(ValueError, match=f"a Keychain {field}"):
            keychain_for(staging).has_generic_password(**lookup)


@pytest.mark.parametrize("secret", ["", "two words", "x" * 1025, "caf\u00e9"])
def test_a_secret_is_one_plain_word_and_is_never_shown(staging: Path, secret: str) -> None:
    with pytest.raises(ValueError, match="a credential secret is 1-1024 characters") as caught:
        keychain_for(staging).add_generic_password(
            service=KEY_ITEM, account=LIBRARY_KEY_ACCOUNT, label=KEY_ITEM, secret=secret
        )
    assert not secret or secret not in str(caught.value)
    assert list(staging.iterdir()) == []


# -- reading what systemd gave the unit -----------------------------------------------------


def test_a_missing_item_names_how_it_comes_back(staging: Path) -> None:
    refused(
        "STORAGE.NOT_BOUND",
        lambda: keychain_for(staging).find_generic_password(service=KEY_ITEM, account="x"),
        match=f"has no item {KEY_ITEM}; a library key comes back only from its recovery sheet",
    )


@pytest.mark.parametrize(
    "content",
    [b"", b"\n", b"zq words\n", b"zqzq\n\n", b"\xff\xfe", b"zq\tinside", b"x" * 1025],
)
def test_an_item_that_is_not_one_plain_secret_is_refused(staging: Path, content: bytes) -> None:
    (staging / KEY_ITEM).write_bytes(content)
    (staging / KEY_ITEM).chmod(0o400)
    error = refused(
        "STORAGE.PROTOCOL",
        lambda: keychain_for(staging).find_generic_password(service=KEY_ITEM, account="x"),
        match=f"holds item {KEY_ITEM}, but not as one plain secret",
    )
    assert "zq" not in str(error) and "inside" not in str(error)


def test_a_secret_with_or_without_a_trailing_newline_reads_the_same(staging: Path) -> None:
    (staging / KEY_ITEM).write_bytes(SECRET.encode("ascii"))
    (staging / KEY_ITEM).chmod(0o400)
    assert keychain_for(staging).find_generic_password(service=KEY_ITEM, account="x") == SECRET


def test_an_oversized_item_is_refused_before_it_is_read(staging: Path) -> None:
    (staging / KEY_ITEM).write_bytes(b"x" * 4096)
    (staging / KEY_ITEM).chmod(0o400)
    refused(
        "STORAGE.PROTOCOL",
        lambda: keychain_for(staging).find_generic_password(service=KEY_ITEM, account="x"),
        match="too large to be a secret",
    )


def unsafe_item(staging: Path, kind: str) -> None:
    item = staging / KEY_ITEM
    if kind == "symlink":
        (staging / "elsewhere").write_bytes(f"{SECRET}\n".encode("ascii"))
        (staging / "elsewhere").chmod(0o400)
        item.symlink_to(staging / "elsewhere")
    elif kind == "directory":
        item.mkdir(mode=0o700)
    elif kind == "linked":
        item.write_bytes(f"{SECRET}\n".encode("ascii"))
        item.chmod(0o400)
        os.link(item, staging / "second-link")
    else:
        item.write_bytes(f"{SECRET}\n".encode("ascii"))
        item.chmod({"group-writable": 0o620, "world-readable": 0o604}[kind])


@pytest.mark.parametrize("kind", ["directory", "linked", "group-writable", "world-readable"])
def test_an_item_that_is_not_a_private_regular_file_is_refused(staging: Path, kind: str) -> None:
    unsafe_item(staging, kind)
    keychain = keychain_for(staging)
    match = "not as a private regular file with one link"
    refused(
        "STORAGE.PROTOCOL",
        lambda: keychain.has_generic_password(service=KEY_ITEM, account="x"),
        match=match,
    )
    refused(
        "STORAGE.PROTOCOL",
        lambda: keychain.find_generic_password(service=KEY_ITEM, account="x"),
        match=match,
    )


def test_an_item_reached_through_a_link_is_never_followed(staging: Path) -> None:
    unsafe_item(staging, "symlink")
    keychain = keychain_for(staging)
    refused(
        "STORAGE.PROTOCOL",
        lambda: keychain.has_generic_password(service=KEY_ITEM, account="x"),
        match="not as a private regular file",
    )
    refused(
        "STORAGE.PROTOCOL",
        lambda: keychain.find_generic_password(service=KEY_ITEM, account="x"),
        match=f"could not open item {KEY_ITEM}",
    )


def test_an_unreadable_item_or_directory_is_a_protocol_failure(staging: Path) -> None:
    keychain = keychain_for(staging)
    (staging / KEY_ITEM).write_bytes(f"{SECRET}\n".encode("ascii"))
    (staging / KEY_ITEM).chmod(0o000)
    refused(
        "STORAGE.PROTOCOL",
        lambda: keychain.find_generic_password(service=KEY_ITEM, account="x"),
        match=f"could not open item {KEY_ITEM} \\(Permission denied\\)",
    )
    # A directory this user may list but not search: nothing in it can be asked about.
    staging.chmod(0o600)
    refused(
        "STORAGE.PROTOCOL",
        lambda: keychain.has_generic_password(service=KEY_ITEM, account="x"),
        match=f"could not be asked for item {KEY_ITEM}",
    )
    staging.chmod(0o700)


def test_a_read_that_fails_midway_is_named_without_the_secret(
    staging: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (staging / KEY_ITEM).write_bytes(f"{SECRET}\n".encode("ascii"))
    (staging / KEY_ITEM).chmod(0o400)

    def fail(descriptor: int, size: int) -> bytes:
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(storage_sealing.os, "read", fail)
    refused(
        "STORAGE.PROTOCOL",
        lambda: keychain_for(staging).find_generic_password(service=KEY_ITEM, account="x"),
        match=f"could not read item {KEY_ITEM} \\(Input/output error\\)",
    )


def test_a_missing_credential_directory_is_a_protocol_failure(tmp_path: Path) -> None:
    keychain = keychain_for(tmp_path.resolve() / "absent")
    refused(
        "STORAGE.PROTOCOL",
        lambda: keychain.has_generic_password(service=KEY_ITEM, account="x"),
        match="cannot be opened as a directory",
    )


# -- where the directory comes from ---------------------------------------------------------


def test_the_directory_is_the_one_systemd_names(tmp_path: Path) -> None:
    unit = tmp_path.resolve() / "voice-lab-vault@laptop.service"
    keychain = SystemdCredentialKeychain.from_environment(
        {CREDENTIALS_DIRECTORY_VARIABLE: str(unit)}
    )
    assert keychain.directory == unit


def test_the_process_environment_is_read_when_none_is_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CREDENTIALS_DIRECTORY_VARIABLE, str(tmp_path))
    assert SystemdCredentialKeychain.from_environment().directory == tmp_path


@pytest.mark.parametrize(
    "environment",
    [{}, {CREDENTIALS_DIRECTORY_VARIABLE: ""}, {CREDENTIALS_DIRECTORY_VARIABLE: "relative/dir"}],
)
def test_outside_a_unit_with_credentials_nothing_can_be_read(environment: dict[str, str]) -> None:
    refused(
        "STORAGE.PROTOCOL",
        lambda: SystemdCredentialKeychain.from_environment(environment),
        match=r"\$CREDENTIALS_DIRECTORY does not name a credential directory.*ImportCredential",
    )


@pytest.mark.parametrize("directory", ["relative", "/run/../etc", "/a\nb", "/a\x7fb"])
def test_a_credential_directory_is_an_absolute_normalized_path(directory: str) -> None:
    with pytest.raises(ValueError, match="a credential directory is an absolute, normalized"):
        SystemdCredentialKeychain(Path(directory))
