"""The derivative render reserve: queued trim work never consumes capture's space.

Every case injects the free-space probe rather than constraining a real volume, which
is how intake already takes its budget. A refusal must stay transient: no durable
failure evidence, no lost take, and one named diagnostic per low-space episode.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from test_recording_native_edits import edit
from test_recording_navigation import NavigationRig
from test_recording_navigation import nav as nav

from cole_voice_lab import recording_edit_jobs as jobs
from cole_voice_lab.canonical import canonical_sha256
from cole_voice_lab.recording_edit_jobs import (
    DERIVATIVE_RENDER_RESERVE_BYTES,
    RecordingEditCoordinator,
)
from cole_voice_lab.recording_edits import (
    current_native_edit,
    native_edit_derivative,
    native_edit_directory,
)

GENEROUS = 1 << 60


def derivative_bytes(nav: NavigationRig, take_id: str) -> int:
    recipe = current_native_edit(nav.rig.layout, take_id)
    assert recipe is not None
    span = recipe.frame_range
    return 44 + 3 * (span.end_frame - span.start_frame)


def failure_evidence(nav: NavigationRig, take_id: str):
    recipe = current_native_edit(nav.rig.layout, take_id)
    assert recipe is not None
    return (
        native_edit_directory(nav.rig.layout, take_id) / f"{canonical_sha256(recipe)}.failure.json"
    )


def run_one(nav: NavigationRig, take_id: str, available: int, seconds: float = 10.0):
    """Submit one take against a fixed free-space answer and report what happened."""

    events: list[Any] = []
    failures: list[Any] = []
    done = threading.Event()

    def receive(event: Any) -> None:
        events.append(event)
        done.set()

    def fail(error: Any) -> None:
        failures.append(error)
        done.set()

    coordinator = RecordingEditCoordinator(
        nav.rig.store, published=receive, failed=fail, available_bytes=lambda: available
    )
    try:
        coordinator.submit(take_id)
        done.wait(seconds)
        return events, failures
    finally:
        coordinator.close()


def test_a_render_that_fits_above_the_reserve_still_runs(nav: NavigationRig) -> None:
    take = nav.capture()
    edit(nav, take)
    take_id = take.reservation.take_id
    exact = derivative_bytes(nav, take_id) + DERIVATIVE_RENDER_RESERVE_BYTES
    events, failures = run_one(nav, take_id, exact)
    assert not failures, failures
    assert events and events[0].state == "ready"
    assert native_edit_derivative(nav.rig.layout, take_id) is not None


def test_one_byte_below_the_reserve_defers_without_burning_the_recipe(
    nav: NavigationRig,
) -> None:
    take = nav.capture()
    edit(nav, take)
    take_id = take.reservation.take_id
    source = nav.rig.layout.root / take.seal.original_path
    original = source.read_bytes()
    exact = derivative_bytes(nav, take_id) + DERIVATIVE_RENDER_RESERVE_BYTES
    events, failures = run_one(nav, take_id, exact - 1, seconds=5.0)

    assert not events, "a refused render must not publish a derivative outcome"
    assert len(failures) == 1
    assert failures[0].code == "RECORDING.DISK_SPACE"
    assert failures[0].retryable is True
    assert f"free_bytes={exact - 1}" in failures[0].details
    assert f"required_bytes={exact}" in failures[0].details
    # Nothing durable was written, so the same trim can render once space returns.
    assert native_edit_derivative(nav.rig.layout, take_id) is None
    assert not failure_evidence(nav, take_id).exists()
    assert current_native_edit(nav.rig.layout, take_id) is not None
    assert source.read_bytes() == original


def test_queued_work_is_counted_so_a_backlog_refuses_what_one_take_would_not(
    nav: NavigationRig,
) -> None:
    """The pending term is load-bearing: identical space, different queue depth."""

    first = nav.capture()
    edit(nav, first)
    first_id = first.reservation.take_id
    single = derivative_bytes(nav, first_id) + DERIVATIVE_RENDER_RESERVE_BYTES

    second = nav.capture("second")
    edit(nav, second, "edit-2")
    second_id = second.reservation.take_id
    assert derivative_bytes(nav, second_id) > 0

    events: list[Any] = []
    failures: list[Any] = []
    settled = threading.Event()

    def fail(error: Any) -> None:
        failures.append(error)
        settled.set()

    coordinator = RecordingEditCoordinator(
        nav.rig.store,
        published=events.append,
        failed=fail,
        available_bytes=lambda: single,
    )
    try:
        coordinator.submit(first_id)
        coordinator.submit(second_id)
        settled.wait(5.0)
    finally:
        coordinator.close()
    # One take alone fits in exactly this much space; two do not.
    assert failures and failures[0].code == "RECORDING.DISK_SPACE"
    assert "queued_renders=2" in failures[0].details


def test_a_low_space_episode_is_announced_once_not_per_take(nav: NavigationRig) -> None:
    takes = []
    for ordinal, operation in enumerate(("capture", "second"), start=1):
        take = nav.capture(operation)
        edit(nav, take, f"edit-{ordinal}")
        takes.append(take)
    failures: list[Any] = []
    settled = threading.Event()

    def fail(error: Any) -> None:
        failures.append(error)
        settled.set()

    coordinator = RecordingEditCoordinator(
        nav.rig.store, published=lambda _: None, failed=fail, available_bytes=lambda: 0
    )
    try:
        for take in takes:
            coordinator.submit(take.reservation.take_id)
        settled.wait(2.0)
        time.sleep(0.3)
    finally:
        coordinator.close()
    # An unbounded diagnostic would overrun the reply budget and kill the supervisor.
    assert len(failures) == 1, failures


def test_a_refused_take_is_rediscovered_once_space_returns(
    nav: NavigationRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without re-armed discovery a deferred take would be dropped for the process life."""

    monkeypatch.setattr(jobs, "LOW_SPACE_RETRY_SECONDS", 0.0)
    take = nav.capture()
    edit(nav, take)
    take_id = take.reservation.take_id
    exact = derivative_bytes(nav, take_id) + DERIVATIVE_RENDER_RESERVE_BYTES
    answers = [exact - 1, exact - 1]
    ready = threading.Event()

    def available() -> int:
        return answers.pop(0) if answers else GENEROUS

    coordinator = RecordingEditCoordinator(
        nav.rig.store,
        published=lambda _: ready.set(),
        failed=lambda _: None,
        available_bytes=available,
    )
    try:
        coordinator.submit(take_id)
        assert ready.wait(15), "a deferred take was never retried after space returned"
    finally:
        coordinator.close()
    assert native_edit_derivative(nav.rig.layout, take_id) is not None


