"""Generate and verify committed JSON Schemas for durable boundaries."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pydantic import BaseModel

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
from .capture import ReadyMarker
from .datasets import (
    DatasetManifest,
    DatasetSelectionDocument,
    FineTuningTransferPlan,
    HostedTransferApproval,
    PilotGateApproval,
)
from .intake import IntakeReceipt
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
from .promotion import (
    ChloeSpeechHandoff,
    HumanReferenceProvenanceRecord,
    LabVerdictRecord,
    PromotionListeningSummaryRecord,
    PromotionManifest,
    WinningRunRecord,
)
from .references import HeadphoneReviewEvidence, ReferenceManifest
from .registry import CandidateConfiguration, ModelSourcesLock
from .runtime import LocalSourceManifest, RuntimeInstallReceipt

SCHEMA_MODELS: dict[str, type[BaseModel]] = {
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
    "capture-ready": ReadyMarker,
    "chloe-speech-handoff": ChloeSpeechHandoff,
    "consent": ConsentRecord,
    "dataset-manifest": DatasetManifest,
    "dataset-selection": DatasetSelectionDocument,
    "experiment-request": ExperimentRequest,
    "experiment-result": ExperimentResult,
    "executed-code-evidence": ExecutedCodeEvidence,
    "fine-tuning-transfer-plan": FineTuningTransferPlan,
    "headphone-review": HeadphoneReviewEvidence,
    "hosted-transfer-approval": HostedTransferApproval,
    "human-reference-provenance": HumanReferenceProvenanceRecord,
    "intake-receipt": IntakeReceipt,
    "lab-verdict": LabVerdictRecord,
    "local-source-manifest": LocalSourceManifest,
    "model-lock-entry": ModelLockEntry,
    "model-sources-lock": ModelSourcesLock,
    "pilot-gate": PilotGateApproval,
    "promotion-manifest": PromotionManifest,
    "promotion-listening-summary": PromotionListeningSummaryRecord,
    "prompt-suite": PromptSuite,
    "qc-report": QcReport,
    "reference-manifest": ReferenceManifest,
    "runtime-install-receipt": RuntimeInstallReceipt,
    "winning-run": WinningRunRecord,
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
