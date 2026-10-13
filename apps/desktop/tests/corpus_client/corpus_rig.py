"""The PC as the corpus client meets it in tests, and synthetic takes. Nothing real.

``PC`` is the backend's own ``ApiServer`` on 127.0.0.1 with the intake staging and the corpus
routes over a synthetic lab whose catalog is seeded (``corpus_take_fixtures.seed_catalog``:
cole-corpus version 1, pilot-v2 without room tones), behind the backend's test token
verifier. ``stop`` and ``start`` take it down and bring it up again (on a new port, as a
restarted app would find it in its settings). ``publish`` is one pass of its intake
publisher, the host unit, run in-process: the client's waits call it between their polls.

``client`` makes a ``CorpusClient`` over a data dir as the app would, with this machine's
library holding the standing home-host approval (``home_host_fixtures``) and the loopback
upload client. ``record`` saves a take as the capture worker leaves it: a generated tone and
worker evidence bound to it (``corpus_take_fixtures``), not simulated unless asked, so the PC
accepts it.
"""

from __future__ import annotations

import http.client
import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from corpus_take_fixtures import NOW, evidence_for, seed_catalog, write_tone
from home_host_fixtures import ENDPOINT, bind_approval, jwks, make_approval, mint, policy
from voice_lab_desktop.api import ApiClient
from voice_lab_desktop.auth import Session
from voice_lab_desktop.capture.evidence import write_evidence
from voice_lab_desktop.capture.types import EVIDENCE_NAME, ORIGINAL_NAME, CapturedTake
from voice_lab_desktop.config import Config
from voice_lab_desktop.corpus.client import CorpusClient
from voice_lab_desktop.corpus.spool import SpooledTake
from voice_lab_desktop.corpus.sync import TakeUploader
from voice_lab_desktop.net import new_http
from voice_lab_desktop.tasks import Tasks
from voice_lab_desktop.tokens import MemoryStore, Tokens

from cole_voice_lab.api.auth import TokenVerifier
from cole_voice_lab.api.contracts import ApiMeta
from cole_voice_lab.api.corpus_contracts import CorpusLibrary
from cole_voice_lab.api.corpus_routes import CorpusRoutes
from cole_voice_lab.api.server import ApiServer
from cole_voice_lab.api.staging import IntakeStaging
from cole_voice_lab.canonical import sha256_file
from cole_voice_lab.corpus_takes import TakeClient
from cole_voice_lab.intake_client import HomeHostClient
from cole_voice_lab.intake_publisher import publish_pending
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_contracts import CatalogReference, CatalogSnapshot

#: The server the settings name: the approved home host (the uploads reach it on loopback).
SERVER_URL = f"https://{ENDPOINT}"
PLAYBACK_APPROVAL = "synthetic playback approval for the corpus client's tests"
TAKE_CLIENT = TakeClient(name="voice-lab-desktop", version="0.0.0-test", platform="Linux test")
DEVICE = "Umik-1  Gain: 18dB [ALSA]"


