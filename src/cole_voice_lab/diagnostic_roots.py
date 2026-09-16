"""Purpose boundaries for physical-test catalogs and diagnostic asset roots.

@canonical diagnostic_roots -- the root marker, the physical-test suite-ID prefix and the
diagnostic subject prefix are self-asserted accident protection, not tamper evidence. A
marker is written only when its root is created and is never added or removed later.
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import field_validator

from .canonical import write_canonical_json
from .errors import ContractError, SecurityError
from .models import (
    PHYSICAL_TEST_SUITE_PREFIX,
    CaptureManifest,
    ContractModel,
    PromptSuite,
    StableId,
    utc_now,
)
from .paths import ASSET_DIRECTORIES, AssetLayout, confined_path

DIAGNOSTIC_ROOT_MARKER = "provenance/diagnostic-root.json"
DIAGNOSTIC_SUBJECT_PREFIX = "cole-physical-diagnostic-"
PHYSICAL_TEST_CATALOG_REFUSAL = (
    "physical-test catalogs require an asset root created with "
    "voice-lab assets init --diagnostic physical-test"
)
DIAGNOSTIC_ROOT_REFUSAL = (
    "this asset root is marked for physical tests; open it only with a physical-test catalog"
)
DIAGNOSTIC_SUBJECT_REFUSAL = "this diagnostic asset root belongs to another subject"

type RootPurpose = Literal["standard", "physical-test"]


class DiagnosticRootMarker(ContractModel):
    """Immutable private evidence that an asset root holds physical-test material only."""

    schema_version: Literal["cole-voice-lab.diagnostic-root/v1"] = (
        "cole-voice-lab.diagnostic-root/v1"
    )
    purpose: Literal["physical-test"]
    subject_id: StableId
    created_at: datetime

    @field_validator("subject_id")
    @classmethod
    def _diagnostic_subject(cls, value: str) -> str:
        if not is_diagnostic_subject(value):
            raise ValueError(
                f"diagnostic root subjects must start with {DIAGNOSTIC_SUBJECT_PREFIX}"
            )
        return value

    @field_validator("created_at")
    @classmethod
    def _timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("diagnostic root creation time requires an explicit timezone")
        return value.astimezone(UTC)


def is_physical_test_suite_id(suite_id: str) -> bool:
    """Every physical-test manifest carries this prefix, including native subsets."""
    return suite_id.startswith(PHYSICAL_TEST_SUITE_PREFIX)


def is_physical_test_catalog(catalog: PromptSuite) -> bool:
    return catalog.purpose == "physical-test" or is_physical_test_suite_id(catalog.suite_id)


def is_diagnostic_subject(subject_id: str) -> bool:
    return subject_id.startswith(DIAGNOSTIC_SUBJECT_PREFIX)


def read_diagnostic_root_marker(layout: AssetLayout) -> DiagnosticRootMarker | None:
    """Return the marker, None for a standard root, or refuse any other on-disk state."""
    path = confined_path(layout.root, DIAGNOSTIC_ROOT_MARKER)
    try:
        metadata = path.lstat()
    except OSError as error:
        if error.errno == errno.ENOENT:
            return None
        raise SecurityError(f"diagnostic root marker cannot be inspected: {error}") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise SecurityError("diagnostic root marker must be one private regular file")
    # recording_takes imports this module; its exact-canonical reader is bound at call time.
    from .recording_takes import read_recording_evidence

    try:
        return read_recording_evidence(path, DiagnosticRootMarker)
    except ContractError as error:
        raise SecurityError("diagnostic root marker is not valid canonical evidence") from error


def root_purpose(layout: AssetLayout) -> RootPurpose:
    marker = read_diagnostic_root_marker(layout)
    return "standard" if marker is None else marker.purpose


def require_root_purpose(
    layout: AssetLayout, purpose: RootPurpose, *, subject_id: str | None = None
) -> DiagnosticRootMarker | None:
    """Refuse a root of another purpose; a marked root also binds its one subject."""
    marker = read_diagnostic_root_marker(layout)
    actual: RootPurpose = "standard" if marker is None else marker.purpose
    if actual != purpose:
        raise ContractError(
            PHYSICAL_TEST_CATALOG_REFUSAL if purpose == "physical-test" else DIAGNOSTIC_ROOT_REFUSAL
        )
    if marker is not None and subject_id is not None and marker.subject_id != subject_id:
        raise ContractError(DIAGNOSTIC_SUBJECT_REFUSAL)
    return marker


def require_catalog_root(layout: AssetLayout, catalog: PromptSuite, subject_id: str) -> None:
    """Guards #2-#4: root purpose is physical-test iff the catalog's purpose is.

    A standard root refuses a physical-test purpose or suite-ID prefix; a marked root
    requires both, so a prefix/purpose disagreement fails closed in either root.
    """
    if read_diagnostic_root_marker(layout) is None:
        if is_physical_test_catalog(catalog):
            raise ContractError(PHYSICAL_TEST_CATALOG_REFUSAL)
        return
    if catalog.purpose != "physical-test" or not is_physical_test_suite_id(catalog.suite_id):
        raise ContractError(DIAGNOSTIC_ROOT_REFUSAL)
    require_root_purpose(layout, "physical-test", subject_id=subject_id)


def require_corpus_eligible_package(layout: AssetLayout, manifest: CaptureManifest) -> None:
    """Corpus readers and writers accept no physical-test root, suite or diagnostic subject."""
    if root_purpose(layout) != "standard":
        raise ContractError("physical-test asset roots never supply corpus evidence")
    if is_physical_test_suite_id(manifest.prompt_suite_id):
        raise ContractError("physical-test capture packages never become corpus evidence")
    if is_diagnostic_subject(manifest.subject_id):
        raise ContractError("diagnostic subjects never supply corpus evidence")


def initialize_diagnostic_root(
    root: Path, *, subject_id: str, clock: Callable[[], datetime] = utc_now
) -> tuple[AssetLayout, DiagnosticRootMarker]:
    """Create a marked root; an existing root qualifies only while every layout member is empty.

    No consent record is installed: diagnostic subjects can never approve consent.
    """
    marker = DiagnosticRootMarker(
        purpose="physical-test", subject_id=subject_id, created_at=clock()
    )
    candidate = Path(os.path.abspath(root.expanduser()))
    if os.path.lexists(candidate):
        for name in ASSET_DIRECTORIES:
            member = candidate / name
            if not os.path.lexists(member):
                continue
            if member.is_symlink() or not member.is_dir() or any(member.iterdir()):
                raise ContractError(
                    "a diagnostic marker is written only when its asset root is created; "
                    f"{name}/ is not empty"
                )
    layout = AssetLayout.initialize(candidate)
    write_canonical_json(confined_path(layout.root, DIAGNOSTIC_ROOT_MARKER), marker, mode=0o400)
    if read_diagnostic_root_marker(layout) != marker:
        raise SecurityError("diagnostic root marker did not read back exactly")
    return layout, marker
