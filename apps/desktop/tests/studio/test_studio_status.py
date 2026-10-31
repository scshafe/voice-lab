"""The status line's words for each ``SyncStatus`` the corpus client reports: queued and sent
counts, offline, the takes waiting for the PC's state, damaged takes and unsaved recordings,
the notices (changed elsewhere, change blocked), uploads waiting for the home-host approval,
and sending held until S-1 as a plain statement, not an error; the sync badge's word or two
(D-058, item 9) and the one action beside it (Approve or Sync now; the badge itself shows the
whole line). Then the same in the window, from the client's status, on the smallest window."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import Any

from conftest import Window
from voice_lab_desktop.corpus.records import Notice
from voice_lab_desktop.corpus.status import HELD_FOR_S1, NO_CATALOG, SyncStatus
from voice_lab_desktop.studio import status
from voice_lab_desktop.studio.status import Line

from cole_voice_lab.recording_contracts import CatalogReference

from .test_studio_screen import click

MOMENT = datetime(2026, 10, 12, 9, 30, tzinfo=UTC)
TAKE = "take-" + "a" * 64


def reference(version: int, digest: str) -> CatalogReference:
    sha = digest * 64
    return CatalogReference(
        catalog_id="cole-corpus",
        catalog_version=version,
        catalog_sha256=sha,
        snapshot_sha256=sha,
        suite_id="cole-corpus",
        purpose="capture-pilot",
        snapshot_path=f"provenance/corpus/snapshots/{sha}.json",
    )


BASE = SyncStatus(
    enabled=True,
    held="",
    busy=False,
    offline=False,
    sign_in_required=False,
    last_sync_at=MOMENT,
    last_attempt_at=MOMENT,
    last_error="",
    upload_blocked="",
    takes=4,
    queued=0,
    waiting_takes=0,
    refreshing=0,
    awaiting_receipt=0,
    sent=6,
    refused=0,
    dropped=0,
    local_only=0,
    damaged=(),
    unsaved_recordings=(),
    pinned_catalog=reference(1, "1"),
    pc_catalog=reference(1, "1"),
    pc_catalog_unavailable=False,
    switch_pending=False,
    can_record=True,
    record_blocked="",
    notices=(),
)


def given(**changes: Any) -> SyncStatus:
    return dataclasses.replace(BASE, **changes)


def notice(kind: str, text: str, number: int = 1) -> Notice:
    return Notice(
        notice_id=f"notice-{number:016x}",
        take_id=TAKE,
        prompt_id="neutral-01",
        kind=kind,  # type: ignore[arg-type]
        text=text,
        at=MOMENT,
    )


def plain_sync(value: SyncStatus) -> Line:
    """The sync line without its badge (the badges: ``test_the_sync_badge_for_each_state``)."""
    return dataclasses.replace(status.sync_line(value), badge="")


def plain_notice(notices: tuple[Notice, ...], titles: dict[str, str]) -> Line | None:
    line = status.notice_line(notices, titles)
    return None if line is None else dataclasses.replace(line, badge="")


def test_the_sync_badge_for_each_state() -> None:
    """The badge's word or two (D-058, item 9), the whole line behind it."""
    badges = [
        (BASE, "Up to date"),
        (given(queued=3), "3 queued"),
        (given(queued=3, busy=True), "Syncing"),
        (given(offline=True, queued=2), "Offline"),
        (given(refreshing=1, queued=1), "Waiting for the PC"),
        (given(queued=1, upload_blocked="the settings name another server"), "Uploads blocked"),
        (given(sign_in_required=True), "Signed out"),
        (given(upload_needs_approval=True), "Needs your OK"),
        (given(enabled=False, held=HELD_FOR_S1), "Uploads off"),
        (given(unsaved_recordings=("rec-1",)), "Not saved"),
        (given(damaged=(TAKE,)), "Damaged takes"),
    ]
    assert [status.sync_line(value).badge for value, _ in badges] == [b for _, b in badges]
    one = notice("changed-elsewhere", "Changed elsewhere.", 1)
    two = notice("change-blocked", "The PC cannot read this change.", 2)
    assert status.notice_line((one,), {}).badge == "A notice"  # type: ignore[union-attr]
    assert status.notice_line((one, two), {}).badge == "2 notices"  # type: ignore[union-attr]


