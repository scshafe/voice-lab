"""Listener lifecycle failures clean up native-vault fixtures without opening sockets."""

from __future__ import annotations

import secrets
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from recording_fixtures import REPOSITORY_ROOT

sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

import native_vault_fixture as fixture_module  # noqa: E402

from cole_voice_lab.storage_contracts import VaultNamespaceConfig, VaultServerConfig  # noqa: E402


class SocketFreeListenerRig:
    """Instrument server/thread lifetimes; shutdown/join reject an unstarted thread."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
        self.fail_start = False
        self.fail_cleanup: str | None = None
        self.servers: list[Any] = []
        self.threads: list[Any] = []
        rig = self

        class Server:
            server_address = ("127.0.0.1", 43210)

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                self.shutdown_calls = 0
                self.close_calls = 0
                self.index = len(rig.servers)
                rig.servers.append(self)

            def serve_forever(self) -> None:
                raise AssertionError("the socket-free test must never run a serving loop")

            def shutdown(self) -> None:
                assert rig.threads[self.index].started, "shutdown would deadlock before start"
                self.shutdown_calls += 1
                if rig.fail_cleanup == "shutdown":
                    raise RuntimeError("injected shutdown failure")

            def server_close(self) -> None:
                self.close_calls += 1
                if rig.fail_cleanup == "close":
                    raise RuntimeError("injected close failure")

        class Thread:
            def __init__(self, **kwargs: Any) -> None:
                self.started = False
                self.join_calls = 0
                rig.threads.append(self)

            def start(self) -> None:
                if rig.fail_start:
                    raise RuntimeError("injected thread start failure")
                self.started = True

            def join(self, *, timeout: float) -> None:
                assert self.started, "an unstarted thread cannot be joined"
                self.join_calls += 1
                if rig.fail_cleanup == "join":
                    raise RuntimeError("injected join failure")

            def is_alive(self) -> bool:
                return False

        monkeypatch.setattr(fixture_module, "VaultServer", Server)
        monkeypatch.setattr(fixture_module, "threading", SimpleNamespace(Thread=Thread))
        config = VaultServerConfig(
            namespaces=(
                VaultNamespaceConfig(
                    name="test",
                    read_token_sha256="1" * 64,
                    write_token_sha256="2" * 64,
                    quota_bytes=1024,
                ),
            )
        )
        self.listener = fixture_module.NativeVaultListener(
            root, config, key=secrets.token_hex(32), token=secrets.token_hex(32)
        )


@pytest.mark.parametrize("restart", [False, True])
def test_failed_initial_or_restarted_thread_closes_without_shutdown_or_join_and_removes_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restart: bool
) -> None:
    rig = SocketFreeListenerRig(monkeypatch, tmp_path)
    if restart:
        rig.listener.start(provision=True)
        rig.listener.stop()
        assert rig.listener.credentials.is_file(), "outage keeps credentials for recovery"
    rig.fail_start = True
    with pytest.raises(RuntimeError, match="injected thread start failure"):
        rig.listener.start(provision=not restart)

    rig.listener.close()

    assert rig.servers[-1].shutdown_calls == 0
    assert rig.servers[-1].close_calls == 1
    assert rig.threads[-1].join_calls == 0
    assert not rig.listener.credentials.exists()


@pytest.mark.parametrize("failure", ["shutdown", "close", "join"])
def test_listener_cleanup_failure_still_deletes_credentials_and_attempts_later_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    rig = SocketFreeListenerRig(monkeypatch, tmp_path)
    rig.listener.start(provision=True)
    assert rig.listener.credentials.is_file()
    rig.fail_cleanup = failure

    with pytest.raises(RuntimeError, match=f"injected {failure} failure"):
        rig.listener.close()

    assert rig.servers[0].shutdown_calls == 1
    assert rig.servers[0].close_calls == 1
    assert rig.threads[0].join_calls == 1
    assert not rig.listener.credentials.exists()


def test_second_start_refuses_without_losing_the_live_listener_and_repeated_close_is_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = SocketFreeListenerRig(monkeypatch, tmp_path)
    rig.listener.start(provision=True)
    with pytest.raises(RuntimeError, match="already constructed"):
        rig.listener.start(provision=False)
    rig.listener.close()
    rig.listener.close()

    assert len(rig.servers) == 1 and rig.servers[0].close_calls == 1
    assert not rig.listener.credentials.exists()
