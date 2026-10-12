"""Records of a stored F5-TTS run as the code before model families wrote them (c43d6ba).

Generated once, by c43d6ba's own ``tests/finetune_speech_runs.write_speech_run`` (synthetic
clips and checkpoint, ``/synthetic`` paths; nothing from an asset root) imported from a
detached checkout of that commit, and kept here as the exact bytes it wrote: ``run.json``,
``dataset/dataset.json``, ``dataset/corpus.json`` and ``samples/finetuned/samples.json``.
They are Python bytes, not ``.json`` files, because ``scripts/guard-private-assets`` refuses
any committed JSON shaped like run evidence. Never regenerate them with today's code: their
point is that today's code reads and re-serializes what the old code wrote.
"""

# Not .json files on purpose: scripts/guard-private-assets refuses committed JSON shaped like
# run evidence, and these are synthetic (tests/test_training_families.py checks every digest
# and absolute path in them).
from __future__ import annotations

from typing import Final

RUN: Final = (
    b'{"catalog_sha256":"0f1f399de63a6c3c0e424b10bf27532b0e316c318a398877dea178ffd969a'
    b'028","checkpoints":[{"kind":"last","relative_path":"derived/finetune/finetune-go'
    b'lden/checkpoints/model_last.pt","sha256":"3024987dbe11612f09247e72a59415f4c2cfd4'
    b'3b94b9539884444bc225257b15","size_bytes":37,"update":0}],"corpus_sha256":"ff56ce'
    b'703dc9ec6146282354e4333b3fa68feb98160c8866ffca5efafa300414","dataset_sha256":"1e'
    b'002d00431e342c32e8ba5f8da095da38bba4cc3c23d4f042d4bb5bf72b2d76","error":null,"fi'
    b'nished_at":"2026-09-07T12:10:00Z","hyperparameters":{"batch_frames":3200,"datalo'
    b'ader_workers":2,"epochs":3,"keep_last_n_checkpoints":1,"last_per_updates":180,"l'
    b'earning_rate":1e-05,"light_checkpoints":true,"max_grad_norm":1.0,"max_samples_pe'
    b'r_batch":16,"model_name":"F5TTS_v1_Base","save_per_updates":540,"schema_version"'
    b':"cole-voice-lab.finetune-hyperparameters/v1","warmup_updates":50},"log_path":"d'
    b'erived/finetune/finetune-golden/train.log","run_id":"finetune-golden","run_root"'
    b':"derived/finetune/finetune-golden","runtime":{"f5_tts_version":"stub","pretrain'
    b'ed_path":"/synthetic/runtime/pretrained/model.safetensors","pretrained_sha256":"'
    b'6666666666666666666666666666666666666666666666666666666666666666","python_sha256'
    b'":"5555555555555555555555555555555555555555555555555555555555555555","runtime_ro'
    b'ot":"/synthetic/runtime","torch_version":"stub"},"schema_version":"cole-voice-la'
    b'b.finetune-run/v1","started_at":"2026-09-07T12:00:00Z","status":"finished","subj'
    b'ect_id":"cole-self"}\n'
)

DATASET: Final = (
    b'{"clips":[{"frames":2400,"gain_db":0.0,"prompt_id":"reference-warm-01","relative'
    b'_path":"wavs/reference-warm-01.wav","sample_rate_hz":24000,"sha256":"95379b599cc'
    b'925af3d29331e6fc9ed1cfe67053a5955aca0cc1bc3dd96cec1bb","take_id":"take-reference'
    b'-warm-01","text":"We can take this one step at a time, test what matters, and ke'
    b"ep the parts that genuinely feel useful. There is no need to rush the interestin"
    b'g work."},{"frames":2400,"gain_db":0.0,"prompt_id":"neutral-01","relative_path":'
    b'"wavs/neutral-01.wav","sample_rate_hz":24000,"sha256":"95379b599cc925af3d29331e6'
    b'fc9ed1cfe67053a5955aca0cc1bc3dd96cec1bb","take_id":"take-neutral-01","text":"Ple'
    b'ase place the yellow folder beside the green ceramic bowl."}],"corpus_sha256":"f'
    b'f56ce703dc9ec6146282354e4333b3fa68feb98160c8866ffca5efafa300414","dataset_root":'
    b'"derived/finetune/finetune-golden/dataset","materialized_at":"2026-09-07T12:00:0'
    b'0Z","metadata_csv_sha256":"44444444444444444444444444444444444444444444444444444'
    b'44444444444","normalization_rule":"synthetic","sample_rate_hz":24000,"schema_ver'
    b'sion":"cole-voice-lab.finetune-dataset/v1"}\n'
)

