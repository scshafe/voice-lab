"""Shared fixtures of the speech API tests: a lab with a finished run, consent, a release,
a configuration, tokens for callers, and a fake backend that writes synthetic audio.

Everything is synthetic: the run is ``finetune_speech_runs.write_speech_run``'s (generated
clips and checkpoint bytes), the fake backend writes a generated tone, and the F5 runtime,
where one is used, is the stub of ``finetune_stub_runtime``.
"""

from __future__ import annotations

import base64
import json
import math
import struct
import threading
import wave
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from finetune_speech_runs import write_speech_run
from home_host_fixtures import ISSUER, KID, jwks, mint
from home_host_fixtures import policy as desktop_policy
from test_pilot_v2_coverage import SUBJECT, released_pilot_v2

from cole_voice_lab.api.auth import TokenVerifier
from cole_voice_lab.api.contracts import ApiMeta
from cole_voice_lab.api.server import ApiServer
from cole_voice_lab.api.staging import IntakeStaging
from cole_voice_lab.consent import approve_self_consent, install_pending_consent
from cole_voice_lab.finetune import FineTuneRun
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.speech.backend import PeakMemory, SpeechBackendError
from cole_voice_lab.speech.config import SpeechCaller, SpeechConfig
from cole_voice_lab.speech.contracts import F5RenderSettings, FileDigest, RenderFingerprint
from cole_voice_lab.speech.release import VerifiedRelease, designate
from cole_voice_lab.speech.routes import PREFIX, SpeechRoutes, caller_policy
from cole_voice_lab.speech.runner import PassReport, SpeechRunner

START = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
TAILNET = "example-tailnet.ts.net"
CALLER_SUBJECT = "client-agent-server"
CALLER_AUDIENCE = "voice-lab-speech-agent"
HOOK = f"https://agent.{TAILNET}/hooks/voice-lab"
KEY = bytes(range(32))
SECRET = "whsec_" + base64.b64encode(KEY).decode("ascii")


def write_tone(
    path: Path,
    *,
    frames: int = 2400,
    rate: int = 24_000,
    channels: int = 1,
    period: int = 60,
    stamp: int | None = None,
) -> None:
    """A synthetic tone: ``frames`` frames of 16-bit PCM (private). ``stamp`` (a seed) is
    written into the first four mono frames, so two seeds never give the same bytes."""
    samples = b"".join(
        struct.pack("<h", round(8000 * math.sin(index * math.tau / period))) * channels
        for index in range(frames)
    )
    if stamp is not None:
        samples = struct.pack("<Q", stamp % 2**64) + samples[8:]
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(samples)
    path.chmod(0o600)


def finished_run(layout: AssetLayout, run_id: str = "finetune-a", **changes: Any) -> FineTuneRun:
    fields: dict[str, Any] = {
        "catalog": released_pilot_v2(),
        "subject_id": SUBJECT,
        "started_at": START,
        "finished_at": START + timedelta(minutes=10),
        "entries": ("reference-neutral-01", "reference-warm-01", "neutral-01"),
        "clips": ("reference-warm-01", "neutral-01"),
        **changes,
    }
    return write_speech_run(layout, run_id, **fields)


def approve_consent(
    layout: AssetLayout,
    *,
    purposes: tuple[Any, ...] = ("private-assistant-speech",),
    boundaries: tuple[Any, ...] = ("home-host",),
    now: datetime = NOW - timedelta(days=1),
) -> str:
    _, pending = install_pending_consent(layout)
    _, digest = approve_self_consent(
        layout,
        acknowledgement=True,
        purposes=purposes,
        execution_boundaries=boundaries,
        expires_at=now + timedelta(days=365),
        expected_current_sha256=pending,
        now=now,
    )
    return digest


def caller(**changes: Any) -> SpeechCaller:
    fields: dict[str, Any] = {
        "caller_id": "agent-server",
        "issuer": ISSUER,
        "subject": CALLER_SUBJECT,
        "audiences": (CALLER_AUDIENCE,),
        "allowed_purposes": ("private-assistant-speech",),
        "webhook_urls": (HOOK,),
        "webhook_secret_file": "/etc/voice-lab/speech-webhook-secrets/agent-server",
        **changes,
    }
    return SpeechCaller.model_validate(fields)


def speech_config(root: Path, layout: AssetLayout, **changes: Any) -> SpeechConfig:
    fields: dict[str, Any] = {
        "asset_root": str(layout.root),
        "state_root": str(root / "state" / "speech"),
        "gpu_slot_root": str(root / "state" / "jobs"),
        "tailnet_suffix": TAILNET,
        "callers": (caller(),),
        **changes,
    }
    return SpeechConfig.model_validate(fields)


def caller_token(**claims: Any) -> str:
    return mint({"sub": CALLER_SUBJECT, "aud": CALLER_AUDIENCE, **claims})


