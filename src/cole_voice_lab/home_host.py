"""The standing home-host approval (D-021) and its upload ledger, on the capturing host.

@canonical home_host -- uploads to the lab's home host (the PC) need no per-plan digest.
Instead Cole approves the destination once, for at most ``MAX_APPROVAL_DAYS`` days: a
``HomeHostApproval`` binds the API endpoint, the tailnet suffix, the node's stable Tailscale
ID, ``tag:voice-lab`` and the file classes it may receive. It is written write-once at 0400
under ``provenance/home-host/approvals/`` after its preview's digest was reviewed, and named,
with that digest, by ``provenance/home-host/binding.json``. The upload client re-checks the
node against it before every session and appends a hash-chained ledger entry
(``provenance/home-host/ledger/<sequence>.json``) naming the session's manifest digest
before its first byte, and another one with the outcome.

This shape follows D-020's ``VaultDestinationApproval``. Any other destination still needs
AGENTS.md's separate per-payload approval; there is no hosted destination (D-028).
"""

from __future__ import annotations

import fcntl
import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, Literal, Self

from pydantic import Field, ValidationError, model_validator

from .canonical import (
    canonical_json_bytes,
    open_regular_no_follow,
    write_new_private_file,
)
from .errors import ContractError, SecurityError
from .models import ContractModel, Sha256, StableId
from .paths import AssetLayout
from .storage_contracts import (
    MAX_APPROVAL_DAYS,
    Endpoint,
    NodeStableId,
    NonBlank,
    TailnetName,
    UtcTimestamp,
)
from .tailnet import ExpectedIdentity

HOME_HOST_TAG: Final = "tag:voice-lab"
MAX_RECORD_BYTES: Final = 64 * 1024
type HomeHostClass = Literal["capture", "measurement"]


class HomeHostApproval(ContractModel):
    """Cole's standing authorization to upload captures to his own home host."""

    schema_version: Literal["cole-voice-lab.home-host-approval/v1"] = (
        "cole-voice-lab.home-host-approval/v1"
    )
    approval_id: StableId
    approved: Literal[True]
    endpoint: Endpoint
    tailnet_suffix: TailnetName
    node_stable_id: NodeStableId
    tag: Literal["tag:voice-lab"] = HOME_HOST_TAG
    classes: tuple[HomeHostClass, ...] = Field(min_length=1, max_length=2)
    approved_at: UtcTimestamp
    expires_at: UtcTimestamp
    approved_by: NonBlank = Field(max_length=256)
    statement: NonBlank = Field(max_length=4_096)

    @model_validator(mode="after")
    def _one_node_for_a_bounded_time(self) -> Self:
        if self.endpoint != self.endpoint.lower() or not self.endpoint.endswith(
            f".{self.tailnet_suffix}"
        ):
            raise ValueError("the endpoint must be a lowercase name in the approved tailnet")
        if list(self.classes) != sorted(set(self.classes)):
            raise ValueError("classes are sorted and unique")
        if self.expires_at <= self.approved_at:
            raise ValueError("expires_at must follow approved_at")
        if self.expires_at - self.approved_at > timedelta(days=MAX_APPROVAL_DAYS):
            raise ValueError(f"an approval stands for at most {MAX_APPROVAL_DAYS} days")
        return self

    def effective_at(self, now: datetime) -> bool:
        """Whether the approval stands at ``now``."""
        return self.approved_at <= now < self.expires_at

    def identity(self) -> ExpectedIdentity:
        """The node every upload session must reach."""
        return ExpectedIdentity(
            node_stable_id=self.node_stable_id, tag=self.tag, tailnet_suffix=self.tailnet_suffix
        )


class HomeHostBinding(ContractModel):
    """Which approval is in force: its id and the digest of the file that was reviewed."""

    schema_version: Literal["cole-voice-lab.home-host-binding/v1"] = (
        "cole-voice-lab.home-host-binding/v1"
    )
    approval_id: StableId
    approval_sha256: Sha256
    bound_at: UtcTimestamp


type LedgerEvent = Literal["upload-started", "upload-completed", "upload-refused"]


class HomeHostLedgerEntry(ContractModel):
    """One step of one upload, chained to the entry before it."""

    schema_version: Literal["cole-voice-lab.home-host-ledger-entry/v1"] = (
        "cole-voice-lab.home-host-ledger-entry/v1"
    )
    sequence: int = Field(ge=1)
    previous_sha256: Sha256 | None
    event: LedgerEvent
    at: UtcTimestamp
    approval_id: StableId
    approval_sha256: Sha256
    endpoint: Endpoint
    node_stable_id: NodeStableId
    session_id: StableId
    manifest_sha256: Sha256
    receipt_sha256: Sha256 | None = None
    detail: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _chained(self) -> Self:
        if (self.sequence == 1) != (self.previous_sha256 is None):
            raise ValueError("only the first entry has no previous entry")
        if (self.event == "upload-completed") != (self.receipt_sha256 is not None):
            raise ValueError("exactly a completed upload names its receipt")
        return self


def _directory(layout: AssetLayout, *parts: str) -> Path:
    directory = layout.directory("provenance").joinpath("home-host", *parts)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


def record_bytes(model: ContractModel) -> bytes:
    """The exact bytes a record is stored as (canonical JSON and a newline)."""
    return canonical_json_bytes(model) + b"\n"


def record_sha256(model: ContractModel) -> str:
    """The digest of a record's stored bytes: what a review, a binding or a chain names."""
    return hashlib.sha256(record_bytes(model)).hexdigest()


