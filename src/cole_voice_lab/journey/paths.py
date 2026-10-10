"""One place that decides where the journey pipeline reads and writes (paths.mjs).

Without a data root (``VOICE_JOURNEY_DATA`` unset) the layout is voice-journey's
historical local-development one, relative to the working directory. With a data root
one directory owns every mutable thing::

    $D/corpus/phone/        the phone chain mirror (the mirror writes it; read-only here)
    $D/corpus/measurement/  accepted measurement sessions, <sessionId>/...
    $D/artifacts/           transcripts, the feature store, scratch
    $D/manifests/           GENERATED manifests (index, results, stt, lyrics, ...)
    $D/staging/             intake uploads in flight (same volume as corpus/)
    $D/models/              the whisper.cpp model (fetched once, sha256-pinned)

On the PC (design §4.1) the data root is ``/data/voice-lab/journey`` and the
overrides point the phone corpus at ``/data/voice-lab/memos/phone`` and the API's
mutable state at ``/data/voice-lab/state`` (``VOICE_JOURNEY_STATE_DIR``).

The one INPUT manifest, the chain registry, is authored and versioned: it ships in
the package (``resources/journey-chains.json``, without calibration paths) unless
``VOICE_JOURNEY_CHAINS`` names another file.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

MAC_PHONE_CORPUS = (
    Path.home() / "Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings"
)

GENERATED_MANIFESTS: tuple[str, ...] = (
    "recording-index.json",
    "filtering-sample.json",
    "filter-results.json",
    "full-corpus-filter-results.json",
    "local-stt-transcripts.json",
    "local-lyric-indicators.json",
    "local-voice-features.json",
    "song-journeys.json",
)
INPUT_MANIFESTS: tuple[str, ...] = ("chains.json",)

_THREADS = re.compile(r"[1-9][0-9]{0,3}")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def packaged_chains() -> Path:
    """The chain registry that ships with the package."""

    return Path(str(files("cole_voice_lab").joinpath("resources", "journey-chains.json")))


@dataclass(frozen=True)
class JourneyPaths:
    """Every location the pipeline and the API use, resolved once from the environment."""

    data_root: Path | None
    manifests_dir: Path
    artifacts_dir: Path
    chains: Path
    phone_corpus_root: Path
    measurement_dir: Path
    staging_dir: Path
    models_dir: Path | None
    state_dir: Path

    def manifest(self, name: str) -> Path:
        return self.manifests_dir / name

    def artifact(self, *parts: str) -> Path:
        return self.artifacts_dir.joinpath(*parts)


@dataclass(frozen=True)
class JourneyTools:
    """External binaries and the isolated analysis runtime, from the environment."""

    ffmpeg_bin: str
    whisper_bin: str
    whisper_model: str
    whisper_threads: int | None
    venv_python: str


def _environ(env: Mapping[str, str] | None) -> Mapping[str, str]:
    if env is not None:
        return env
    import os

    return os.environ


def resolve_data_root(env: Mapping[str, str] | None = None) -> Path | None:
    value = _environ(env).get("VOICE_JOURNEY_DATA")
    return Path(value).resolve() if value and value.strip() else None


def is_container(env: Mapping[str, str] | None = None) -> bool:
    return _environ(env).get("VOICE_JOURNEY_CONTAINER") == "1"


def resolve_paths(env: Mapping[str, str] | None = None) -> JourneyPaths:
    """Relative (cwd-based) paths in local development; absolute under a data root."""

    environ = _environ(env)
    data = resolve_data_root(environ)
    manifests_dir = data / "manifests" if data else Path("manifests")
    artifacts_dir = data / "artifacts" if data else Path("local-artifacts")
    corpus_dir = data / "corpus" if data else artifacts_dir / "corpus"
    default_phone = corpus_dir / "phone" if data else MAC_PHONE_CORPUS
    default_staging = data / "staging" if data else artifacts_dir / "staging"

    def override(name: str, default: Path) -> Path:
        value = environ.get(name)
        return Path(value) if value is not None else default

    return JourneyPaths(
        data_root=data,
        manifests_dir=manifests_dir,
        artifacts_dir=artifacts_dir,
        chains=override("VOICE_JOURNEY_CHAINS", packaged_chains()),
        phone_corpus_root=override("VOICE_JOURNEY_CORPUS_ROOT", default_phone),
        measurement_dir=override("VOICE_JOURNEY_MEASUREMENT_DIR", corpus_dir / "measurement"),
        staging_dir=override("VOICE_JOURNEY_STAGING_DIR", default_staging),
        models_dir=data / "models" if data else None,
        state_dir=override("VOICE_JOURNEY_STATE_DIR", artifacts_dir),
    )


def parse_whisper_threads(value: str | None) -> int | None:
    """``WHISPER_CPP_THREADS``: unset or empty is None; a plain positive integer is
    passed to whisper-cli as ``-t <n>``; anything else is refused, never ignored."""

    if value is None or value == "":
        return None
    if not _THREADS.fullmatch(value):
        raise ValueError(f"WHISPER_CPP_THREADS must be a positive integer (1-9999), got {value!r}")
    return int(value)


def resolve_tools(
    env: Mapping[str, str] | None = None, paths: JourneyPaths | None = None
) -> JourneyTools:
    """Binaries come from the environment; bare names resolve through PATH."""

    environ = _environ(env)
    resolved = paths if paths is not None else resolve_paths(environ)
    default_model = (
        resolved.models_dir / "ggml-medium.bin"
        if resolved.models_dir is not None
        else Path("models/ggml-medium.bin")
    )
    default_python = (
        resolved.data_root / "runtimes" / "analysis" / "bin" / "python"
        if resolved.data_root is not None
        else Path("analysis/.venv/bin/python")
    )
    return JourneyTools(
        ffmpeg_bin=environ.get("FFMPEG_BIN", "ffmpeg"),
        whisper_bin=environ.get("WHISPER_CPP_BIN", "whisper-cli"),
        whisper_model=environ.get("WHISPER_CPP_MODEL", str(default_model)),
        whisper_threads=parse_whisper_threads(environ.get("WHISPER_CPP_THREADS")),
        venv_python=environ.get("VOICE_JOURNEY_PYTHON", str(default_python)),
    )


def is_inside_artifacts(file_path: Path | str, env: Mapping[str, str] | None = None) -> bool:
    """True when ``file_path`` is inside the artifacts directory (the out-of-tree store);
    the manifests record this as ``gitignoredExpected``."""

    artifacts = resolve_paths(env).artifacts_dir.resolve()
    target = Path(file_path).resolve()
    return target == artifacts or artifacts in target.parents


def assert_bind_allowed(host: str, env: Mapping[str, str] | None = None) -> None:
    """Loopback only outside the container; the door and sidecar front it inside."""

    if host in _LOOPBACK_HOSTS or is_container(env):
        return
    raise ValueError(
        f"refusing to bind {host}: Voice Journey binds loopback only outside the container "
        "(set VOICE_JOURNEY_CONTAINER=1 in the image)"
    )