@dataclass
class PC:
    """The PC's lab and, while it is up, its API."""

    layout: AssetLayout
    work: Path
    catalog: tuple[CatalogReference, CatalogSnapshot] | Any
    server: ApiServer | None = None
    last_port: int = 0
    _thread: threading.Thread | None = field(default=None, repr=False)

    @classmethod
    def create(cls, work: Path, *, seeded: bool = True) -> PC:
        """A PC whose catalog is seeded (``seeded``; else none is active there yet)."""
        work.mkdir(parents=True, exist_ok=True)
        layout = AssetLayout.initialize(work / "lab")
        (work / "jwks.json").write_text(json.dumps(jwks()), encoding="utf-8")
        return cls(layout, work, seed_catalog(layout) if seeded else None)

    def start(self) -> PC:
        assert self.server is None
        self.server = ApiServer(
            ("127.0.0.1", 0),
            staging=IntakeStaging(self.layout, min_free_bytes=0),
            verifier=TokenVerifier(policy(self.work / "jwks.json")),
            meta=ApiMeta(server_build="test-build", minimum_client_version="0.1.0"),
            log=False,
            routes=(CorpusRoutes(self.layout, playback_approval=PLAYBACK_APPROVAL),),
        )
        self.last_port = int(self.server.server_address[1])
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    @property
    def port(self) -> int:
        """The port it answers on (the last one while it is down: nothing answers there)."""
        return self.last_port

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def publish(self, _seconds: float = 0.0) -> None:
        """One pass of the PC's intake publisher (also the client's sleep between polls)."""
        publish_pending(self.layout, min_free_bytes=0)

    def send(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, Any]]:
        """Another client's request (its own token), answered as JSON."""
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        payload = None if body is None else json.dumps(body).encode()
        headers = {"Authorization": f"Bearer {mint()}"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, json.loads(data) if data else {}

    def library(self) -> CorpusLibrary:
        status, document = self.send("GET", "/api/v1/corpus/library")
        assert status == 200, document
        return CorpusLibrary.model_validate_json(json.dumps(document))

    def raw_packages(self) -> list[Path]:
        raw = self.layout.root / "raw"
        return sorted(path for path in raw.iterdir() if path.is_dir())


def bind_library(data_dir: Path) -> AssetLayout:
    """This machine's library (``<data dir>/library``) with the standing approval bound."""
    library = AssetLayout.initialize(data_dir / "library")
    bind_approval(library, make_approval(classes=("capture", "measurement")))
    return library


def signed_in(server_url: str, data_dir: Path) -> Session:
    config = Config(server_url=server_url, data_dir=data_dir).validated()
    tokens = Tokens(mint(), mint({"email": "owner@example.test"}), time.time() + 3600, None)
    return Session(config, MemoryStore(tokens), new_http())


class Clock:
    """The client's clock: the real time, moved forward by ``advance`` (backoffs pass)."""

    def __init__(self) -> None:
        self.offset = timedelta()

    def __call__(self) -> datetime:
        from cole_voice_lab.models import utc_now

        return utc_now() + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += timedelta(seconds=seconds)


def client(
    data_dir: Path,
    pc: PC,
    *,
    enabled: bool | Callable[[], bool] = True,
    tasks: Tasks | None = None,
    upload_wait: float = 30.0,
    request_wait: float = 10.0,
    publish_while_waiting: bool = True,
    clock: Clock | None = None,
) -> CorpusClient:
    """A ``CorpusClient`` over ``data_dir`` as the app makes it, toward ``pc``."""
    session = signed_in(pc.url, data_dir)

    def api() -> ApiClient:
        return ApiClient(pc.url, session, session.http)

    def loopback(_approval: Any, bearer: Callable[[], str]) -> HomeHostClient:
        return HomeHostClient.loopback_for_tests(pc.port, bearer)

    wait = pc.publish if publish_while_waiting else (lambda _seconds: None)
    the_clock = clock or Clock()

    def uploader() -> TakeUploader:
        return TakeUploader(
            library_dir=data_dir / "library",
            server_url=SERVER_URL,
            bearer=lambda: mint(),
            client_factory=loopback,
            wait_seconds=upload_wait,
            sleep=wait,
        )

    made = CorpusClient(
        data_dir / "corpus",
        api=api,
        uploader=uploader,
        enabled=enabled if callable(enabled) else (lambda: enabled),
        tasks=tasks,
        clock=the_clock,
        sleep=wait,
        take_client=TAKE_CLIENT,
        capture_host="synthetic-host",
    )
    made.engine.request_wait_seconds = request_wait
    return made


def capture(
    directory: Path,
    *,
    seed: int,
    seconds: float = 0.25,
    simulated: bool = False,
    device_name: str = "Umik-1  Gain: 18dB",
) -> CapturedTake:
    """What the capture worker leaves in ``directory`` and reports: a generated tone as
    ``original.wav`` (0600) and its evidence, checked as the parent checks them."""
    wav = directory / ORIGINAL_NAME
    write_tone(wav, seconds=seconds, seed=seed, frequency=180.0 + 7 * seed)
    os.chmod(wav, 0o600)
    requested_at = NOW + timedelta(seconds=seed)
    evidence = evidence_for(
        wav, simulated=simulated, started_at=requested_at, device_name=device_name
    )
    write_evidence(directory / EVIDENCE_NAME, evidence)
    sha256, size = sha256_file(wav)
    return CapturedTake(
        path=wav,
        evidence_path=directory / EVIDENCE_NAME,
        sha256=sha256,
        size_bytes=size,
        frames=evidence.frames,
        requested_at=requested_at,
        device=DEVICE,
    )


def record(corpus: CorpusClient, prompt_id: str, *, seed: int, **options: Any) -> SpooledTake:
    """A take of ``prompt_id`` recorded and saved as the prompt studio would."""
    pending = corpus.begin_take(prompt_id)
    return corpus.save_take(pending, capture(pending.directory, seed=seed, **options))


class Scripted:
    """The app's API client with some of its methods replaced (``methods``)."""

    def __init__(self, api: Any, **methods: Any) -> None:
        self._api = api
        self._methods = methods

    def __getattr__(self, name: str) -> Any:
        if name in self._methods:
            return self._methods[name]
        return getattr(self._api, name)


def raising(error: BaseException) -> Callable[..., Any]:
    """A stand-in for any method: it raises ``error``."""

    def method(*_args: Any, **_kwargs: Any) -> Any:
        raise error

    return method


def uploaded_take(corpus: CorpusClient, pc: PC, seed: int) -> SpooledTake:
    """A take recorded and accepted by the PC (the catalog fetched first if need be)."""
    corpus.run_pass()
    take = record(corpus, pc.catalog[1].suite.prompts[seed % 5].prompt_id, seed=seed)
    assert corpus.run_pass().uploaded == 1
    return corpus.spool.take(take.take_id)