def test_an_unreadable_free_space_probe_refuses_rather_than_rendering_blind(
    nav: NavigationRig,
) -> None:
    take = nav.capture()
    edit(nav, take)
    take_id = take.reservation.take_id

    def unavailable() -> int:
        raise OSError("synthetic statvfs failure")

    failures: list[Any] = []
    settled = threading.Event()

    def fail(error: Any) -> None:
        failures.append(error)
        settled.set()

    coordinator = RecordingEditCoordinator(
        nav.rig.store,
        published=lambda _: None,
        failed=fail,
        available_bytes=unavailable,
    )
    try:
        coordinator.submit(take_id)
        settled.wait(5.0)
    finally:
        coordinator.close()
    assert failures and failures[0].code == "RECORDING.DISK_SPACE"
    # A probe failure is not the recipe's fault and must not burn it.
    assert not failure_evidence(nav, take_id).exists()
    assert native_edit_derivative(nav.rig.layout, take_id) is None


def test_the_reserve_subsumes_the_native_capture_requirement() -> None:
    """A permitted render can never drop free space below what Record itself needs."""

    from cole_voice_lab.recording_contracts import MAX_RECORDING_FRAMES

    assert DERIVATIVE_RENDER_RESERVE_BYTES >= MAX_RECORDING_FRAMES * 3 + 256 * 1024 * 1024