def test_the_sync_line_for_each_state() -> None:
    clock = MOMENT.astimezone().strftime("%H:%M")
    assert plain_sync(BASE) == Line("Up to date · 6 sent", "ok")
    # Sync now beside what waits for a pass: queued operations, offline, refreshing.
    assert plain_sync(given(queued=3)) == Line(
        "Waiting to sync · 3 queued · 6 sent", "info", action="sync"
    )
    assert plain_sync(given(queued=3, busy=True)) == Line("Syncing · 3 queued · 6 sent", "info")
    assert plain_sync(given(local_only=2)) == Line("Up to date · 6 sent · 2 local-only", "ok")
    assert plain_sync(given(offline=True, queued=2)) == Line(
        f"Offline · 2 queued · 6 sent · last synced {clock}", "neutral", action="sync"
    )
    assert plain_sync(given(offline=True, sent=0, last_sync_at=None)) == Line(
        "Offline", action="sync"
    )
    assert plain_sync(given(refreshing=1, queued=1)) == Line(
        "1 take waiting for the PC's state · 1 queued · 6 sent", "info", action="sync"
    )
    # A long cause: the line says what, its detail says why, whole.
    blocked = "no home-host approval is bound on this machine yet; approve the home host once"
    assert plain_sync(given(queued=1, upload_blocked=blocked)) == Line(
        "Uploads cannot start", "warn", f"Uploads cannot start: {blocked}"
    )
    assert plain_sync(given(sign_in_required=True)) == Line(
        "Sign in on the Status screen to sync with the PC", "warn"
    )
    # Uploads wait for the home-host approval: said plainly, queued or not, with Approve in
    # Details' place (the approval dialog says the rest).
    unbound = given(upload_needs_approval=True, upload_blocked=blocked)
    assert plain_sync(unbound) == Line("Uploads need your OK · 6 sent", "warn", "", "approve")
    assert plain_sync(dataclasses.replace(unbound, queued=2)).text == (
        "Uploads need your OK · 2 queued · 6 sent"
    )
    # Held until S-1, or offline, comes first: nothing would be sent anyway (offline offers
    # Sync now).
    assert plain_sync(dataclasses.replace(unbound, enabled=False)).action == ""
    assert plain_sync(dataclasses.replace(unbound, offline=True)).action == "sync"
    # Held until S-1: said plainly, with what stays here, whatever else is true.
    held = given(enabled=False, held=HELD_FOR_S1, queued=4, offline=True)
    assert plain_sync(held) == Line(
        "Offline · Uploads off until S-1 passes (corpus_sync) · 4 takes here",
        "neutral",
    )
    assert plain_sync(given(enabled=False, held=HELD_FOR_S1)) == Line(
        "Uploads off until S-1 passes (corpus_sync) · 4 takes here", "neutral"
    )
    assert plain_sync(given(enabled=False, takes=1)).text.endswith("1 take here")
    # What cannot be read or saved comes first.
    assert plain_sync(given(unsaved_recordings=("rec-1", "rec-2"))) == Line(
        "2 recordings from an earlier run could not be saved: kept on disk", "warn"
    )
    assert plain_sync(given(damaged=(TAKE,), unsaved_recordings=("rec-1",))) == Line(
        "1 take here cannot be read: kept, never sent", "danger"
    )


