"""The resident F5-TTS speech worker (run by the F5 runtime's python; D-046).

canonical: speech resident worker -- keeps one model, vocoder and voice release loaded and
renders one segment at a time, for ``cole_voice_lab.speech.resident``. The protocol is JSON
lines: on stdin, first the load request, then render requests; on stdout, only the answers.
Anything F5-TTS prints goes to stderr (fd 1 is pointed at fd 2 once the protocol's own copy
of stdout is kept), so it can never corrupt the protocol.

Load (first line)::

  {"checkpoint_path": "...", "vocab_path": "...", "vocoder_path": "...", "use_ema": true,
   "device": "cuda"|"cpu", "reference_audio_path": "...", "reference_text": "...",
   "work_directory": "..."}

answered ``{"event": "ready", "f5_tts_version": "...", "torch_version": "...",
"model_dtype": "..."}`` or ``{"event": "failed", "error": "..."}``. ``vocoder_path`` is the
runtime's pinned Vocos directory, loaded locally, never from Hugging Face.
``work_directory`` is created private (0700, an earlier one removed) and becomes the
temporary directory before F5-TTS is imported: ``preprocess_ref_audio_text`` writes a
converted copy of the reference clip there. It is removed when the worker exits.

Render::

  {"op": "render", "id": N, "text": "...", "seed": S, "nfe_steps": 32, "output_path": "..."}

answered ``{"event": "rendered", "id": N, "frames": F}`` (the WAV, 24 kHz mono 16-bit, 0600,
at ``output_path``; on a GPU also ``"peak_memory_bytes"``, the most memory torch has
allocated) or ``{"event": "failed", "id": N, "error": "..."}``. A failed segment's error is
the exception's class and the innermost frames that raised it (``_failure``), never its
message, which may quote the text. ``{"op": "quit"}``
ends the worker. Each segment is seeded just before it renders and is rendered in one pass
through ``infer_batch_process``'s sequential streaming path, never through ``infer_process``,
whose thread pool shares the global RNG between chunks (spike S-2: not reproducible, and
peak VRAM rises from 1.6 to 7.3 GiB).
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import traceback
from importlib.resources import files
from pathlib import Path
from typing import IO, Any

os.environ.setdefault("WANDB_MODE", "disabled")
os.environ.setdefault("HF_HUB_OFFLINE", "1")

# How many of the innermost frames a failed segment names.
FAILURE_FRAMES = 4


def _private_work_directory(work_directory: Path) -> None:
    if os.path.lexists(work_directory):
        if not stat.S_ISDIR(os.lstat(work_directory).st_mode):
            raise OSError(f"{work_directory} exists and is not a directory")
        shutil.rmtree(work_directory)
    work_directory.mkdir(mode=0o700)
    os.chmod(work_directory, 0o700)


def _failure(error: BaseException) -> str:
    """A failed segment's report: the exception's class and where it was raised, innermost
    first (file name, line, function), so the job runner's log can say what broke. Never
    its message: an error inside F5-TTS can quote the text being rendered."""
    frames = traceback.extract_tb(error.__traceback__)[-FAILURE_FRAMES:]
    where = " < ".join(
        f"{Path(frame.filename).name}:{frame.lineno} {frame.name}" for frame in reversed(frames)
    )
    return f"{type(error).__name__} at {where}" if where else type(error).__name__


def _send(protocol: IO[str], message: dict[str, Any]) -> None:
    protocol.write(json.dumps(message) + "\n")
    protocol.flush()


def main() -> int:
    protocol = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)  # everything else printed goes to stderr
    line = sys.stdin.readline()
    if not line:
        return 2
    job: dict[str, Any] = json.loads(line)
    work_directory = Path(job["work_directory"])
    _private_work_directory(work_directory)
    tempfile.tempdir = str(work_directory)
    os.environ["TMPDIR"] = str(work_directory)
    try:
        return _serve(job, protocol)
    finally:
        shutil.rmtree(work_directory, ignore_errors=True)


def _serve(job: dict[str, Any], protocol: IO[str]) -> int:
    try:
        import numpy as np
        import soundfile as sf  # type: ignore[import-not-found]
        import torch  # type: ignore[import-not-found]
        from f5_tts.infer import utils_infer  # type: ignore[import-not-found]
        from f5_tts.model import DiT  # type: ignore[import-not-found]
        from f5_tts.model.utils import seed_everything  # type: ignore[import-not-found]
        from omegaconf import OmegaConf  # type: ignore[import-not-found]

        device = job["device"]
        config = OmegaConf.load(str(files("f5_tts").joinpath("configs/F5TTS_v1_Base.yaml")))
        vocoder = utils_infer.load_vocoder(
            vocoder_name="vocos", is_local=True, local_path=job["vocoder_path"], device=device
        )
        model = utils_infer.load_model(
            DiT,
            config.model.arch,
            job["checkpoint_path"],
            mel_spec_type="vocos",
            vocab_file=job["vocab_path"],
            use_ema=bool(job["use_ema"]),
            device=device,
        )
        reference_path, reference_text = utils_infer.preprocess_ref_audio_text(
            job["reference_audio_path"], job["reference_text"], show_info=lambda *_: None
        )
        reference = utils_infer.torchaudio.load(reference_path)
        from importlib.metadata import version

        _send(
            protocol,
            {
                "event": "ready",
                "f5_tts_version": version("f5-tts"),
                "torch_version": str(torch.__version__),
                "model_dtype": str(next(model.parameters()).dtype),
            },
        )
    except Exception as error:  # noqa: BLE001 - reported to the job runner, which fails the job
        _send(protocol, {"event": "failed", "error": f"{type(error).__name__}: {error}"[:1000]})
        return 1
    for line in sys.stdin:
        request: dict[str, Any] = json.loads(line)
        if request.get("op") == "quit":
            return 0
        identifier = request.get("id")
        try:
            seed_everything(int(request["seed"]))
            chunks = [
                chunk
                for chunk, _ in utils_infer.infer_batch_process(
                    reference,
                    reference_text,
                    [request["text"]],
                    model,
                    vocoder,
                    mel_spec_type="vocos",
                    progress=None,
                    nfe_step=int(request["nfe_steps"]),
                    device=device,
                    streaming=True,
                    chunk_size=1 << 30,
                )
            ]
            wave = np.concatenate(chunks)
            output = Path(request["output_path"])
            # The job runner renders to ".NNNN.wav.part": soundfile cannot tell the format
            # from that name (TypeError), so it is named.
            sf.write(str(output), wave, 24_000, format="WAV", subtype="PCM_16")
            os.chmod(output, 0o600)
            answer: dict[str, Any] = {
                "event": "rendered",
                "id": identifier,
                "frames": int(len(wave)),
            }
            if torch.cuda.is_available():  # ROCm answers through the same API
                answer["peak_memory_bytes"] = int(torch.cuda.max_memory_allocated())
            _send(protocol, answer)
        except Exception as error:  # noqa: BLE001 - one segment fails, the worker stays
            _send(protocol, {"event": "failed", "id": identifier, "error": _failure(error)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
