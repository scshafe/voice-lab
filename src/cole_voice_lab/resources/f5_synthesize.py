"""Synthesize listening samples with an F5-TTS checkpoint (run by the F5 runtime's python).

canonical: finetune synthesizer -- the only way the lab turns text into audio with a
fine-tuned or base F5-TTS checkpoint. Input is one JSON job file:

  {"checkpoint_path": "...", "vocab_path": "...", "vocoder_path": "...", "use_ema": true,
   "device": "mps"|"cpu", "nfe_steps": 32, "reference_audio_path": "...",
   "reference_text": "...", "output_directory": "...", "work_directory": "...",
   "file_prefix": "sample", "texts": ["...", ...]}

``vocoder_path`` is the runtime's pinned Vocos directory (``config.yaml`` and
``pytorch_model.bin``); it is loaded locally, never from Hugging Face. ``file_prefix``
(default ``sample``; Speak uses ``speech``) names the clips.

``work_directory`` must be a direct child of ``output_directory``. It is created private
(0700), replacing one an interrupted earlier run left there, and becomes the temporary
directory (``tempfile.tempdir`` and ``TMPDIR``) before F5-TTS is imported: F5-TTS 1.1.22's
``preprocess_ref_audio_text`` writes a converted copy of the reference clip to the temporary
directory and never removes it. The work directory is removed when synthesis ends, whether
it succeeded or failed.

Writes ``<file_prefix>-NN.wav`` (24 kHz mono 16-bit PCM) into ``output_directory`` and
prints one JSON line per clip: {"index": N, "path": "...", "seconds": S}. The caller checks
each clip's header and that the work directory is gone. Idempotent for the same job.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
from importlib.resources import files
from pathlib import Path
from typing import Any

os.environ.setdefault("WANDB_MODE", "disabled")
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")


def _private_work_directory(output_directory: Path, work_directory: Path) -> None:
    """Create ``work_directory`` (0700) inside ``output_directory``.

    Raises: ``ValueError`` when it is not a direct child of ``output_directory``;
    ``OSError`` when an earlier one cannot be removed (a link is refused) or the directory
    cannot be created.
    """
    if work_directory.parent != output_directory or work_directory.name in {"", ".", ".."}:
        raise ValueError(f"work_directory must lie directly in {output_directory}")
    if os.path.lexists(work_directory):
        if not stat.S_ISDIR(os.lstat(work_directory).st_mode):
            raise OSError(f"{work_directory} exists and is not a directory")
        shutil.rmtree(work_directory)
    work_directory.mkdir(mode=0o700)
    os.chmod(work_directory, 0o700)


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        print("usage: f5_synthesize.py <job.json>", file=sys.stderr)
        return 2
    job: dict[str, Any] = json.loads(Path(arguments[0]).read_text(encoding="utf-8"))
    output_directory = Path(job["output_directory"])
    output_directory.mkdir(parents=True, exist_ok=True)
    work_directory = Path(job["work_directory"])
    _private_work_directory(output_directory, work_directory)
    previous_tempdir, previous_tmpdir = tempfile.tempdir, os.environ.get("TMPDIR")
    tempfile.tempdir = str(work_directory)
    os.environ["TMPDIR"] = str(work_directory)
    try:
        return _synthesize(job, output_directory)
    finally:
        tempfile.tempdir = previous_tempdir
        if previous_tmpdir is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = previous_tmpdir
        shutil.rmtree(work_directory)


def _synthesize(job: dict[str, Any], output_directory: Path) -> int:
    import soundfile as sf  # type: ignore[import-not-found]
    from f5_tts.infer.utils_infer import (  # type: ignore[import-not-found]
        infer_process,
        load_model,
        load_vocoder,
        preprocess_ref_audio_text,
    )
    from f5_tts.model import DiT  # type: ignore[import-not-found]
    from omegaconf import OmegaConf  # type: ignore[import-not-found]

    config = OmegaConf.load(str(files("f5_tts").joinpath("configs/F5TTS_v1_Base.yaml")))
    device = job["device"]
    vocoder = load_vocoder(
        vocoder_name="vocos", is_local=True, local_path=job["vocoder_path"], device=device
    )
    model = load_model(
        DiT,
        config.model.arch,
        job["checkpoint_path"],
        mel_spec_type="vocos",
        vocab_file=job["vocab_path"],
        use_ema=bool(job["use_ema"]),
        device=device,
    )
    prefix = str(job.get("file_prefix", "sample"))
    reference_audio, reference_text = preprocess_ref_audio_text(
        job["reference_audio_path"], job["reference_text"], show_info=lambda *_: None
    )
    for index, text in enumerate(job["texts"], start=1):
        wave, sample_rate, _ = infer_process(
            reference_audio,
            reference_text,
            text,
            model,
            vocoder,
            mel_spec_type="vocos",
            nfe_step=int(job["nfe_steps"]),
            device=device,
            show_info=lambda *_: None,
        )
        path = output_directory / f"{prefix}-{index:02d}.wav"
        sf.write(str(path), wave, sample_rate, subtype="PCM_16")
        print(
            json.dumps({"index": index, "path": str(path), "seconds": len(wave) / sample_rate}),
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