def _read[ModelT: ContractModel](path: Path, model: type[ModelT]) -> tuple[ModelT, str]:
    try:
        with open_regular_no_follow(path) as stream:
            data = stream.read(MAX_RECORD_BYTES + 1)
    except SecurityError as error:
        if not os.path.lexists(path):
            raise ContractError(f"no such home-host record: {path}") from error
        raise
    try:
        value = model.model_validate_json(data)
    except ValidationError as error:
        raise ContractError(f"invalid home-host record {path}: {error}") from error
    if record_bytes(value) != data:
        raise SecurityError(f"home-host record is not in canonical form: {path}")
    return value, hashlib.sha256(data).hexdigest()


def approve(
    layout: AssetLayout, approval: HomeHostApproval, *, reviewed_sha256: str, now: datetime
) -> HomeHostBinding:
    """Store a reviewed approval (0400, write-once) and bind it.

    Raises: ``SecurityError`` when ``reviewed_sha256`` is not the digest of exactly this
    approval, or the approval does not stand at ``now``; ``FileExistsError`` when an approval
    with this id exists already (approvals are never replaced; use a new id).
    """
    digest = record_sha256(approval)
    if reviewed_sha256 != digest:
        raise SecurityError("the reviewed digest is not this approval's digest")
    if not approval.effective_at(now):
        raise SecurityError("the approval does not stand now")
    path = _directory(layout, "approvals") / f"{approval.approval_id}.json"
    write_new_private_file(path, record_bytes(approval), mode=0o400)
    binding = HomeHostBinding(
        approval_id=approval.approval_id, approval_sha256=digest, bound_at=now
    )
    binding_path = _directory(layout) / "binding.json"
    replacement = binding_path.with_name(f".binding.json.{os.getpid()}")
    replacement.unlink(missing_ok=True)
    write_new_private_file(replacement, record_bytes(binding), mode=0o600)
    os.replace(replacement, binding_path)
    return binding


def load_approval(layout: AssetLayout, *, now: datetime) -> tuple[HomeHostApproval, str]:
    """The bound approval and its digest, checked against the binding and the clock.

    Raises: ``ContractError`` when nothing is bound; ``SecurityError`` when the approval file
    no longer has the bound digest, or the approval does not stand at ``now``.
    """
    binding, _ = _read(_directory(layout) / "binding.json", HomeHostBinding)
    approval, digest = _read(
        _directory(layout, "approvals") / f"{binding.approval_id}.json", HomeHostApproval
    )
    if digest != binding.approval_sha256:
        raise SecurityError("the home-host approval is not the one that was bound")
    if approval.approval_id != binding.approval_id:
        raise SecurityError("the home-host approval file names another approval")
    if not approval.effective_at(now):
        raise SecurityError(
            f"the home-host approval {approval.approval_id} does not stand now "
            f"(expires {approval.expires_at.isoformat()}); approve the destination again"
        )
    return approval, digest


class HomeHostLedger:
    """The chained, write-once upload ledger of one asset root."""

    def __init__(self, layout: AssetLayout) -> None:
        self.directory = _directory(layout, "ledger")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def entries(self) -> list[tuple[HomeHostLedgerEntry, str]]:
        """Every entry with its digest, in order; the chain is checked as it is read.

        Raises: ``SecurityError`` for a gap, a broken link or an unexpected file.
        """
        found: list[tuple[HomeHostLedgerEntry, str]] = []
        names = sorted(name for name in os.listdir(self.directory) if not name.startswith("."))
        for index, name in enumerate(names, start=1):
            if name != f"{index:08d}.json":
                raise SecurityError(f"the home-host ledger has a gap or a stray file: {name}")
            entry, digest = _read(self.directory / name, HomeHostLedgerEntry)
            previous = found[-1][1] if found else None
            if entry.sequence != index or entry.previous_sha256 != previous:
                raise SecurityError(f"the home-host ledger chain is broken at {name}")
            found.append((entry, digest))
        return found

    def append(self, **fields: object) -> HomeHostLedgerEntry:
        """Write the next entry (``sequence`` and ``previous_sha256`` are filled in)."""
        with self._locked():
            existing = self.entries()
            entry = HomeHostLedgerEntry.model_validate(
                {
                    **fields,
                    "sequence": len(existing) + 1,
                    "previous_sha256": existing[-1][1] if existing else None,
                }
            )
            write_new_private_file(
                self.directory / f"{entry.sequence:08d}.json", record_bytes(entry), mode=0o400
            )
            return entry


class HomeHostSignIn(ContractModel):
    """Which identity provider and public client this host signs in with (no secret)."""

    schema_version: Literal["cole-voice-lab.home-host-sign-in/v1"] = (
        "cole-voice-lab.home-host-sign-in/v1"
    )
    issuer: str = Field(min_length=9, max_length=512, pattern=r"^https://[^/]+(/[^\s]*[^/])?$")
    client_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")

    def keychain_account(self) -> str:
        """The keychain account of this host's refresh token."""
        return f"{self.client_id}@{self.issuer.removeprefix('https://')}"


def save_sign_in(layout: AssetLayout, sign_in: HomeHostSignIn) -> Path:
    """Record the sign-in settings (replacing earlier ones)."""
    path = _directory(layout) / "sign-in.json"
    replacement = path.with_name(f".sign-in.json.{os.getpid()}")
    replacement.unlink(missing_ok=True)
    write_new_private_file(replacement, record_bytes(sign_in), mode=0o600)
    os.replace(replacement, path)
    return path


def load_sign_in(layout: AssetLayout) -> HomeHostSignIn:
    """The recorded sign-in settings; ``ContractError`` when this host never signed in."""
    sign_in, _ = _read(_directory(layout) / "sign-in.json", HomeHostSignIn)
    return sign_in
