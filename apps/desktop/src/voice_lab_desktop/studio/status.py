"""The studio's status line: the sync, from the corpus client's ``SyncStatus``, and its notices.

One line, always there (it never moves the layout). Its sync half says, in order of weight:

- take directories here that cannot be read (``damaged``), and recordings an earlier run left
  that could not be saved (``unsaved_recordings``): kept on disk, never sent;
- a lost sign-in;
- sending held until spike S-1 passes (``corpus_sync`` off): a plain statement, not an error
  (with "Offline" before it when the PC did not answer either);
- offline, with what is queued and when the PC last answered;
- uploads waiting for the home-host approval (none stands for this server, D-021): "Uploads
  need your OK", with the Approve action beside it (``Line.action``);
- the takes waiting for the PC's state after a refusal (``refreshing``);
- otherwise what is queued and what was sent.

A notice the client keeps for the operator (``changed-elsewhere``, ``change-blocked``, a
refusal) is shown instead until it is dismissed, the newest first.

Each half wraps to two lines at most, so a line says its cause in a few words; what it
leaves out is its ``detail`` (the client's whole message), which the screen shows on request
(the sync half's Details) or in the take's place (the take half).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

from ..corpus.records import Notice
from ..corpus.status import SyncStatus

type Tone = Literal["neutral", "info", "ok", "warn", "danger"]

HELD: Final = "Uploads off until S-1 passes (corpus_sync)"
NEEDS_APPROVAL: Final = "Uploads need your OK"
#: The action a line offers beside it: Approve uploads to my PC (``approval.py``).
APPROVE: Final = "approve"
NOTICE_TONES: Final[dict[str, Tone]] = {
    "changed-elsewhere": "warn",
    "change-blocked": "warn",
    "change-refused": "warn",
    "take-refused": "danger",
    "local-only": "info",
}


#: What each kind of notice is, in a few words (the notice's own text is its detail).
NOTICE_SUMMARIES: Final[dict[str, str]] = {
    "changed-elsewhere": "changed elsewhere",
    "change-blocked": "a change waits: the PC cannot read it",
    "change-refused": "the PC refused a change",
    "take-refused": "the PC refused this take",
    "local-only": "kept on this machine only",
}
_CODE: Final = re.compile(r"\(([a-z_]+)\)")


@dataclass(frozen=True)
class Line:
    """A short line (``text``, at most two lines on the smallest window), when it leaves
    something out the whole message (``detail``), and the action it offers beside it
    (``action``: ``APPROVE``, or ``""``)."""

    text: str
    tone: Tone = "neutral"
    detail: str = ""
    action: str = ""


def _count(count: int, one: str, many: str | None = None) -> str:
    return f"{count:,} {one if count == 1 else (many or one + 's')}"


def _clock(moment: datetime) -> str:
    return moment.astimezone().strftime("%H:%M")


def sync_line(status: SyncStatus) -> Line:
    """The sync half of the status line."""
    if status.damaged:
        unreadable = _count(len(status.damaged), "take")
        return Line(f"{unreadable} here cannot be read: kept, never sent", "danger")
    if status.unsaved_recordings:
        left = _count(len(status.unsaved_recordings), "recording")
        return Line(f"{left} from an earlier run could not be saved: kept on disk", "warn")
    if status.sign_in_required:
        return Line("Sign in on the Status screen to sync with the PC", "warn")
    counts: list[str] = []
    if status.queued:
        counts.append(f"{status.queued:,} queued")
    if status.sent:
        counts.append(f"{status.sent:,} sent")
    if not status.enabled:
        here = _count(status.takes, "take") + " here"
        offline = ["Offline"] if status.offline else []
        return Line(" · ".join([*offline, HELD, here]), "neutral")
    if status.offline:
        parts = ["Offline", *counts]
        if status.last_sync_at is not None:
            parts.append(f"last synced {_clock(status.last_sync_at)}")
        return Line(" · ".join(parts), "neutral")
    if status.upload_needs_approval:
        detail = f"{NEEDS_APPROVAL}: {status.upload_blocked}"
        return Line(" · ".join([NEEDS_APPROVAL, *counts]), "warn", detail, APPROVE)
    if status.refreshing:
        parts = [_count(status.refreshing, "take") + " waiting for the PC's state", *counts]
        return Line(" · ".join(parts), "info")
    if status.upload_blocked and status.queued:
        return Line(
            "Uploads cannot start", "warn", f"Uploads cannot start: {status.upload_blocked}"
        )
    lead = "Syncing" if status.busy else ("Waiting to sync" if status.queued else "Up to date")
    parts = [lead, *counts]
    if status.local_only:
        parts.append(f"{status.local_only:,} local-only")
    return Line(" · ".join(parts), "ok" if lead == "Up to date" else "info")


def notice_line(notices: tuple[Notice, ...], title_of: dict[str, str]) -> Line | None:
    """The newest notice (``title_of``: prompt ids to titles), with how many more wait."""
    if not notices:
        return None
    notice = notices[-1]
    title = title_of.get(notice.prompt_id, notice.prompt_id)
    summary = NOTICE_SUMMARIES.get(notice.kind, "a notice")
    code = _CODE.search(notice.text)
    if notice.kind == "change-refused" and code is not None:
        summary += f" ({code[1]})"
    more = f" (+{len(notices) - 1} more)" if len(notices) > 1 else ""
    tone = NOTICE_TONES.get(notice.kind, "warn")
    return Line(f"{title}: {summary}{more}", tone, f"{title}: {notice.text}")


def catalog_line(status: SyncStatus) -> Line:
    """The library's catalog line: the version this session records against (the line below
    it, ``switch_line``, says what the next session does)."""
    pinned = status.pinned_catalog
    if pinned is None:
        return Line("No catalog yet", "warn")
    return Line(f"{pinned.catalog_id} · v{pinned.catalog_version}")


def switch_line(status: SyncStatus) -> str:
    if status.switch_pending and status.pc_catalog is not None:
        version = status.pc_catalog.catalog_version
        return f"Version {version} is active on the PC: it switches at the next session"
    if status.pinned_catalog is None:
        return "Recording starts once the PC has answered"
    if status.pc_catalog_unavailable:
        return "The PC has no active catalog now: recording goes on with this one"
    return ""
