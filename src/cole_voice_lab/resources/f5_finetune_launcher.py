"""Launch F5-TTS fine-tuning inside the pinned F5 runtime (run by that runtime's python).

canonical: finetune launcher -- the only way the lab starts an F5-TTS training process.
It wraps ``f5_tts.train.finetune_cli.main`` with Mac-sized defaults; every argument after
the launcher's own ``--lab-*`` options is passed to finetune_cli verbatim.

Options (all set by cole_voice_lab.finetune.run_f5_finetune):
  --lab-dataloader-workers N   DataLoader workers (finetune_cli hardcodes 16)
  --lab-light-checkpoints      save model + EMA weights only (2.7 GB, not resumable)
  --lab-no-checkpoint-activations  keep every transformer activation (needs ~20 GB on
                               this corpus; the default recomputes them in backward)

Side effects: writes checkpoints to the runtime's ``ckpts/<dataset_name>/`` directory as
finetune_cli does. Not idempotent: a second run resumes from ``model_last.pt`` if present.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

os.environ.setdefault("WANDB_MODE", "disabled")
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


def _parse_lab_options(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--lab-dataloader-workers", type=int, default=2)
    parser.add_argument("--lab-light-checkpoints", action="store_true")
    parser.add_argument("--lab-no-checkpoint-activations", action="store_true")
    return parser.parse_known_args(argv)


def _install_patches(options: argparse.Namespace) -> None:
    from f5_tts.model import trainer as trainer_mod  # type: ignore[import-not-found]
    from f5_tts.model.backbones import dit as dit_mod  # type: ignore[import-not-found]

    original_train = trainer_mod.Trainer.train
    original_save = trainer_mod.Trainer.save_checkpoint
    original_dit_init = dit_mod.DiT.__init__

    def train(
        self: Any, train_dataset: Any, num_workers: int = 16, resumable_with_seed: Any = None
    ) -> Any:
        return original_train(
            self,
            train_dataset,
            num_workers=options.lab_dataloader_workers,
            resumable_with_seed=resumable_with_seed,
        )

    def save_checkpoint(self: Any, update: int, last: bool = False) -> Any:
        if not options.lab_light_checkpoints:
            return original_save(self, update, last)
        self.optimizer.state_dict = lambda: {"omitted": "light checkpoint"}
        self.scheduler.state_dict = lambda: {"omitted": "light checkpoint"}
        try:
            return original_save(self, update, last)
        finally:
            del self.optimizer.state_dict
            del self.scheduler.state_dict

    def dit_init(self: Any, *args: Any, **kwargs: Any) -> None:
        if not options.lab_no_checkpoint_activations:
            kwargs["checkpoint_activations"] = True
        original_dit_init(self, *args, **kwargs)

    trainer_mod.Trainer.train = train
    trainer_mod.Trainer.save_checkpoint = save_checkpoint
    dit_mod.DiT.__init__ = dit_init


def main(argv: list[str] | None = None) -> int:
    options, passthrough = _parse_lab_options(sys.argv[1:] if argv is None else argv)
    print(
        f"lab launcher: workers={options.lab_dataloader_workers} "
        f"light_checkpoints={options.lab_light_checkpoints} "
        f"checkpoint_activations={not options.lab_no_checkpoint_activations}",
        flush=True,
    )
    print("finetune_cli argv: " + " ".join(passthrough), flush=True)
    _install_patches(options)
    from f5_tts.train.finetune_cli import main as finetune_main  # type: ignore[import-not-found]

    sys.argv = [sys.argv[0], *passthrough]
    finetune_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
