"""A stand-in F5-TTS runtime for fine-tune tests and the native ``finetune`` suite.

The runtime's ``bin/python`` is a script that answers the runtime probe, packs a dataset,
"trains" (printing tqdm-shaped status lines) and "synthesizes" silent samples, so the real
fine-tune chain runs end to end without torch, weights or a GPU. Its pretrained weights and
Vocos vocoder files are a few bytes, so callers pass the pins this module returns
(``FineTuneSupervisor`` / ``run_finetune_pipeline`` ``pretrained_pin``); the shipped service
never does.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from cole_voice_lab.canonical import sha256_file
from cole_voice_lab.finetune import FineTunePinnedFile, FineTunePretrainedPins

STUB_RUNTIME_PYTHON = '''#!{python}
"""Stand-in for the F5 runtime python: answers the probe, packs, trains and synthesizes.

<root>/stub-mode.json, when present, slows or breaks it: probe_seconds (the probe sleeps),
step_seconds (each training status line waits), fail_training, ignore_sigterm (the trainer
survives SIGTERM, so only SIGKILL stops it), sample_seconds (sample synthesis sleeps before
writing), fail_sampling (sample synthesis exits 1), synthesis_seconds (Speak synthesis, the
job's file_prefix "speech", sleeps before writing), fail_synthesis (Speak synthesis exits 1),
synthesis_format ([rate, channels, sample width] Speak writes instead of 24000 Hz mono
16-bit), synthesis_report (a line Speak prints instead of its clip report). A Speak clip's
240 frames follow a pattern set by its text, so different texts give different clips;
listening samples are silent.
"""
import json, os, signal, sys, time, wave
from pathlib import Path

ROOT = Path({root!r})
PACKAGE = ROOT / "lib" / "python3.12" / "site-packages" / "f5_tts"
MODE_PATH = ROOT / "stub-mode.json"
MODE = json.loads(MODE_PATH.read_text()) if MODE_PATH.exists() else {{}}
argv = [a for a in sys.argv[1:] if a != "-B"]
if argv[:1] == ["-c"]:
    time.sleep(MODE.get("probe_seconds", 0))
    print(json.dumps({{"f5_tts_version": "1.1.22-stub", "torch_version": "2.14.0-stub",
                       "package_path": str(PACKAGE), "home": os.environ.get("HOME"),
                       "hf_hub_offline": os.environ.get("HF_HUB_OFFLINE")}}))
    sys.exit(0)
script = Path(argv[0]).name
if script == "prepare_csv_wavs.py":
    out = Path(argv[2]); out.mkdir(parents=True, exist_ok=True)
    rows = Path(argv[1]).read_text().splitlines()[1:]
    (out / "raw.arrow").write_bytes(b"arrow")
    (out / "duration.json").write_text(json.dumps({{"duration": [1.0] * len(rows)}}))
    print(f"packed {{len(rows)}} rows")
    sys.exit(0)
if script == "f5_finetune_launcher.py":
    name = argv[argv.index("--dataset_name") + 1]
    print("lab launcher stub argv: " + " ".join(argv[1:]), flush=True)
    if name.startswith("fail") or MODE.get("fail_training"):
        print("Traceback: stub training failure", file=sys.stderr); sys.exit(1)
    if MODE.get("ignore_sigterm"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    epochs = int(argv[argv.index("--epochs") + 1])
    update = 0
    for epoch in range(1, epochs + 1):
        for step in (1, 2):
            update += 1
            # tqdm's shape: \\r-separated status lines on stderr (merged into train.log).
            sys.stderr.write(
                f"\\rEpoch {{epoch}}/{{epochs}}:  {{step * 50}}%|#####     | {{step}}/2 "
                f"[00:0{{step}}<00:0{{2 - step}},  1.00s/update, loss={{0.9 / epoch:.3f}}, "
                f"update={{update}}]"
            )
            sys.stderr.flush()
            time.sleep(MODE.get("step_seconds", 0))
    sys.stderr.write("\\n")
    ckpts = ROOT / "lib" / "python3.12" / "ckpts" / name
    (ckpts / "model_9.pt").write_bytes(b"intermediate")
    (ckpts / "model_last.pt").write_bytes(b"last")
    print("Saved last checkpoint at update 9")
    sys.exit(0)
if script == "f5_synthesize.py":
    job = json.loads(Path(argv[1]).read_text())
    prefix = job.get("file_prefix", "sample")
    speech = prefix == "speech"
    time.sleep(MODE.get("synthesis_seconds" if speech else "sample_seconds", 0))
    if MODE.get("fail_synthesis" if speech else "fail_sampling"):
        print("Traceback: stub " + ("speech" if speech else "sampling") + " failure",
              file=sys.stderr); sys.exit(1)
    rate, channels, width = MODE.get("synthesis_format", [24000, 1, 2]) if speech else [24000, 1, 2]
    out = Path(job["output_directory"]); out.mkdir(parents=True, exist_ok=True)
    assert Path(job["checkpoint_path"]).is_file() and Path(job["reference_audio_path"]).is_file()
    # The pinned vocoder is loaded from the runtime's own copy, as f5_synthesize.py does.
    vocoder = Path(job["vocoder_path"])
    assert vocoder.resolve() == (ROOT / "pretrained" / "vocos-mel-24khz").resolve(), vocoder
    assert (vocoder / "config.yaml").is_file() and (vocoder / "pytorch_model.bin").is_file()
    for index, text in enumerate(job["texts"], start=1):
        path = out / f"{{prefix}}-{{index:02d}}.wav"
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(channels); handle.setsampwidth(width); handle.setframerate(rate)
            if speech and (channels, width) == (1, 2):
                seed = sum(text.encode()) % 997
                handle.writeframes(b"".join(
                    ((frame * 131 + seed * 7) % 20_000 - 10_000).to_bytes(2, "little", signed=True)
                    for frame in range(240)))
            else:
                handle.writeframes(bytes(240 * channels * width))
        if speech and "synthesis_report" in MODE:
            print(MODE["synthesis_report"])
        else:
            print(json.dumps({{"index": index, "path": str(path), "seconds": 0.01}}))
    sys.exit(0)
print("unexpected invocation", argv, file=sys.stderr); sys.exit(3)
'''


def write_stub_mode(root: Path, mode: dict[str, Any]) -> None:
    """Replace the stub runtime's ``stub-mode.json``; each later runtime invocation reads it."""

    (root / "stub-mode.json").write_text(json.dumps(mode), encoding="utf-8")


def _pin(path: Path) -> FineTunePinnedFile:
    digest, size = sha256_file(path)
    return FineTunePinnedFile(sha256=digest, size_bytes=size)


def stub_finetune_runtime(
    root: Path, mode: dict[str, Any] | None = None
) -> tuple[Path, FineTunePretrainedPins]:
    """Create a fake F5 runtime at ``root``; return ``root`` and its pretrained-file pins.

    ``mode`` is written to ``stub-mode.json`` (see STUB_RUNTIME_PYTHON).
    Raises: ``FileExistsError`` when ``root/bin`` already exists.
    """

    (root / "bin").mkdir(parents=True)
    if mode is not None:
        write_stub_mode(root, mode)
    python = root / "bin" / "python"
    python.write_text(STUB_RUNTIME_PYTHON.format(python=sys.executable, root=str(root)))
    python.chmod(0o700)
    package = root / "lib" / "python3.12" / "site-packages" / "f5_tts"
    (package / "train" / "datasets").mkdir(parents=True)
    (package / "train" / "datasets" / "prepare_csv_wavs.py").write_text("# stub\n")
    pretrained = root / "pretrained" / "F5TTS_v1_Base"
    pretrained.mkdir(parents=True)
    (pretrained / "model_1250000.safetensors").write_bytes(b"pretrained-weights")
    (pretrained / "vocab.txt").write_text("a\nb\n")
    vocoder = root / "pretrained" / "vocos-mel-24khz"
    vocoder.mkdir()
    (vocoder / "config.yaml").write_text("feature_extractor: stub\n")
    (vocoder / "pytorch_model.bin").write_bytes(b"vocoder-weights")
    return root, FineTunePretrainedPins(
        weights=_pin(pretrained / "model_1250000.safetensors"),
        vocoder_config=_pin(vocoder / "config.yaml"),
        vocoder_model=_pin(vocoder / "pytorch_model.bin"),
    )