@dataclass
class FakeBackend:
    """A stand-in for the resident model: each segment is a tone shaped by its seed, so the
    same seed gives the same bytes. It can fail, write something else, or call a hook."""

    frames: int = 2400
    fail: str | None = None
    fail_at: int = 1
    write: Callable[[Path], None] | None = None
    on_segment: Callable[[int], None] | None = None
    calls: list[tuple[str, int]] = field(default_factory=list)
    prepared: list[VerifiedRelease] = field(default_factory=list)
    unloads: int = 0
    loaded: bool = False
    peak: int | None = None

    def prepare(self, release: VerifiedRelease) -> RenderFingerprint:
        if self.fail == "runtime_unavailable":
            raise SpeechBackendError("runtime_unavailable", "fake: no runtime")
        self.prepared.append(release)
        self.loaded = True
        record = release.release
        return RenderFingerprint(
            model_family=record.model_family,
            backend="fake",
            files=tuple(FileDigest(role=item.role, sha256=item.sha256) for item in record.files),
            runtime={"fake": "1"},
            device="cpu",
            native_sample_rate_hz=24_000,
            seeded=True,
            settings=F5RenderSettings(nfe_steps=32),
            voice_lab_version="test",
        )

    def render_segment(
        self, text: str, *, seed: int, output: Path, deadline_seconds: float
    ) -> None:
        self.calls.append((text, seed))
        index = len(self.calls)
        if self.on_segment is not None:
            self.on_segment(index)
        if self.fail is not None and index == self.fail_at:
            raise SpeechBackendError(self.fail, f"fake {self.fail}")  # type: ignore[arg-type]
        if self.write is not None:
            self.write(output)
        else:
            write_tone(output, frames=self.frames, period=20 + seed % 40, stamp=seed)

    def peak_memory(self) -> PeakMemory | None:
        return None if self.peak is None else PeakMemory(bytes=self.peak, method="fake")

    def unload(self) -> None:
        self.loaded = False
        self.unloads += 1


class OnePass(SpeechRunner):
    """A runner whose every pass ends by unloading, as ``speech run --once`` does: tests that
    build a runner per pass must not leave the GPU slot held by one they dropped."""

    def run_pending(self, *, sweep: bool = True) -> PassReport:
        try:
            return super().run_pending(sweep=sweep)
        finally:
            self.unload()


@dataclass
class Lab:
    root: Path
    layout: AssetLayout
    run: FineTuneRun
    config: SpeechConfig
    consent_sha256: str

    def designate(self, **options: Any) -> None:
        options.setdefault("consent_id", "cole-self-v1")
        options.setdefault("run_id", self.run.run_id)
        designate(self.layout, designated_by="Cole", now=NOW, **options)


def designate_another_subject(lab: Lab) -> None:
    """Make the current release another subject's: a finished run and an approved consent
    record of ``someone-else``, designated as such (the configuration cannot pin them)."""
    finished_run(lab.layout, "finetune-other", subject_id="someone-else")
    approve_self_consent(
        lab.layout,
        acknowledgement=True,
        purposes=("private-assistant-speech",),
        execution_boundaries=("home-host",),
        expires_at=NOW + timedelta(days=30),
        subject_id="someone-else",
        consent_id="someone-else-v1",
        now=NOW - timedelta(days=1),
    )
    designate(
        lab.layout,
        run_id="finetune-other",
        designated_by="Cole",
        now=NOW,
        consent_id="someone-else-v1",
        subject_id="someone-else",
    )


def build_lab(tmp_path: Path, *, consent: bool = True, release: bool = True, **config: Any) -> Lab:
    root = tmp_path.resolve()
    layout = AssetLayout.initialize(root / "lab")
    run = finished_run(layout)
    digest = approve_consent(layout) if consent else ""
    lab = Lab(root, layout, run, speech_config(root, layout, **config), digest)
    if release:
        lab.designate()
    return lab


@dataclass
class SpeechRig:
    lab: Lab
    server: ApiServer
    routes: SpeechRoutes
    clock: list[datetime]

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])


def start_speech_rig(lab: Lab) -> Iterator[SpeechRig]:
    jwks_path = lab.root / "jwks.json"
    jwks_path.write_text(json.dumps(jwks()), encoding="utf-8")
    clock = [NOW]
    routes = SpeechRoutes(
        lab.config,
        TokenVerifier(caller_policy(lab.config, str(jwks_path))),
        clock=lambda: clock[0],
    )
    staging_root = AssetLayout.initialize(lab.root / "server-lab")
    server = ApiServer(
        ("127.0.0.1", 0),
        staging=IntakeStaging(staging_root, min_free_bytes=0),
        verifier=TokenVerifier(desktop_policy(jwks_path)),
        meta=ApiMeta(server_build="test-build", minimum_client_version="0.1.0"),
        log=False,
        routes=(routes,),
        self_authenticating=(PREFIX,),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SpeechRig(lab, server, routes, clock)
    finally:
        server.shutdown()
        server.server_close()


__all__ = ["KID", "NOW", "SUBJECT"]
