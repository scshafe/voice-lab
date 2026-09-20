"""Private fixture roots in which the actual native app fine-tunes against a stub F5 runtime.

``prepare_finetune_fixture`` builds one root: an asset root whose corpus store serves a small
spoken catalog (one prompt is a reference candidate), a stub runtime
(``tests/finetune_stub_runtime.py``), the recording schemas, build identities and an
application configuration whose service is started with ``--finetune-runtime-root``. The
native ``finetune`` suite uses it here; the ``speak`` suite reuses it. The shipped service never
overrides the pretrained-file pins, so a configured root's service starts through the
test-only wrapper ``tests/native_finetune_service_fixture.py``, copied into the root's isolated
runtime, which hands the stub's pins to the fine-tune supervisor. A root prepared with
``runtime_configured=False`` runs the ordinary service with a runtime directory that has no
``bin/python``.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from verify_native_workspace import (
    SERVICE_MODULE_ARGUMENTS,
    SUBJECT,
    application_configuration,
    copied_service_arguments,
    fixture_environment,
    isolated_service_runtime,
    native_builds,
    private_fixture_root,
    seed_active_catalog,
    verified_pcm,
    write_recording_schemas,
)

from cole_voice_lab.canonical import write_canonical_json
from cole_voice_lab.finetune import FineTunePretrainedPins
from cole_voice_lab.models import PROMPT_SUITE_V2, Prompt, PromptSuite
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_contracts import CatalogReference

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "tests"))

from finetune_stub_runtime import stub_finetune_runtime, write_stub_mode  # noqa: E402
from native_finetune_service_fixture import format_pins  # noqa: E402

REFERENCE_PROMPT_ID = "finetune-reference"
READING_PROMPT_IDS = ("finetune-reading-one", "finetune-reading-two", "finetune-reading-three")
SERVICE_WRAPPER = REPOSITORY / "tests/native_finetune_service_fixture.py"
SERVICE_WRAPPER_MODULE = "_synthetic_finetune_fixture"
# 120 default passes of two status lines each: about 12 s of training, so at least one
# rate-limited (5 s) progress event lands inside training.
TRAINING_STEP_SECONDS = 0.05
# A job meant to be cancelled trains for minutes.
SLOW_TRAINING_STEP_SECONDS = 1.0
FINETUNE_HARNESS_SECONDS = 300
UNAVAILABLE_HARNESS_SECONDS = 90


def finetune_catalog() -> PromptSuite:
    """Spoken prompts only (the corpus has no room tone); the first is a reference candidate."""

    return PromptSuite(
        schema_version=PROMPT_SUITE_V2,
        suite_id="synthetic-finetune",
        purpose="capture-pilot",
        prompts=(
            Prompt(
                prompt_id=REFERENCE_PROMPT_ID,
                text="Synthetic reference reading for the fine-tuned voice.",
                category="neutral",
                style="Speak naturally",
                expected_seconds=1.0,
                reference_candidate=True,
            ),
            *(
                Prompt(
                    prompt_id=prompt_id,
                    text=f"Synthetic fine-tune reading {prompt_id.rsplit('-', 1)[1]}.",
                    category="neutral",
                    style="Speak naturally",
                    expected_seconds=1.0,
                )
                for prompt_id in READING_PROMPT_IDS
            ),
        ),
    )


@dataclass(frozen=True)
class NativeFineTuneFixture:
    """One prepared root.

    ``configuration`` is the recording-application JSON the harness loads; ``runtime_root``
    is the directory the service is given as ``--finetune-runtime-root`` (a stub runtime when
    ``runtime_configured``, otherwise an empty directory); ``pretrained_pin`` is the stub's
    pins (``None`` when not configured); ``harness`` is the built ``RecordingHarness``.
    """

    root: Path
    layout: AssetLayout
    configuration: Path
    runtime_root: Path
    runtime_configured: bool
    pretrained_pin: FineTunePretrainedPins | None
    catalog: PromptSuite
    reference: CatalogReference
    harness: Path

    def set_training_step_seconds(self, seconds: float) -> None:
        """Set how long each stub training status line takes, for the runtime's next job."""

        if not self.runtime_configured:
            raise ValueError("an unconfigured fixture has no stub runtime")
        write_stub_mode(self.runtime_root, {"step_seconds": seconds})


