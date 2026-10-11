"""The home host as the app uploads to it in tests: the backend's real ``/api/v1/intake``
(``ApiServer`` + ``IntakeStaging``) over a synthetic lab, its intake publisher run between
the client's polls, and this machine's library with a bound home-host approval covering
measurement sessions (the backend's ``home_host_fixtures``). The client is the loopback
variant of ``HomeHostClient``: no tailnet, no identity check; everything else is real."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from home_host_fixtures import ENDPOINT, bind_approval, jwks, make_approval, mint, policy

from cole_voice_lab.api.auth import TokenVerifier
from cole_voice_lab.api.contracts import ApiMeta
from cole_voice_lab.api.server import ApiServer
from cole_voice_lab.api.staging import IntakeStaging
from cole_voice_lab.home_host import HomeHostApproval
from cole_voice_lab.intake_client import HomeHostClient
from cole_voice_lab.intake_publisher import publish_pending
from cole_voice_lab.paths import AssetLayout

SERVER_URL = f"https://{ENDPOINT}"


@dataclass
class HomeRig:
    server: ApiServer
    server_layout: AssetLayout
    library: AssetLayout
    library_dir: Path

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def client_factory(
        self,
    ) -> Callable[[HomeHostApproval, Callable[[], str]], HomeHostClient]:
        def make(_approval: HomeHostApproval, bearer: Callable[[], str]) -> HomeHostClient:
            return HomeHostClient.loopback_for_tests(self.port, bearer)

        return make

    def publish(self, _seconds: float = 0.0) -> None:
        """The intake publisher's one pass (the PC's host unit); also the client's sleep."""
        publish_pending(self.server_layout, min_free_bytes=0)

    def accepted(self) -> list[Path]:
        raw = self.server_layout.root / "raw"
        return sorted(path for path in raw.iterdir() if path.is_dir())


@contextmanager
def home_rig(
    work: Path, library_dir: Path, *, classes: tuple[str, ...] = ("capture", "measurement")
) -> Iterator[HomeRig]:
    server_layout = AssetLayout.initialize(work / "pc" / "lab")
    library = AssetLayout.initialize(library_dir)
    bind_approval(library, make_approval(classes=classes))
    jwks_path = work / "jwks.json"
    jwks_path.write_text(json.dumps(jwks()), encoding="utf-8")
    server = ApiServer(
        ("127.0.0.1", 0),
        staging=IntakeStaging(server_layout, min_free_bytes=0),
        verifier=TokenVerifier(policy(jwks_path)),
        meta=ApiMeta(server_build="test-build", minimum_client_version="0.1.0"),
        log=False,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield HomeRig(server, server_layout, library, library_dir)
    finally:
        server.shutdown()
        server.server_close()


def bearer(claims: dict[str, Any] | None = None) -> Callable[[], str]:
    token = mint(claims)
    return lambda: token
