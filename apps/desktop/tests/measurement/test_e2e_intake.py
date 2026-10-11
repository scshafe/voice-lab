"""End to end against the real voice-journey intake (src/intake.mjs inside src/corpus-browser.mjs).

Runs when a voice-journey checkout is found (``$VOICE_JOURNEY_CHECKOUT``, else
``../voice-journey`` next to this repository) and ``node`` >= 22 is on PATH;
skipped otherwise. The checkout is only read: the server runs with a temporary
``VOICE_JOURNEY_DATA``, and the test checks the checkout's git status is
unchanged. A small fake door in front of it does what oauth2-proxy does for a
bearer token: checks it, drops client-sent identity headers and sets
``X-Forwarded-User`` / ``X-Forwarded-Email``.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from helpers import record_session

from voice_lab_desktop.auth import Session
from voice_lab_desktop.measurement.upload import IntakeClient, upload_pending

REPO = Path(__file__).resolve().parents[1]


def checkout() -> Path | None:
    candidates = [os.environ.get("VOICE_JOURNEY_CHECKOUT"), str(REPO.parent / "voice-journey")]
    for c in candidates:
        if c and (Path(c) / "src" / "intake.mjs").is_file() and (Path(c) / "manifests" / "chains.json").is_file():
            return Path(c)
    return None


def node_ok() -> bool:
    node = shutil.which("node")
    if not node:
        return False
    out = subprocess.run([node, "--version"], capture_output=True, text=True).stdout.strip()
    return out.startswith("v") and int(out[1:].split(".")[0]) >= 22


CHECKOUT = checkout()
pytestmark = pytest.mark.skipif(CHECKOUT is None or not node_ok(), reason="no voice-journey checkout (VOICE_JOURNEY_CHECKOUT) or node >= 22")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def git_status(path: Path) -> str | None:
    if not (path / ".git").exists() or not shutil.which("git"):
        return None
    return subprocess.run(["git", "-C", str(path), "status", "--porcelain"], capture_output=True, text=True).stdout


class FakeDoor:
    def __init__(self, upstream_port: int, valid_tokens: set[str]) -> None:
        self.upstream_port = upstream_port
        self.valid_tokens = valid_tokens
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.05), daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _handler(self):
        door = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                return

            def _proxy(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                auth = self.headers.get("Authorization", "")
                if not auth.startswith("Bearer ") or auth[7:] not in door.valid_tokens:
                    raw = b'{"error":"unauthorized"}'
                    self.send_response(401)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                headers = {k: v for k, v in self.headers.items()
                           if k.lower() in ("content-type", "content-range", "content-length", "accept")}
                headers["X-Forwarded-User"] = "owner"
                headers["X-Forwarded-Email"] = "owner@example.test"
                conn = http.client.HTTPConnection("127.0.0.1", door.upstream_port, timeout=60)
                conn.request(self.command, self.path, body=body, headers=headers)
                answer = conn.getresponse()
                data = answer.read()
                self.send_response(answer.status)
                self.send_header("Content-Type", answer.getheader("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                conn.close()

            do_GET = do_POST = do_PUT = _proxy  # noqa: N815

        return Handler


@pytest.fixture
def server(tmp_path, issuer, intake):
    before = git_status(CHECKOUT)
    data = tmp_path / "vj-data"
    port = free_port()
    env = {**os.environ, "VOICE_JOURNEY_DATA": str(data), "VOICE_JOURNEY_CHAINS": str(CHECKOUT / "manifests" / "chains.json")}
    for var in ("VOICE_JOURNEY_CONTAINER", "VOICE_JOURNEY_WATCH", "VOICE_JOURNEY_NTFY", "VOICE_JOURNEY_PLAYBACK_APPROVAL"):
        env.pop(var, None)
    proc = subprocess.Popen(["node", "src/corpus-browser.mjs", "--host", "127.0.0.1", "--port", str(port)], cwd=CHECKOUT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if proc.poll() is not None or time.monotonic() > deadline:
                pytest.fail(f"voice-journey did not start: {proc.stderr.read() if proc.stderr else ''}")
            time.sleep(0.1)
        door = FakeDoor(port, intake.valid_tokens)  # the issuer's tokens, as the door's JWT check would accept
        yield door, port, data
        door.stop()
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    assert git_status(CHECKOUT) == before, "the voice-journey checkout changed"


def test_the_real_intake_accepts_a_recorded_session(server, config, signed_in, backend, levels, pinned, sessions):
    door, port, data = server
    ritual = record_session(backend, levels, pinned, sessions, skip_free=True)
    with httpx.Client(follow_redirects=False, timeout=60) as http:
        client = IntakeClient(door.url, Session(config, signed_in, http), http, chunk_bytes=300_000)
        summary = upload_pending(client, sessions, door.url)
        assert (summary.accepted, summary.failed, summary.pending) == (1, 0, 0), summary.last_error
        assert client.status(ritual.session_id).body["state"] == "accepted"
    published = data / "corpus" / "measurement" / ritual.session_id
    record = json.loads((published / "session.json").read_text())
    assert record["schemaVersion"] == "voice-journey.intake-session.v1" and record["chainId"] == "umik1"
    assert record["intake"]["acceptedBy"] == "owner"
    assert record["capture"]["inputVolume"] == 63 and record["capture"]["sampleRate"] == 48000
    assert [s["key"] for s in record["segments"]] == [s["key"] for s in ritual.manifest["segments"]]
    for entry in ritual.manifest["files"]:
        assert (published / entry["name"]).read_bytes() == (ritual.session_dir / entry["name"]).read_bytes()
    assert sessions.sessions()[0].upload_state == "accepted"


def test_the_real_intake_is_fail_closed(server, config, signed_in, backend, levels, pinned, sessions):
    door, port, data = server
    ritual = record_session(backend, levels, pinned, sessions, skip_free=True)
    manifest_bytes = (ritual.session_dir / "session.json").read_bytes()
    first = ritual.manifest["files"][0]
    with httpx.Client(follow_redirects=False, timeout=60) as http:
        assert http.post(f"http://127.0.0.1:{port}/api/intake/sessions", content=manifest_bytes).json()["error"] == "identity_required"
        assert http.post(f"{door.url}/api/intake/sessions", content=manifest_bytes).status_code == 401
        client = IntakeClient(door.url, Session(config, signed_in, http), http)
        opened = client.open(manifest_bytes)
        assert opened.status == 201 and opened.body["state"] == "open"
        bad = bytearray((ritual.session_dir / first["name"]).read_bytes())
        bad[-1] ^= 0xFF
        answer = client._send("PUT", f"{client.base}/{ritual.session_id}/files/{first['name']}", content=bytes(bad),
                              headers={"Content-Range": f"bytes 0-{len(bad) - 1}/{len(bad)}", "Content-Type": "application/octet-stream"})
        assert (answer.status, answer.body["error"]) == (422, "checksum_mismatch")
        final = client.finalize(ritual.session_id)
        assert (final.status, final.body["error"]) == (409, "incomplete")
        assert not (data / "corpus" / "measurement" / ritual.session_id).exists()
        # A different manifest for the same session is a conflict; the client's normal path then completes it.
        other = json.loads(manifest_bytes)
        other["capture"]["distanceCm"] = 36
        conflict = client._send("POST", client.base, content=json.dumps(other).encode(), headers={"Content-Type": "application/json"})
        assert (conflict.status, conflict.body["error"]) == (409, "session_manifest_conflict")
        assert upload_pending(client, sessions, door.url).accepted == 1
    assert (data / "corpus" / "measurement" / ritual.session_id / first["name"]).exists()