def prepare_finetune_fixture(
    root: Path,
    *,
    package: Path,
    binary_directory: Path,
    catalog: PromptSuite | None = None,
    runtime_mode: Mapping[str, Any] | None = None,
    runtime_configured: bool = True,
    synthesis_deadline: float | None = None,
) -> NativeFineTuneFixture:
    """Create a fine-tune fixture root at ``root`` (new or empty; made private).

    Params: ``package`` and ``binary_directory`` name the Swift package and its built
    products; ``catalog`` defaults to ``finetune_catalog()`` and is published and activated
    for the synthetic subject; ``runtime_mode`` is the stub's initial ``stub-mode.json``
    (default: ``TRAINING_STEP_SECONDS`` per status line); ``runtime_configured=False`` gives
    the service a runtime directory without ``bin/python``; ``synthesis_deadline`` shortens
    the Speak deadline through the test-only wrapper (configured roots only).
    Returns: the prepared fixture.
    Raises: ``ValueError`` as ``private_fixture_root`` and ``seed_active_catalog``, and for a
    ``synthesis_deadline`` without a configured runtime.
    """

    if synthesis_deadline is not None and not runtime_configured:
        raise ValueError("only the test-only service wrapper can shorten the Speak deadline")
    root = private_fixture_root(root, package=package)
    catalog = finetune_catalog() if catalog is None else catalog
    layout = AssetLayout.initialize(root / "assets")
    reference = seed_active_catalog(layout, catalog)
    write_canonical_json(root / "catalog-reference.json", reference)
    schemas = write_recording_schemas(root)
    builds = native_builds(package, binary_directory)
    environment = fixture_environment()
    runtime_root = root / "f5-runtime"
    pin: FineTunePretrainedPins | None = None
    if runtime_configured:
        mode = {"step_seconds": TRAINING_STEP_SECONDS} if runtime_mode is None else runtime_mode
        _, pin = stub_finetune_runtime(runtime_root, dict(mode))
        service_build = isolated_service_runtime(
            root, SERVICE_WRAPPER, SERVICE_WRAPPER_MODULE, environment
        )
        service_arguments = [
            *copied_service_arguments(root, SERVICE_WRAPPER_MODULE),
            "--test-pretrained-pins",
            format_pins(pin),
        ]
        if synthesis_deadline is not None:
            service_arguments += ["--test-synthesis-deadline", f"{synthesis_deadline:g}"]
    else:
        runtime_root.mkdir(mode=0o700)
        from cole_voice_lab.recording_service import recording_service_build_identity

        service_build = recording_service_build_identity()
        service_arguments = list(SERVICE_MODULE_ARGUMENTS)
    service_arguments += [
        "--native-workspace",
        "--subject-id",
        SUBJECT,
        "--finetune-runtime-root",
        str(runtime_root),
    ]
    pcm = root / "pattern.pcm"
    # Fine-tuning trains only on retained takes; the sine passes independent verification.
    pcm.write_bytes(verified_pcm())
    configuration = application_configuration(
        root,
        layout,
        schemas,
        purpose=catalog.purpose,
        builds=builds,
        helper_arguments=[
            "--synthetic-pcm",
            str(pcm),
            "--synthetic-chunk-frames",
            "480",
            "--synthetic-total-frames",
            "4801",
        ],
        service_build=service_build,
        service_arguments=service_arguments,
        environment=environment,
    )
    configuration_path = root / "recording-application.json"
    write_canonical_json(configuration_path, configuration)
    return NativeFineTuneFixture(
        root=root,
        layout=layout,
        configuration=configuration_path,
        runtime_root=runtime_root,
        runtime_configured=runtime_configured,
        pretrained_pin=pin,
        catalog=catalog,
        reference=reference,
        harness=builds.harness,
    )


def run_finetune_suite(root: Path, *, package: Path, binary_directory: Path) -> None:
    """The native ``finetune`` suite: the real app, helper, service and worker, twice.

    ``root/trained``: record two takes, fine-tune on them to a finished run, then start a
    second job, record and save a take while it trains, and cancel it (the harness switches
    the stub to ``SLOW_TRAINING_STEP_SECONDS`` itself between the jobs). ``root/unavailable``:
    a runtime without ``bin/python`` leaves Fine-tune model unavailable with its reason.
    Evidence: ``root/native/finetune-report.json``, ``root/native-unavailable/
    finetune-unavailable-report.json`` and ``root/python-finetune-evidence.json``.
    Raises: ``subprocess.CalledProcessError`` or ``subprocess.TimeoutExpired`` when a
    harness run fails; ``AssertionError`` when the durable evidence disagrees.
    """

    trained = prepare_finetune_fixture(
        root / "trained", package=package, binary_directory=binary_directory
    )
    subprocess.run(
        [
            str(trained.harness),
            "finetune",
            str(trained.configuration),
            str(trained.runtime_root),
            str(root / "native"),
        ],
        check=True,
        timeout=FINETUNE_HARNESS_SECONDS,
    )
    unavailable = prepare_finetune_fixture(
        root / "unavailable",
        package=package,
        binary_directory=binary_directory,
        runtime_configured=False,
    )
    subprocess.run(
        [
            str(unavailable.harness),
            "finetune-unavailable",
            str(unavailable.configuration),
            str(root / "native-unavailable"),
        ],
        check=True,
        timeout=UNAVAILABLE_HARNESS_SECONDS,
    )
    from verify_native_finetune import verify

    verify(root, trained=trained, unavailable=unavailable)
