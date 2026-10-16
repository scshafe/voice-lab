"""The speech configuration and its caller registry (D-046, docs/SPEECH_API.md §4).

@canonical speech.config -- one strict JSON file, ``/data/voice-lab/config/speech.json`` on the
PC, root-owned and readable by ``voice-lab``, read by the API (its directory bound read-only
into the container; infra refuses any bind under ``/etc``), the job runner and the webhook
delivery process. It holds no secret: a caller's webhook secret is a file named by path
(root-only, under ``/etc/voice-lab/speech-webhook-secrets/``, never bound into a container),
which only the delivery process reads (``webhooks.read_secret``).

A caller is a machine client of the identity provider. Its tokens' issuer, subject and
audience are listed explicitly, because what the provider puts in a client-credentials
token is read from a real one before the caller is registered.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Final, Literal
from urllib.parse import SplitResult, urlsplit

from pydantic import AfterValidator, Field, StringConstraints, ValidationError, model_validator

from ..canonical import open_regular_no_follow
from ..errors import ContractError, VoiceLabError
from ..models import F5_FAMILY, ContractModel, StableId
from ..paths import validate_absolute_path
from ..recording_contracts import (
    SPEECH_DEFAULT_NFE_STEPS,
    SPEECH_MAX_NFE_STEPS,
    SPEECH_MAX_TEXT_CHARACTERS,
    SPEECH_MIN_NFE_STEPS,
)
from .release import SPEECH_SUBJECT

SPEECH_CONFIG_SCHEMA: Final = "cole-voice-lab.speech-config/v1"
MAX_CONFIG_BYTES: Final = 256 * 1024
MAX_CALLERS: Final = 4
MAX_WEBHOOK_URL_LENGTH: Final = 2048
# The one use a caller of the speech API may be registered for (D-046). It is the caller's
# scope in the registry, not a consent purpose: consent records are retired (D-050).
type SpeechPurpose = Literal["private-assistant-speech"]

_LABEL: Final = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_TAILNET_SUFFIX: Final = re.compile(rf"^(?:{_LABEL}\.)+ts\.net$")
_HOST_LABEL: Final = re.compile(rf"^{_LABEL}$")
_SECRET_NAME: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# No ``;``: a receiver that reads path parameters (``/hooks/..;/admin``) would see a path that
# the allowlist check did not.
_PATH: Final = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,=:@%/-]*$")
# A percent-escape is two hex digits, and never an encoded dot, slash, semicolon or
# backslash: a receiver that decodes them would see a path that the allowlist check did not.
_BAD_ESCAPE: Final = re.compile(r"%(?![0-9A-Fa-f]{2})|%2[EeFf]|%3[Bb]|%5[Cc]")

# Only Cole's stable subject id (``release.SPEECH_SUBJECT``; a test holds the two equal): the
# configuration cannot name another subject (D-045, D-050).
type SpeechSubject = Literal["cole-self"]

type AbsolutePath = Annotated[
    str, StringConstraints(min_length=1, max_length=4096), AfterValidator(validate_absolute_path)
]


def _tailnet_suffix(value: str) -> str:
    if len(value) > 253 or not _TAILNET_SUFFIX.fullmatch(value):
        raise ValueError("the tailnet suffix is a lowercase name ending in .ts.net")
    return value


type TailnetSuffix = Annotated[str, AfterValidator(_tailnet_suffix)]


def _issuer(value: str) -> str:
    if not value.startswith("https://") or value.endswith("/") or len(value) > 512:
        raise ValueError("an issuer is an https URL without a trailing slash")
    return value


type Issuer = Annotated[str, AfterValidator(_issuer)]
type TokenValue = Annotated[
    str, StringConstraints(min_length=1, max_length=256, pattern=r"^[\x21-\x7e]+$")
]


def webhook_url_problem(url: str, tailnet_suffix: str) -> str | None:
    """Why ``url`` cannot be a webhook URL on this tailnet, or ``None`` when it can.

    A webhook URL is ``https://<one label>.<tailnet suffix>[:port]/<path>``: no
    credentials, no query, no fragment, no IP literal, nothing outside printable ASCII, and
    a path without dot segments, empty segments or path parameters (``;``).
    """
    if len(url) > MAX_WEBHOOK_URL_LENGTH or not url.isascii() or not url.isprintable():
        return "a webhook URL is printable ASCII of at most 2048 characters"
    if any(character in url for character in " \\"):
        return "a webhook URL has no space or backslash"
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return "a webhook URL must parse"
    if parts.scheme != "https":
        return "a webhook URL is https"
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return "a webhook URL carries no credentials"
    if parts.query or parts.fragment or "?" in url or "#" in url:
        return "a webhook URL has no query or fragment"
    host = parts.hostname or ""
    if host != parts.netloc.split(":", 1)[0] or not host.endswith("." + tailnet_suffix):
        return f"a webhook URL's host is a node under {tailnet_suffix}"
    if not _HOST_LABEL.fullmatch(host.removesuffix("." + tailnet_suffix)):
        return "a webhook URL's host is one node name under the tailnet suffix"
    if port is not None and not 1 <= port <= 65535:
        return "a webhook URL's port is 1 to 65535"
    if ";" in parts.path:
        return "a webhook URL's path has no path parameters (';')"
    if not parts.path or not _PATH.fullmatch(parts.path):
        return "a webhook URL has an absolute path"
    if _BAD_ESCAPE.search(parts.path):
        return "a webhook URL's path has no encoded dot, slash, semicolon or backslash"
    segments = parts.path.split("/")[1:]
    if any(segment in {".", ".."} for segment in segments):
        return "a webhook URL's path has no dot segments"
    if "" in segments[:-1]:
        return "a webhook URL's path has no empty segments"
    return None


def callback_target(url: str, tailnet_suffix: str) -> tuple[str, int, tuple[str, ...]] | None:
    """A webhook URL as it is compared with an allowlist: its host, its port (443 when it
    names none) and its path segments (a trailing slash dropped); ``None`` when it is not a
    webhook URL on this tailnet."""
    if webhook_url_problem(url, tailnet_suffix) is not None:
        return None
    parts = urlsplit(url)
    segments = tuple(parts.path.split("/")[1:])
    if segments and segments[-1] == "":
        segments = segments[:-1]
    return parts.hostname or "", parts.port or 443, segments


def parse_webhook_url(url: str, tailnet_suffix: str) -> SplitResult:
    """``url``'s parts once ``webhook_url_problem`` finds nothing wrong.

    Raises: ``ValueError`` naming the problem.
    """
    problem = webhook_url_problem(url, tailnet_suffix)
    if problem is not None:
        raise ValueError(problem)
    return urlsplit(url)


class SpeechCaller(ContractModel):
    """One registered caller: who its tokens say it is, what it may do, and its limits."""

    caller_id: StableId
    issuer: Issuer
    subject: TokenValue
    audiences: tuple[TokenValue, ...] = Field(min_length=1, max_length=4)
    allowed_purposes: tuple[SpeechPurpose, ...] = Field(min_length=1, max_length=1)
    webhook_urls: tuple[str, ...] = Field(default=(), max_length=8)
    webhook_secret_file: AbsolutePath | None = None
    max_queued_jobs: int = Field(default=4, ge=1, le=16)
    max_text_characters: int = Field(default=SPEECH_MAX_TEXT_CHARACTERS, ge=1, le=2000)
    daily_character_budget: int = Field(default=50_000, ge=1, le=1_000_000)

    @model_validator(mode="after")
    def _webhooks_need_a_secret(self) -> SpeechCaller:
        if bool(self.webhook_urls) != (self.webhook_secret_file is not None):
            raise ValueError("a caller with webhook URLs names a secret file, and only then")
        if len(set(self.webhook_urls)) != len(self.webhook_urls):
            raise ValueError("a caller lists each webhook URL once")
        return self

    def allows_callback(self, url: str, tailnet_suffix: str) -> bool:
        """Whether ``url`` is a webhook URL on the tailnet that equals one of the caller's, or
        extends one by whole path segments (compared after parsing: host, port and segments)."""
        target = callback_target(url, tailnet_suffix)
        if target is None:
            return False
        host, port, segments = target
        for allowed in self.webhook_urls:
            known = callback_target(allowed, tailnet_suffix)
            if (
                known is not None
                and (known[0], known[1]) == (host, port)
                and segments[: len(known[2])] == known[2]
            ):
                return True
        return False


class F5SpeechRuntime(ContractModel):
    """The F5-TTS runtime the job runner renders an ``f5-tts`` release with (D-048)."""

    model_family: Literal["f5-tts"] = F5_FAMILY
    runtime_root: AbsolutePath
    device: Literal["cuda", "cpu"] = "cuda"
    # Flow steps: 32 until Cole compares 16 and 32 by ear (D-046); every ledger entry names it.
    nfe_steps: int = Field(
        default=SPEECH_DEFAULT_NFE_STEPS, ge=SPEECH_MIN_NFE_STEPS, le=SPEECH_MAX_NFE_STEPS
    )
    # HIP_VISIBLE_DEVICES for the resident worker (the discrete card, design §7.1), if set.
    visible_devices: str | None = Field(default=None, pattern=r"^[0-9]{1,2}(,[0-9]{1,2}){0,3}$")


# The runtime of the release's model family: one member per family (D-048). Choosing a family
# is designating a release of it; the configuration only says where its runtime is.
type SpeechRuntime = Annotated[F5SpeechRuntime, Field(discriminator="model_family")]


class SpeechConfig(ContractModel):
    """The whole speech configuration (docs/SPEECH_API.md §4)."""

    schema_version: Literal["cole-voice-lab.speech-config/v1"] = SPEECH_CONFIG_SCHEMA
    asset_root: AbsolutePath
    state_root: AbsolutePath
    gpu_slot_root: AbsolutePath
    # The one subject whose release may speak (AGENTS.md: never another subject), pinned in
    # code to ``SPEECH_SUBJECT``. A release of any other subject is refused (D-050).
    subject_id: SpeechSubject = SPEECH_SUBJECT
    tailnet_suffix: TailnetSuffix
    audio_retention_hours: int = Field(default=168, ge=1, le=720)
    record_retention_days: int = Field(default=30, ge=1, le=365)
    queue_limit: int = Field(default=32, ge=1, le=256)
    queue_timeout_seconds: int = Field(default=600, ge=10, le=3600)
    segment_deadline_seconds: int = Field(default=120, ge=10, le=600)
    idle_unload_seconds: int = Field(default=900, ge=10, le=86_400)
    gpu_busy_retry_seconds: int = Field(default=600, ge=1, le=86_400)
    runtime: SpeechRuntime | None = None

    def runtime_for(self, family: str) -> SpeechRuntime | None:
        """The configured runtime when it is ``family``'s, else ``None``."""
        runtime = self.runtime
        return runtime if runtime is not None and runtime.model_family == family else None

    @property
    def base_family(self) -> str:
        """The family whose base voice speaks while no release exists (D-055): the configured
        runtime's, else F5-TTS (whose base voice is then unavailable: its files are in the
        runtime)."""
        return F5_FAMILY if self.runtime is None else self.runtime.model_family

    callers: tuple[SpeechCaller, ...] = Field(min_length=1, max_length=MAX_CALLERS)

    @model_validator(mode="after")
    def _callers_are_distinct_and_tailnet_only(self) -> SpeechConfig:
        ids = [caller.caller_id for caller in self.callers]
        principals = [(caller.issuer, caller.subject) for caller in self.callers]
        secrets = [caller.webhook_secret_file for caller in self.callers]
        named_secrets = [path for path in secrets if path is not None]
        if len(set(ids)) != len(ids) or len(set(principals)) != len(principals):
            raise ValueError("callers have distinct ids and distinct issuer and subject pairs")
        # The delivery unit receives the secrets as credentials named by file name.
        names = [Path(path).name for path in named_secrets]
        if len(set(names)) != len(names):
            raise ValueError("callers never share a webhook secret file or its file name")
        if not all(_SECRET_NAME.fullmatch(name) for name in names):
            raise ValueError("a webhook secret file's name is letters, digits, '.', '_', '-'")
        for caller in self.callers:
            for url in caller.webhook_urls:
                problem = webhook_url_problem(url, self.tailnet_suffix)
                if problem is not None:
                    raise ValueError(f"caller {caller.caller_id}: {problem}")
        return self

    def caller(self, caller_id: str) -> SpeechCaller | None:
        return next((caller for caller in self.callers if caller.caller_id == caller_id), None)

    @property
    def state_path(self) -> Path:
        return Path(self.state_root)


def load_speech_config(path: Path) -> SpeechConfig:
    """Read and validate the configuration at ``path`` (a regular file, never a link).

    Raises: ``ContractError`` for a missing, oversized or invalid file.
    """
    try:
        with open_regular_no_follow(path) as stream:
            data = stream.read(MAX_CONFIG_BYTES + 1)
    except (OSError, VoiceLabError) as error:
        raise ContractError(
            f"the speech configuration cannot be read as a regular file: {path}"
        ) from error
    if len(data) > MAX_CONFIG_BYTES:
        raise ContractError(f"the speech configuration is larger than {MAX_CONFIG_BYTES} bytes")
    try:
        return SpeechConfig.model_validate_json(data)
    except ValidationError as error:
        raise ContractError(f"the speech configuration is invalid: {error}") from error
