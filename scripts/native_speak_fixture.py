"""The native ``speak`` suite: the actual app, helper, service and Speak worker, stub runtime.

``prepare_speak_fixture`` builds on ``native_finetune_fixture.prepare_finetune_fixture``: the
same corpus-store catalog plus a second reference-candidate prompt, the stub F5 runtime, the
test-only service wrapper (which also shortens the Speak deadline to
``SPEECH_TEST_DEADLINE_SECONDS``), and two finished runs written with
``tests/finetune_speech_runs.py`` (no training needed). ``run_speak_suite`` runs the
``speak`` harness against it and hands the root to ``verify_native_speak.verify``.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from native_finetune_fixture import (
    READING_PROMPT_IDS,
    REFERENCE_PROMPT_ID,
    TRAINING_STEP_SECONDS,
    NativeFineTuneFixture,
    finetune_catalog,
    prepare_finetune_fixture,
)
from verify_native_workspace import SUBJECT

from cole_voice_lab.models import Prompt, PromptSuite

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "tests"))

from finetune_speech_runs import write_speech_run  # noqa: E402

SECOND_REFERENCE_PROMPT_ID = "finetune-reference-two"
LATEST_RUN_ID = "finetune-speak-latest"
EARLIER_RUN_ID = "finetune-speak-earlier"
# The newest run records both reference prompts; the earlier one only the second.
LATEST_RUN_CLIPS = (REFERENCE_PROMPT_ID, SECOND_REFERENCE_PROMPT_ID, READING_PROMPT_IDS[0])
EARLIER_RUN_CLIPS = (SECOND_REFERENCE_PROMPT_ID, READING_PROMPT_IDS[0])
RUN_ENTRIES = (REFERENCE_PROMPT_ID, SECOND_REFERENCE_PROMPT_ID, READING_PROMPT_IDS[0])
EARLIER_FINISHED_AT = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
LATEST_FINISHED_AT = datetime(2026, 9, 2, 10, 0, tzinfo=UTC)
# Short enough that the suite shows a deadline failure in seconds, long enough that an
# ordinary stub request (worker start, runtime probe, synthesis) always finishes first.
SPEECH_TEST_DEADLINE_SECONDS = 10.0
# A request meant to be cancelled or to run past the deadline sleeps this long.
SLOW_SYNTHESIS_SECONDS = 60.0
SPEAK_HARNESS_SECONDS = 300


def speak_catalog() -> PromptSuite:
    """The fine-tune catalog with a second reference candidate after the first."""

    base = finetune_catalog()
    second = Prompt(
        prompt_id=SECOND_REFERENCE_PROMPT_ID,
        text="A second synthetic reference reading.",
        category="neutral",
        style="Speak naturally",
        expected_seconds=1.0,
        reference_candidate=True,
    )
    return PromptSuite(
        schema_version=base.schema_version,
        suite_id="synthetic-speak",
        purpose=base.purpose,
        prompts=(base.prompts[0], second, *base.prompts[1:]),
    )


@dataclass(frozen=True)
class NativeSpeakFixture:
    """A prepared root: the fine-tune fixture plus the two finished runs written into it."""

    fixture: NativeFineTuneFixture
    latest_run_id: str
    earlier_run_id: str


def prepare_speak_fixture(
    root: Path, *, package: Path, binary_directory: Path
) -> NativeSpeakFixture:
    """Create the ``speak`` fixture root at ``root`` (new or empty; made private).

    Returns: the prepared fixture.
    Raises: ``ValueError`` as ``prepare_finetune_fixture``.
    """

    catalog = speak_catalog()
    fixture = prepare_finetune_fixture(
        root,
        package=package,
        binary_directory=binary_directory,
        catalog=catalog,
        runtime_mode={"step_seconds": TRAINING_STEP_SECONDS},
        synthesis_deadline=SPEECH_TEST_DEADLINE_SECONDS,
    )
    for run_id, clips, finished_at in (
        (EARLIER_RUN_ID, EARLIER_RUN_CLIPS, EARLIER_FINISHED_AT),
        (LATEST_RUN_ID, LATEST_RUN_CLIPS, LATEST_FINISHED_AT),
    ):
        write_speech_run(
            fixture.layout,
            run_id,
            catalog=catalog,
            subject_id=SUBJECT,
            started_at=finished_at.replace(hour=8),
            finished_at=finished_at,
            entries=RUN_ENTRIES,
            clips=clips,
        )
    return NativeSpeakFixture(
        fixture=fixture, latest_run_id=LATEST_RUN_ID, earlier_run_id=EARLIER_RUN_ID
    )


def run_speak_suite(root: Path, *, package: Path, binary_directory: Path) -> None:
    """The native ``speak`` suite (see ``RecordingSpeakHarness.swift``).

    Evidence: ``root/native/speak-report.json`` (what the app did and showed), the Save As
    copy under ``root/native/save-as/`` and ``root/python-speak-evidence.json``.
    Raises: ``subprocess.CalledProcessError`` or ``subprocess.TimeoutExpired`` when the
    harness fails; ``AssertionError`` when the durable evidence disagrees.
    """

    prepared = prepare_speak_fixture(
        root / "speak", package=package, binary_directory=binary_directory
    )
    fixture = prepared.fixture
    subprocess.run(
        [
            str(fixture.harness),
            "speak",
            str(fixture.configuration),
            str(fixture.runtime_root),
            str(root / "native"),
        ],
        check=True,
        timeout=SPEAK_HARNESS_SECONDS,
    )
    from verify_native_speak import verify

    verify(root, prepared)