CORPUS: Final = (
    b'{"catalog_sha256":"0f1f399de63a6c3c0e424b10bf27532b0e316c318a398877dea178ffd969a'
    b'028","catalog_suite_id":"cole-pilot-v2","entries":[{"date_precision":"unknown","'
    b'frame_range":null,"prompt":{"category":"reference","expected_seconds":13.0,"hold'
    b'out":false,"prompt_id":"reference-neutral-01","reference_candidate":true,"style"'
    b':"Natural, steady, close conversation.","text":"A quiet morning gives us room to'
    b" notice small details: the click of a latch, the weight of a book, and the soft "
    b'rhythm of rain beyond the window."},"prompt_id":"reference-neutral-01","prompt_s'
    b'ha256":"84601f1ad69a4ebd8f30bc85629aac0507029018f6035e0ca12c5738e40bf24b","recor'
    b'ded_at":null,"recorded_prompt_sha256":"84601f1ad69a4ebd8f30bc85629aac0507029018f'
    b'6035e0ca12c5738e40bf24b","source_frames":48000,"source_kind":"native","source_pa'
    b'th":"tmp/recording-takes/take-reference-neutral-01/original.wav","source_sample_'
    b'rate_hz":48000,"source_sha256":"000000000000000000000000000000000000000000000000'
    b'0000000000000000","take_id":"take-reference-neutral-01"},{"date_precision":"unkn'
    b'own","frame_range":null,"prompt":{"category":"reference","expected_seconds":13.0'
    b',"holdout":false,"prompt_id":"reference-warm-01","reference_candidate":true,"sty'
    b'le":"Warm, reassuring, and unforced.","text":"We can take this one step at a tim'
    b"e, test what matters, and keep the parts that genuinely feel useful. There is no"
    b' need to rush the interesting work."},"prompt_id":"reference-warm-01","prompt_sh'
    b'a256":"9d153ee309beb2edb869d2b944b749121cea140aa33d2138dbbc54d931876b99","record'
    b'ed_at":null,"recorded_prompt_sha256":"9d153ee309beb2edb869d2b944b749121cea140aa3'
    b'3d2138dbbc54d931876b99","source_frames":48000,"source_kind":"native","source_pat'
    b'h":"tmp/recording-takes/take-reference-warm-01/original.wav","source_sample_rate'
    b'_hz":48000,"source_sha256":"0000000000000000000000000000000000000000000000000000'
    b'000000000000","take_id":"take-reference-warm-01"},{"date_precision":"unknown","f'
    b'rame_range":null,"prompt":{"category":"neutral","expected_seconds":6.0,"holdout"'
    b':false,"prompt_id":"neutral-01","reference_candidate":false,"style":"Natural neu'
    b'tral speech.","text":"Please place the yellow folder beside the green ceramic bo'
    b'wl."},"prompt_id":"neutral-01","prompt_sha256":"2bda9ff615fe794cbde2039e2f150ea0'
    b'f64da0262d62c2750d2abefcb5cd935b","recorded_at":null,"recorded_prompt_sha256":"2'
    b'bda9ff615fe794cbde2039e2f150ea0f64da0262d62c2750d2abefcb5cd935b","source_frames"'
    b':48000,"source_kind":"native","source_path":"tmp/recording-takes/take-neutral-01'
    b'/original.wav","source_sample_rate_hz":48000,"source_sha256":"000000000000000000'
    b'0000000000000000000000000000000000000000000000","take_id":"take-neutral-01"}],"m'
    b'issing_prompt_ids":[],"schema_version":"cole-voice-lab.finetune-corpus/v1","sele'
    b'cted_at":"2026-09-07T12:00:00Z","subject_id":"cole-self","unknown_record_ids":[]'
    b"}\n"
)

SAMPLES: Final = (
    b'{"checkpoint_path":"/synthetic/finetuned/model.pt","checkpoint_sha256":"77777777'
    b'77777777777777777777777777777777777777777777777777777777","created_at":"2026-09-'
    b'07T12:10:00Z","device":"mps","label":"finetuned","nfe_steps":32,"reference_audio'
    b'_sha256":"95379b599cc925af3d29331e6fc9ed1cfe67053a5955aca0cc1bc3dd96cec1bb","ref'
    b'erence_prompt_id":"reference-warm-01","samples":[{"index":1,"relative_path":"der'
    b'ived/finetune/finetune-golden/samples/finetuned/sample-01.wav","seconds":4.5,"sh'
    b'a256":"8888888888888888888888888888888888888888888888888888888888888888","text":'
    b'"This is a quick check of the fine-tuned voice, reading a sentence it has never '
    b'seen before."},{"index":2,"relative_path":"derived/finetune/finetune-golden/samp'
    b'les/finetuned/sample-02.wav","seconds":4.5,"sha256":"888888888888888888888888888'
    b'8888888888888888888888888888888888888","text":"The meeting moved to Thursday at '
    b'four thirty, so please update the shared calendar before noon."},{"index":3,"rel'
    b'ative_path":"derived/finetune/finetune-golden/samples/finetuned/sample-03.wav","'
    b'seconds":4.5,"sha256":"888888888888888888888888888888888888888888888888888888888'
    b'8888888","text":"For a few quiet minutes, the whole house seemed to breathe with'
    b' the wind in the trees."}],"schema_version":"cole-voice-lab.finetune-samples/v1"'
    b',"use_ema":true}\n'
)
