"""Generate and verify committed JSON Schemas for durable boundaries."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pydantic import BaseModel

from .api.contracts import (
    ApiMeta,
    ApiRefusal,
    ChunkAnswer,
    IntakeSessionRequest,
    IntakeSessionStatus,
)
from .api.journey_contracts import JOURNEY_API_SCHEMA_MODELS
from .audition import (
    AuditionPlan,
    AuditionRunManifest,
    PersistedTrialResult,
    PersistedTrialSourceEvidence,
)
from .blind import (
    PublicReviewManifest,
    RevealMap,
    ReviewIndex,
    ReviewPackageManifest,
    ScoreSheet,
)
from .capture import CaptureContinuityEvidence, ReadyMarker, RejectedTakeLog
from .datasets import (
    DatasetManifest,
    DatasetSelectionDocument,
    FineTuningTransferPlan,
    HostedTransferApproval,
    PilotGateApproval,
)
from .diagnostic_roots import DiagnosticRootMarker
from .finetune import FINETUNE_SCHEMA_MODELS
from .home_host import HomeHostApproval, HomeHostBinding, HomeHostLedgerEntry
from .intake import IntakeReceipt
from .intake_publisher import IntakeAcceptanceRecord, IntakeRefusalRecord
from .library_replication_service import VAULT_SUPERVISOR_SCHEMA_MODELS
from .model_comparison import ModelComparison
from .models import (
    CandidateSpec,
    CaptureManifest,
    ConsentRecord,
    ExecutedCodeEvidence,
    ExperimentRequest,
    ExperimentResult,
    ModelLockEntry,
    PromptSuite,
    QcReport,
    WorkerModelEvidence,
)
from .native_recording_application import NATIVE_RECORDING_APPLICATION_SCHEMA_MODELS
from .recording_analysis import RECORDING_ANALYSIS_SCHEMA_MODELS
from .recording_contracts import CORPUS_SCHEMA_MODELS, RECORDING_SCHEMA_MODELS
from .recording_discovery import RecordingDiscoveryCheckpoint
from .recording_discovery_contracts import (
    DiscoveryAck,
    DiscoveryEnvelope,
    DiscoveryRequest,
    DiscoveryWorkerStart,
    LegacyProjectionGroup,
    LegacyProjectionHeader,
    LegacyProjectionRow,
    LegacyProjectionUnknown,
)
from .recording_edit_jobs import RECORDING_EDIT_JOB_SCHEMA_MODELS
from .recording_edits import RECORDING_EDIT_SCHEMA_MODELS
from .recording_finetune_jobs import RECORDING_FINETUNE_JOB_SCHEMA_MODELS
from .recording_library import DesktopSessionEvidence
from .recording_packages import RECORDING_PACKAGE_SCHEMA_MODELS
from .recording_recovery import RecordingLegacyContinuation
from .recording_takes import RECORDING_TAKE_SCHEMA_MODELS
from .references import HeadphoneReviewEvidence, ReferenceManifest
from .registry import CandidateConfiguration, ModelSourcesLock
from .runtime import LocalSourceManifest, RecordingPythonRuntimeReceipt, RuntimeInstallReceipt
from .speech.config import SpeechConfig
from .speech.contracts import SPEECH_SCHEMA_MODELS
from .speech.release import VoiceRelease
from .storage_contracts import STORAGE_SCHEMA_MODELS

# A ``**`` merge silently keeps the last value of a repeated name; tests/test_schema.py
# refuses any name this literal would register twice.
SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    **STORAGE_SCHEMA_MODELS,
    **SPEECH_SCHEMA_MODELS,
    "speech-config": SpeechConfig,
    "voice-release": VoiceRelease,
    "model-comparison": ModelComparison,
    **VAULT_SUPERVISOR_SCHEMA_MODELS,
    **NATIVE_RECORDING_APPLICATION_SCHEMA_MODELS,
    "recording-python-runtime-receipt": RecordingPythonRuntimeReceipt,
    **RECORDING_SCHEMA_MODELS,
    **CORPUS_SCHEMA_MODELS,
    **RECORDING_ANALYSIS_SCHEMA_MODELS,
    **RECORDING_TAKE_SCHEMA_MODELS,
    **RECORDING_PACKAGE_SCHEMA_MODELS,
    **RECORDING_EDIT_SCHEMA_MODELS,
    **RECORDING_EDIT_JOB_SCHEMA_MODELS,
    **RECORDING_FINETUNE_JOB_SCHEMA_MODELS,
    "recording-discovery-checkpoint": RecordingDiscoveryCheckpoint,
    "recording-discovery-request": DiscoveryRequest,
    "recording-discovery-worker-start": DiscoveryWorkerStart,
    "recording-discovery-envelope": DiscoveryEnvelope,
    "recording-discovery-ack": DiscoveryAck,
    "recording-discovery-legacy-row": LegacyProjectionRow,
    "recording-discovery-legacy-header": LegacyProjectionHeader,
    "recording-discovery-legacy-group": LegacyProjectionGroup,
    "recording-discovery-legacy-unknown": LegacyProjectionUnknown,
    "recording-legacy-continuation": RecordingLegacyContinuation,
    "api-v1-chunk-answer": ChunkAnswer,
    "api-v1-intake-session": IntakeSessionRequest,
    "api-v1-intake-session-status": IntakeSessionStatus,
    "api-v1-meta": ApiMeta,
    "api-v1-refusal": ApiRefusal,
    "home-host-approval": HomeHostApproval,
    "home-host-binding": HomeHostBinding,
    "home-host-ledger-entry": HomeHostLedgerEntry,
    "intake-acceptance": IntakeAcceptanceRecord,
    "intake-refusal": IntakeRefusalRecord,
    "audition-plan": AuditionPlan,
    "audition-run": AuditionRunManifest,
    "audition-trial-result": PersistedTrialResult,
    "audition-trial-source-evidence": PersistedTrialSourceEvidence,
    "blind-review-index": ReviewIndex,
    "blind-review-package": ReviewPackageManifest,
    "blind-review-public-package": PublicReviewManifest,
    "blind-reveal-map": RevealMap,
    "blind-score-sheet": ScoreSheet,
    "candidate-configuration": CandidateConfiguration,
    "candidate-spec": CandidateSpec,
    "capture-manifest": CaptureManifest,
    "capture-continuity": CaptureContinuityEvidence,
    "capture-ready": ReadyMarker,
    "capture-rejected-takes": RejectedTakeLog,
    "capture-desktop-session": DesktopSessionEvidence,
    "consent": ConsentRecord,
    "dataset-manifest": DatasetManifest,
    "dataset-selection": DatasetSelectionDocument,
    "diagnostic-root": DiagnosticRootMarker,
    "experiment-request": ExperimentRequest,
    "experiment-result": ExperimentResult,
    "executed-code-evidence": ExecutedCodeEvidence,
    **FINETUNE_SCHEMA_MODELS,
    **JOURNEY_API_SCHEMA_MODELS,
    "fine-tuning-transfer-plan": FineTuningTransferPlan,
    "headphone-review": HeadphoneReviewEvidence,
    "hosted-transfer-approval": HostedTransferApproval,
    "intake-receipt": IntakeReceipt,
    "local-source-manifest": LocalSourceManifest,
    "model-lock-entry": ModelLockEntry,
    "model-sources-lock": ModelSourcesLock,
    "pilot-gate": PilotGateApproval,
    "prompt-suite": PromptSuite,
    "qc-report": QcReport,
    "reference-manifest": ReferenceManifest,
    "runtime-install-receipt": RuntimeInstallReceipt,
    "worker-model-evidence": WorkerModelEvidence,
}


def schema_bytes(model: type[BaseModel]) -> bytes:
    return (json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n").encode()


def generate_schemas(output_directory: Path, *, check: bool) -> list[str]:
    if not check:
        output_directory.mkdir(parents=True, exist_ok=True)
    drift: list[str] = []
    expected_names = {f"{name}.schema.json" for name in SCHEMA_MODELS}
    if check and output_directory.is_symlink():
        drift.append(str(output_directory))
    if check and output_directory.is_dir() and not output_directory.is_symlink():
        actual_names = {path.name for path in output_directory.iterdir()}
        for name in sorted(actual_names - expected_names):
            drift.append(str(output_directory / name))
    for name, model in sorted(SCHEMA_MODELS.items()):
        path = output_directory / f"{name}.schema.json"
        expected = schema_bytes(model)
        if check:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != expected:
                drift.append(str(path))
        else:
            path.write_bytes(expected)
    return sorted(set(drift))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("schemas"))
    options = parser.parse_args(argv)
    drift = generate_schemas(options.output, check=options.check)
    if drift:
        for path in drift:
            print(f"schema drift: {path}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
