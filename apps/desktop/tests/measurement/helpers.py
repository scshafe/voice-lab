"""Recording a whole simulated session quickly (the simulated UMIK-1 runs faster than real time)."""

from __future__ import annotations

import time

from voice_lab_desktop.measurement.ritual import Ritual
from voice_lab_desktop.measurement.simulated import UMIK


def wait_for(predicate, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.005)


def record_segment(ritual: Ritual, manual_seconds: float = 1.3) -> dict:
    segment = ritual.current
    recording = ritual.start_segment()
    if segment.mode == "timed":
        wait_for(recording.finished.is_set)
    else:
        wait_for(lambda: recording.seconds >= manual_seconds)
    return ritual.finish_segment()


def new_ritual(backend, levels, state, sessions, **options) -> Ritual:
    return Ritual(backend, UMIK, levels, state, sessions, options.pop("distance_cm", 35), **options)


def record_session(backend, levels, state, sessions, **options) -> Ritual:
    ritual = new_ritual(backend, levels, state, sessions, **options)
    ritual.begin()
    while ritual.current is not None:
        record_segment(ritual)
    ritual.complete()
    return ritual