def test_notices_show_the_newest_until_dismissed() -> None:
    titles = {"neutral-01": "Neutral 01"}
    assert plain_notice((), titles) is None
    elsewhere = notice(
        "changed-elsewhere", "Changed elsewhere: this take now shows the PC's state."
    )
    assert plain_notice((elsewhere,), titles) == Line(
        "Neutral 01: changed elsewhere",
        "warn",
        "Neutral 01: Changed elsewhere: this take now shows the PC's state.",
    )
    blocked = notice("change-blocked", "The PC cannot read this change (invalid_request).", 2)
    line = plain_notice((elsewhere, blocked), titles)
    assert line == Line(
        "Neutral 01: a change waits: the PC cannot read it (+1 more)",
        "warn",
        "Neutral 01: The PC cannot read this change (invalid_request).",
    )
    refused = notice("take-refused", "The PC refused this take: refused_by_intake", 3)
    assert plain_notice((refused,), {}) == Line(
        "neutral-01: the PC refused this take",
        "danger",
        "neutral-01: The PC refused this take: refused_by_intake",
    )
    change = notice(
        "change-refused",
        "The PC refused a change of this take (trim_out_of_bounds): the PC's state for it is "
        "read again before anything more is sent; 2 queued change(s) were dropped.",
        4,
    )
    line = plain_notice((change,), titles)
    assert line is not None
    assert line.text == "Neutral 01: the PC refused a change (trim_out_of_bounds)"
    assert line.detail.endswith("2 queued change(s) were dropped.")


def test_the_catalog_lines() -> None:
    assert status.catalog_line(BASE) == Line("cole-corpus · v1")
    assert status.switch_line(BASE) == ""
    switching = given(pc_catalog=reference(2, "2"), switch_pending=True)
    assert status.catalog_line(switching) == Line("cole-corpus · v1")
    assert (
        status.switch_line(switching)
        == "Version 2 is active on the PC: it switches at the next session"
    )
    nothing = given(
        pinned_catalog=None, pc_catalog=None, can_record=False, record_blocked=NO_CATALOG
    )
    assert status.catalog_line(nothing) == Line("No catalog yet", "warn")
    assert status.switch_line(nothing) == "Recording starts once the PC has answered"
    assert status.switch_line(given(pc_catalog_unavailable=True)).startswith("The PC has no active")


def test_the_window_shows_the_clients_status(qtbot: Any, studio_window: Window) -> None:
    studio = studio_window.model.studio
    client = studio.client
    sync = studio_window.find("studioSyncText")
    qtbot.waitUntil(lambda: sync.property("text") == studio.syncBadge, timeout=5000)
    assert studio.syncBadge == "Uploads off"
    assert studio.syncText.startswith("Uploads off until S-1 passes")
    assert studio.syncTone == "neutral"  # a plain statement, not an error

    states = [
        given(queued=2, sent=1),
        given(offline=True, queued=2, sent=1),
        given(refreshing=1),
        given(damaged=(TAKE,)),
        given(unsaved_recordings=("rec-1",)),
        given(
            notices=(
                notice(
                    "changed-elsewhere", "Changed elsewhere: this take now shows the PC's state."
                ),
            )
        ),
        given(
            notices=(notice("change-blocked", "The PC cannot read this change (invalid_request)."),)
        ),
        given(queued=2, upload_needs_approval=True, upload_blocked="no approval is bound"),
        given(
            upload_needs_approval=True,
            upload_blocked="no approval is bound",
            notices=(notice("changed-elsewhere", "Changed elsewhere: the PC's state."),),
        ),
    ]
    for value in states:
        client.status = lambda value=value, **_: value  # type: ignore[method-assign]
        studio._client_changed()
        expected = status.notice_line(value.notices, {"neutral-01": "Neutral 01"})
        expected = expected or status.sync_line(value)
        assert (studio.syncText, studio.syncTone, studio.syncDetail) == (
            expected.text,
            expected.tone,
            expected.detail,
        )
        qtbot.waitUntil(lambda e=expected: sync.property("text") == e.badge, timeout=5000)
        # One action beside the badge at most: Approve or Sync now (the badge shows the rest).
        assert studio.syncAction == expected.action
        assert buttons(studio_window) == BUTTON_FOR[expected.action]


#: The button beside the badge, by the line's action.
BUTTON_FOR: dict[str, list[str]] = {
    "approve": ["studioApprove"],
    "sync": ["studioSyncNow"],
    "": [],
}


def buttons(window: Window) -> list[str]:
    """The action shown beside the sync badge."""
    names = ("studioApprove", "studioSyncNow")
    return [name for name in names if window.find(name).property("visible")]


