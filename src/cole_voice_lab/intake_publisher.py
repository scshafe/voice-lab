"""The intake publisher: the only process that publishes raw audio on the home host (D-021).

@canonical intake_publisher -- one bounded pass over ``incoming/``. Each direct child that
is not a dot entry is a finalized capture package or take package (``corpus_takes``, D-052;
``intake.validate_package`` tells them apart by the manifest's schema);
``accept_capture_session`` (intake.py) validates every byte and publishes it into ``raw/``.
A take is published only once ``corpus_takes.admit_take`` admits it (real audio, and a prompt
its catalog version, a snapshot on this server, counts). The outcome is recorded where
the API can read it:

- accepted: ``provenance/intake/sessions/<session_id>.json``, naming the receipt;
- refused (a contract or security failure): the package is moved, untouched, to
  ``incoming/.refused/<session_id>.<UTC time>`` and
  ``provenance/intake/refusals/<session_id>.json`` says why. Nothing is deleted.

An operating failure (an ``OSError``, the disk floor) leaves the package where it is for the
next pass, and so does a take the PC cannot judge yet (``corpus_takes.TakeDeferral``: its
catalog version is not on this server, or the server's copy cannot be read). Refusing either
would be final, since a take's upload session is its audio's digest.

Every pass then writes the take store's record of each new take
(``corpus_take_store.record_takes``: ``provenance/corpus-takes/<take_id>.json``, an undecided
draft), applies the decisions and trims the API queued in ``incoming/``
(``corpus_requests.apply_requests``: in the order they arrived, each only at the revision its
client expected, its outcome in ``provenance/corpus-requests/``), and refreshes the corpus's
read model (``corpus_read_model``: the active catalog and its library): the PC's one lab
writer keeps what the read-only API serves current. A failure there is reported in the
pass's summary and never holds back a package; a request that cannot be applied yet waits
for the next pass.

On the PC this runs as a systemd host unit with no network
(``deploy/host/voice-lab-intake.service``), started by a path unit whenever ``incoming/``
changes; the API container cannot write ``raw/`` at all.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

from pydantic import Field, ValidationError

from .canonical import open_regular_no_follow, write_canonical_json
from .capture import READY_NAME, ReadyMarker
from .corpus_read_model import refresh_active_catalog, refresh_library
from .corpus_requests import RequestPass, apply_requests, queued_requests
from .corpus_take_store import CorpusTakeState, record_takes
from .corpus_takes import TakeDeferral
from .errors import ContractError, SecurityError, VoiceLabError
from .intake import MIN_FREE_BYTES, IntakeReceipt, accept_capture_session
from .models import ContractModel, Sha256, StableId, utc_now
from .paths import AssetLayout

REFUSED_NAME: Final = ".refused"
#: ``provenance/intake/.publisher.lock``: one pass at a time (classified with intake's records).
LOCK_NAME: Final = ".publisher.lock"
#: How long a pass waits for one already running (the unit's and a hand-run one) to end.
LOCK_WAIT_SECONDS: Final = 3600.0
MAX_RECORD_BYTES: Final = 1024 * 1024
#: How many times a pass applies the queued requests and rebuilds the library while requests
#: keep arriving during it.
REQUEST_ROUNDS: Final = 3


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
    #: The takes whose records this pass wrote.
    takes_recorded: list[str] = field(default_factory=list)
    #: The corpus requests (decisions and trims, ``corpus_requests``) this pass applied (their
    #: ids), refused (``{"request", "take", "code"}``) and left queued for a later pass
    #: (``{"request", "take", "reason"}``: most often a take not recorded yet).
    requests_applied: list[str] = field(default_factory=list)
    requests_refused: list[dict[str, str]] = field(default_factory=list)
    requests_waiting: list[dict[str, str]] = field(default_factory=list)
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
        with open_regular_no_follow(package / READY_NAME, nonblocking=True) as stream:
            return ReadyMarker.model_validate_json(stream.read(MAX_RECORD_BYTES)).manifest_sha256
    except (SecurityError, ValidationError, OSError, ValueError):
        return None


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def _refused_root(layout: AssetLayout) -> Path:
    """``incoming/.refused``, made when missing. Raises: ``SecurityError`` for a link."""
    refused_root = layout.directory("incoming") / REFUSED_NAME
    if refused_root.is_symlink():
        raise SecurityError(f"the refused-package directory is a link: {refused_root}")
    refused_root.mkdir(mode=0o700, exist_ok=True)
    return refused_root


def _quarantine(
    layout: AssetLayout, package: Path, error: Exception, clock: Callable[[], datetime]
) -> IntakeRefusalRecord:
    refused_at = clock()
    refused_root = _refused_root(layout)
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


class PublisherBusy(VoiceLabError):
    """Another publisher pass held the lock for longer than this one would wait."""


@contextmanager
def one_pass_at_a_time(
    layout: AssetLayout, wait_seconds: float = LOCK_WAIT_SECONDS
) -> Iterator[None]:
    """Hold the publisher's lock (``provenance/intake/.publisher.lock``, ``flock``) for a pass.

    Two passes at once (the systemd unit and ``voice-lab intake publish`` run by hand) would
    interleave a request's outcome and its take record and lose a change, so a second pass
    waits for the first, polling, for at most ``wait_seconds``. The kernel releases the lock
    when the holder exits, however it ends.
    Raises: ``PublisherBusy`` after the wait; ``SecurityError`` when the lock is not a
    regular file."""
    directory = layout.directory("provenance") / "intake"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(
        directory / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise SecurityError(f"the publisher's lock is not a regular file: {LOCK_NAME}")
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise PublisherBusy(
                        f"another intake pass has held {LOCK_NAME} for over {wait_seconds:g} s"
                    ) from None
                time.sleep(0.2)
        yield
    finally:
        os.close(descriptor)


def publish_pending(
    layout: AssetLayout,
    *,
    min_free_bytes: int = MIN_FREE_BYTES,
    clock: Callable[[], datetime] = utc_now,
    lock_wait_seconds: float = LOCK_WAIT_SECONDS,
) -> PublishSummary:
    """Validate and publish every finalized package in ``incoming/``, once, one pass at a time
    (``one_pass_at_a_time``).

    Raises: ``SecurityError`` when the disk is below the intake floor (nothing is touched);
    ``PublisherBusy`` when another pass keeps the lock past ``lock_wait_seconds``.
    """
    with one_pass_at_a_time(layout, lock_wait_seconds):
        return _publish_pending(layout, min_free_bytes=min_free_bytes, clock=clock)


def _publish_pending(
    layout: AssetLayout, *, min_free_bytes: int, clock: Callable[[], datetime]
) -> PublishSummary:
    free = shutil.disk_usage(layout.root).free
    if free < min_free_bytes:
        raise SecurityError(f"intake disk floor not met: {free} bytes free")
    summary = PublishSummary()
    for candidate in sorted(layout.directory("incoming").iterdir()):
        if candidate.name.startswith("."):
            continue
        try:
            receipt = accept_capture_session(
                layout, candidate, min_free_bytes=min_free_bytes, clock=clock
            )
        except TakeDeferral as error:
            summary.deferred.append({"session": candidate.name, "error": str(error)})
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
    summary.read_model = _refresh_read_model(layout, summary, clock)
    return summary


def _refresh_read_model(
    layout: AssetLayout, summary: PublishSummary, clock: Callable[[], datetime]
) -> str:
    # The takes' records, then the requests that change them, then the read model. A take
    # store that fails its checks holds back only the library, and makes a request whose take
    # has no record wait instead of being refused. Requests that arrive while the library is
    # rebuilt are applied in another round: the path unit does not see what changes while a
    # pass runs.
    states: dict[str, CorpusTakeState] | None
    try:
        recorded_states, recorded = record_takes(layout)
        summary.takes_recorded.extend(recorded)
        states = {state.take_id: state for state in recorded_states}
        takes_error = None
    except (ContractError, SecurityError, ValidationError, OSError) as error:
        states, takes_error = None, error
    requests = RequestPass()
    read_model = "current"
    for _round in range(REQUEST_ROUNDS):
        try:
            apply_requests(
                layout,
                refused_root=lambda: _refused_root(layout),
                records_complete=takes_error is None,
                clock=clock,
                into=requests,
            )
        except (SecurityError, OSError) as error:
            requests.wait("", "", str(error))
        if states is not None:
            states.update(requests.updated)
        read_model = _rebuild_read_model(layout, states, takes_error, clock)
        if not _requests_arrived(layout, requests):
            break
    summary.requests_applied.extend(requests.applied)
    summary.requests_refused.extend(requests.refused)
    summary.requests_waiting.extend(requests.waiting)
    return read_model


def _rebuild_read_model(
    layout: AssetLayout,
    states: dict[str, CorpusTakeState] | None,
    takes_error: Exception | None,
    clock: Callable[[], datetime],
) -> str:
    try:
        record = refresh_active_catalog(layout, clock=clock)
        if takes_error is not None:
            return f"error: {takes_error}"
        if record is not None and states is not None:
            refresh_library(layout, record, tuple(states[take] for take in sorted(states)))
    except (ContractError, SecurityError, ValidationError, OSError) as error:
        return f"error: {error}"
    return "current" if record is not None else "no catalog"


def _requests_arrived(layout: AssetLayout, requests: RequestPass) -> bool:
    """Whether a request the pass has not handled waits in ``incoming/`` now."""
    try:
        return any(entry.name not in requests.seen for entry in queued_requests(layout))
    except OSError:
        return False
