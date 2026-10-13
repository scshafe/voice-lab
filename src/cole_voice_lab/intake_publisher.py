"""The intake publisher: the only process that publishes raw audio on the home host (D-021).

@canonical intake_publisher -- one bounded pass over ``incoming/``. Each direct child that
is not a dot entry is a finalized capture package; ``accept_capture_session`` (unchanged,
intake.py) validates every byte and publishes it into ``raw/``. The outcome is recorded
where the API can read it:

- accepted: ``provenance/intake/sessions/<session_id>.json``, naming the receipt;
- refused (a contract or security failure): the package is moved, untouched, to
  ``incoming/.refused/<session_id>.<UTC time>`` and
  ``provenance/intake/refusals/<session_id>.json`` says why. Nothing is deleted.

An operating failure (an ``OSError``, the disk floor) leaves the package where it is for the
next pass.

Every pass also refreshes the corpus's read model (``corpus_read_model``, D-052): the PC's one
lab writer keeps what the read-only API serves current. A failure there is reported in the
pass's summary and never holds back a package.

On the PC this runs as a systemd host unit with no network
(``deploy/host/voice-lab-intake.service``), started by a path unit whenever ``incoming/``
changes; the API container cannot write ``raw/`` at all.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

from pydantic import Field, ValidationError

from .canonical import open_regular_no_follow, write_canonical_json
from .capture import READY_NAME, ReadyMarker
from .corpus_read_model import refresh_active_catalog
from .errors import ContractError, SecurityError
from .intake import MIN_FREE_BYTES, IntakeReceipt, accept_capture_session
from .models import ContractModel, Sha256, StableId, utc_now
from .paths import AssetLayout

REFUSED_NAME: Final = ".refused"
MAX_RECORD_BYTES: Final = 1024 * 1024


class IntakeAcceptanceRecord(ContractModel):
    """One accepted session: which receipt the publisher wrote, and when."""

    schema_version: Literal["cole-voice-lab.intake-acceptance/v1"] = (
        "cole-voice-lab.intake-acceptance/v1"
    )
    session_id: StableId
    manifest_sha256: Sha256
    accepted_at: datetime
    receipt: IntakeReceipt


class IntakeRefusalRecord(ContractModel):
    """One refused package: why, and where it was set aside."""

    schema_version: Literal["cole-voice-lab.intake-refusal/v1"] = "cole-voice-lab.intake-refusal/v1"
    session_id: str = Field(min_length=1, max_length=255)
    manifest_sha256: Sha256 | None = None
    refused_at: datetime
    error: str = Field(max_length=2000)
    quarantined_as: str = Field(min_length=1, max_length=512)


@dataclass(slots=True)
class PublishSummary:
    """What one pass did."""

    accepted: list[IntakeReceipt] = field(default_factory=list)
    refused: list[IntakeRefusalRecord] = field(default_factory=list)
    deferred: list[dict[str, str]] = field(default_factory=list)
    #: The corpus read model after the pass: ``"current"``, ``"no catalog"`` or the error.
    read_model: str = "current"


def _records(layout: AssetLayout, kind: str) -> Path:
    directory = layout.directory("provenance") / "intake" / kind
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


def read_refusal(layout: AssetLayout, session_id: str) -> IntakeRefusalRecord | None:
    """The latest refusal recorded for ``session_id``, or ``None``."""
    path = layout.directory("provenance") / "intake" / "refusals" / f"{session_id}.json"
    if not path.exists():
        return None
    with open_regular_no_follow(path) as stream:
        return IntakeRefusalRecord.model_validate_json(stream.read(MAX_RECORD_BYTES))


def _manifest_digest(package: Path) -> str | None:
    """The manifest digest a package's READY marker names, when it can be read at all."""
    try:
        with open_regular_no_follow(package / READY_NAME) as stream:
            return ReadyMarker.model_validate_json(stream.read(MAX_RECORD_BYTES)).manifest_sha256
    except (SecurityError, ValidationError, OSError, ValueError):
        return None


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def _quarantine(
    layout: AssetLayout, package: Path, error: Exception, clock: Callable[[], datetime]
) -> IntakeRefusalRecord:
    refused_at = clock()
    refused_root = layout.directory("incoming") / REFUSED_NAME
    if refused_root.is_symlink():
        raise SecurityError(f"the refused-package directory is a link: {refused_root}")
    refused_root.mkdir(mode=0o700, exist_ok=True)
    manifest = _manifest_digest(package) if not package.is_symlink() else None
    target = refused_root / f"{package.name}.{_stamp(refused_at)}"
    os.rename(package, target)
    record = IntakeRefusalRecord(
        session_id=package.name,
        manifest_sha256=manifest,
        refused_at=refused_at,
        error=str(error)[:2000],
        quarantined_as=f"incoming/{REFUSED_NAME}/{target.name}",
    )
    write_canonical_json(_records(layout, "refusals") / f"{package.name}.json", record)
    return record


def publish_pending(
    layout: AssetLayout,
    *,
    min_free_bytes: int = MIN_FREE_BYTES,
    clock: Callable[[], datetime] = utc_now,
) -> PublishSummary:
    """Validate and publish every finalized package in ``incoming/``, once.

    Raises: ``SecurityError`` when the disk is below the intake floor (nothing is touched).
    """
    free = shutil.disk_usage(layout.root).free
    if free < min_free_bytes:
        raise SecurityError(f"intake disk floor not met: {free} bytes free")
    summary = PublishSummary()
    for candidate in sorted(layout.directory("incoming").iterdir()):
        if candidate.name.startswith("."):
            continue
        try:
            receipt = accept_capture_session(layout, candidate, min_free_bytes=min_free_bytes)
        except (ContractError, SecurityError, ValidationError, ValueError) as error:
            if not os.path.lexists(candidate):
                summary.deferred.append({"session": candidate.name, "error": str(error)})
                continue
            summary.refused.append(_quarantine(layout, candidate, error, clock))
        except OSError as error:
            summary.deferred.append({"session": candidate.name, "error": str(error)})
        else:
            summary.accepted.append(receipt)
            write_canonical_json(
                _records(layout, "sessions") / f"{receipt.session_id}.json",
                IntakeAcceptanceRecord(
                    session_id=receipt.session_id,
                    manifest_sha256=receipt.manifest_sha256,
                    accepted_at=receipt.accepted_at,
                    receipt=receipt,
                ),
            )
    summary.read_model = _refresh_read_model(layout, clock)
    return summary


def _refresh_read_model(layout: AssetLayout, clock: Callable[[], datetime]) -> str:
    try:
        record = refresh_active_catalog(layout, clock=clock)
    except (ContractError, SecurityError, OSError) as error:
        return f"error: {error}"
    return "current" if record is not None else "no catalog"