def test_every_sync_line_is_whole_on_the_smallest_window(qtbot: Any, studio_window: Window) -> None:
    """PROMPT-STUDIO.md: the sync badge and its action fit beside the take's line on the
    smallest window (960 × 600), and the badge shows the whole line, never cut short."""
    from .test_studio_screen import drawn, resize, scene_rect

    studio = studio_window.model.studio
    client = studio.client
    sync = studio_window.find("studioSyncText")
    line = studio_window.find("studioStatusText")
    actions = studio_window.find("studioSyncActions")
    row = studio_window.find("studioStatus")
    popup = studio_window.find("studioDetailsPopup")
    whole = studio_window.find("studioDetailsLine")
    resize(qtbot, studio_window, 960, 600)
    many = {"queued": 12, "sent": 1234}
    blocked = "no home-host approval is bound on this machine yet"
    states = [
        given(),
        given(**many, local_only=3),
        given(**many, busy=True),
        given(**many, offline=True),
        given(**many, refreshing=2),
        given(damaged=(TAKE, "take-" + "b" * 64)),
        given(unsaved_recordings=("rec-1",)),
        given(sign_in_required=True),
        given(enabled=False, offline=True, takes=1234),
        given(**many, upload_blocked="the settings name another server"),
        given(**many, upload_needs_approval=True, upload_blocked=blocked),
        given(
            notices=(
                notice(
                    "changed-elsewhere", "Changed elsewhere: this take now shows the PC's state."
                ),
            )
        ),
        given(
            notices=(
                notice("changed-elsewhere", "Changed elsewhere.", 1),
                notice("change-blocked", "The PC cannot read this change (invalid_request).", 2),
            )
        ),
        given(
            notices=(
                notice("change-refused", "The PC refused a change (trim_out_of_bounds): …", 3),
            )
        ),
        given(notices=(notice("take-refused", "The PC refused this take: refused_by_intake", 4),)),
    ]
    for value in states:
        client.status = lambda value=value, **_: value  # type: ignore[method-assign]
        studio._client_changed()
        expected = status.notice_line(value.notices, {"neutral-01": "Neutral 01"})
        expected = expected or status.sync_line(value)
        qtbot.waitUntil(lambda e=expected: sync.property("text") == e.badge, timeout=5000)
        drawn(qtbot, studio_window)
        assert buttons(studio_window) == BUTTON_FOR[expected.action]
        left, right, whole_row = scene_rect(line), scene_rect(actions), scene_rect(row)
        assert left[0] + left[2] <= right[0], (expected.badge, left, right)
        assert right[0] + right[2] <= whole_row[0] + whole_row[2], expected.badge
        # The badge opens the whole line.
        click(studio_window, "studioSyncBadge")
        qtbot.waitUntil(lambda: popup.property("visible"), timeout=5000)
        assert whole.property("text") == expected.text and not whole.property("truncated")
        popup.close()
        qtbot.waitUntil(lambda: not popup.property("visible"), timeout=5000)


def test_a_toast_never_covers_the_status_line(qtbot: Any, studio_window: Window) -> None:
    """A toast shows top right: at the bottom it covered Sync now (seen on the Air)."""
    from .test_studio_screen import drawn, resize, scene_rect

    studio = studio_window.model.studio
    resize(qtbot, studio_window, 960, 600)
    studio.client.status = lambda **_: given(queued=2, sent=2)  # type: ignore[method-assign]
    studio._client_changed()
    qtbot.waitUntil(lambda: buttons(studio_window) == ["studioSyncNow"], timeout=5000)
    stack = studio_window.find("toastStack")
    for _ in range(4):  # as many as show at once
        studio.toast.emit("Take removed", "ok")
    qtbot.waitUntil(lambda: stack.height() > 0, timeout=5000)
    drawn(qtbot, studio_window)
    x, y, width, height = scene_rect(stack)
    line = scene_rect(studio_window.find("studioStatus"))
    assert y + height <= line[1] or x + width <= line[0], (scene_rect(stack), line)
