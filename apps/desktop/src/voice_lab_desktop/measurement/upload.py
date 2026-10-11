"""Uploading a session to voice-journey's intake API (README "Intake API").

1. ``POST /api/intake/sessions`` with the manifest (``session.json`` as written);
   ``201`` opens it, ``200`` resumes it (per-file ``receivedBytes``) or says
   it is already ``accepted``.
2. ``PUT /api/intake/sessions/<id>/files/<name>`` in chunks, each with
   ``Content-Range: bytes <start>-<end>/<size>``, from the server's
   ``receivedBytes``; ``409 range_not_contiguous`` tells where to continue.
3. ``POST /api/intake/sessions/<id>/finalize``: the server re-verifies every
   size and sha256 and publishes the session, or nothing.

The bearer token goes to the door; ``401`` refreshes once and retries.
Redirects are never followed (a redirect means the door wants a browser
sign-in, i.e. it did not accept the token). Before anything is sent the
local files are re-hashed against the manifest: a package that changed on
disk is never uploaded. Raw audio goes only to the configured server.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import httpx

from ..auth import Session, SignInRequired
from ..net import new_http as _new_http
from .package import PackageError, read_session_json, validate_manifest, verify_files
from .store import LocalSession, SessionStore

CHUNK_BYTES = 8 * 1024 * 1024
TIMEOUT = httpx.Timeout(60.0, connect=15.0)
#: A PUT that completes a file, and finalize, wait while the server re-hashes (up to 4 GiB on a
#: 2-core laptop); the door allows the upstream 300 s (infra stacks/voice-journey).
SLOW_TIMEOUT = httpx.Timeout(300.0, connect=15.0)
#: Rounds of (upload what is missing, finalize) before giving up for this launch.
MAX_ROUNDS = 3

Progress = Callable[[str, int, int], None]  # (file name, bytes sent, file size)


class UploadError(Exception):
    """Try again later (network, server busy or down). The message is safe to show."""


class UploadRefused(UploadError):
    """The server will not take this package as it is; retrying will not help."""


@dataclass
class Answer:
    status: int
    body: dict


def new_http() -> httpx.Client:
    # No cookies, no redirects: the bearer is the only credential (net.py).
    return _new_http()


class IntakeClient:
    def __init__(self, server_url: str, session: Session, http: httpx.Client, chunk_bytes: int = CHUNK_BYTES) -> None:
        self.base = server_url.rstrip("/") + "/api/intake/sessions"
        self.session = session
        self.http = http
        self.chunk_bytes = chunk_bytes

    def _send(self, method: str, url: str, *, content: bytes | None = None, headers: dict[str, str] | None = None,
              timeout: httpx.Timeout = TIMEOUT) -> Answer:
        bearer = self.session.bearer()
        for attempt in (1, 2):
            sent = {"Authorization": f"Bearer {bearer}", "Accept": "application/json", **(headers or {})}
            try:
                response = self.http.request(method, url, content=content, headers=sent, timeout=timeout)
            except httpx.HTTPError as exc:
                raise UploadError(f"the server did not answer ({type(exc).__name__})") from exc
            self.http.cookies.clear()
            if response.status_code == 401 and attempt == 1:
                bearer = self.session.after_unauthorized(bearer)
                continue
            break
        if response.is_redirect:
            raise SignInRequired("the server's door asked for a browser sign-in instead of accepting the token (is the door configured for this client?)")
        if response.status_code == 401:
            raise SignInRequired("the server refused the sign-in; sign in again")
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        if response.status_code >= 500 or response.status_code in (408, 429, 502, 503, 504):
            detail = f" {body['error']}" if body.get("error") else ""
            raise UploadError(f"the server is unavailable (HTTP {response.status_code}{detail})")
        return Answer(response.status_code, body)

    def open(self, manifest_bytes: bytes) -> Answer:
        answer = self._send("POST", self.base, content=manifest_bytes, headers={"Content-Type": "application/json"})
        if answer.status in (200, 201):
            return answer
        code = answer.body.get("error", f"HTTP {answer.status}")
        if answer.status in (409, 422, 413, 400):
            raise UploadRefused(f"the server refused the session manifest ({code})")
        raise UploadError(f"opening the session failed ({code})")

    def status(self, session_id: str) -> Answer:
        return self._send("GET", f"{self.base}/{quote(session_id)}")

    def put_file(self, session_id: str, path: Path, size: int, start: int, progress: Progress | None = None) -> None:
        """Send ``path`` from ``start`` in chunks until the server has it all."""
        url = f"{self.base}/{quote(session_id)}/files/{quote(path.name)}"
        offset = start
        mismatches = 0
        with open(path, "rb") as fh:
            while offset < size:
                fh.seek(offset)
                chunk = fh.read(min(self.chunk_bytes, size - offset))
                if not chunk:
                    raise UploadRefused(f"{path.name} is shorter on disk than its manifest says")
                end = offset + len(chunk) - 1
                answer = self._send("PUT", url, content=chunk, headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Range": f"bytes {offset}-{end}/{size}",
                }, timeout=SLOW_TIMEOUT if end + 1 == size else TIMEOUT)
                state = answer.body.get("state")
                if answer.status == 202 and state == "partial":
                    offset = int(answer.body.get("receivedBytes", end + 1))
                elif answer.status == 200 and state in ("received", "already_received"):
                    offset = size
                elif answer.status == 409 and answer.body.get("error") == "range_not_contiguous":
                    offset = int(answer.body.get("receivedBytes", 0))
                elif answer.status == 422 and answer.body.get("error") == "checksum_mismatch":
                    # The server dropped what it had; send the whole file once more, then give up.
                    mismatches += 1
                    if mismatches > 1:
                        raise UploadRefused(f"the server keeps rejecting {path.name}'s checksum")
                    offset = 0
                elif answer.status == 409 and answer.body.get("error") == "session_busy":
                    raise UploadError("the server is busy with this session; try again shortly")
                elif answer.status == 404 and answer.body.get("error") == "session_not_found":
                    raise UploadError("the server no longer holds the open session (purged); it is reopened next time")
                else:
                    raise UploadRefused(f"the server refused {path.name} ({answer.body.get('error', f'HTTP {answer.status}')})")
                if progress:
                    progress(path.name, offset, size)

    def finalize(self, session_id: str) -> Answer:
        return self._send("POST", f"{self.base}/{quote(session_id)}/finalize", content=b"", headers={"Content-Type": "application/json"},
                          timeout=SLOW_TIMEOUT)


def upload_session(client: IntakeClient, local: LocalSession, progress: Progress | None = None) -> str:
    """Upload one completed session until the server accepts it. Returns "accepted"."""
    manifest = read_session_json(local.path)
    if manifest.get("status") != "completed":
        raise UploadRefused("only completed sessions are uploaded")
    try:
        validate_manifest(manifest)
    except PackageError as exc:
        raise UploadRefused(f"the package is not acceptable ({exc})") from exc
    changed = verify_files(local.path, manifest)
    if changed:
        raise UploadRefused(f"{len(changed)} file(s) changed on disk since the session was recorded; not uploaded")
    body = (local.path / "session.json").read_bytes()  # sent exactly as written, every time
    session_id = manifest["sessionId"]
    sizes = {f["name"]: f["sizeBytes"] for f in manifest["files"]}
    answer = client.open(body)
    for _round in range(MAX_ROUNDS):
        if answer.body.get("state") == "accepted":
            return "accepted"
        for entry in answer.body.get("files", []):
            name = entry.get("name")
            if name not in sizes or entry.get("complete"):
                continue
            client.put_file(session_id, local.path / name, sizes[name], int(entry.get("receivedBytes", 0)), progress)
        final = client.finalize(session_id)
        if final.status == 200 and final.body.get("state") == "accepted":
            return "accepted"
        code = final.body.get("error", f"HTTP {final.status}")
        if final.status == 409 and code == "incomplete" or final.status == 422 and code == "checksum_mismatch":
            answer = client.status(session_id)  # the server dropped or misses files: send them again
            if answer.status != 200:
                raise UploadError(f"the session's status could not be read ({answer.body.get('error', answer.status)})")
            continue
        if final.status == 409 and code == "session_busy" or final.status == 404 and code == "session_not_found":
            raise UploadError(f"the session was not finalized ({code}); it is retried next time")
        raise UploadRefused(f"the server did not accept the session ({code})")
    raise UploadError("the session was not accepted after several rounds; it will be retried")


@dataclass
class UploadSummary:
    accepted: int = 0
    pending: int = 0
    failed: int = 0
    last_error: str | None = None


def upload_pending(client: IntakeClient, store: SessionStore, server: str, progress: Progress | None = None,
                   on_session: Callable[[str, str], None] | None = None,
                   on_start: Callable[[str], None] | None = None) -> UploadSummary:
    """Every pending session, oldest first. Logs and messages carry ids and counts only.

    ``on_start(session_id)`` is called as each session starts (the Uploads screen shows
    which one is in flight), ``on_session(session_id, state)`` as each ends."""
    summary = UploadSummary()
    for local in store.pending():
        if on_start:
            on_start(local.session_id)
        try:
            upload_session(client, local, progress)
        except SignInRequired:
            store.record_attempt(local.path, "sign-in required")
            raise
        except UploadRefused as exc:
            store.mark_failed(local.path, str(exc))
            summary.failed += 1
            summary.last_error = str(exc)
            if on_session:
                on_session(local.session_id, "failed")
            continue
        except (UploadError, PackageError, OSError) as exc:
            store.record_attempt(local.path, str(exc))
            summary.pending += 1
            summary.last_error = str(exc)
            if on_session:
                on_session(local.session_id, "pending")
            continue
        store.mark_accepted(local.path, server)
        summary.accepted += 1
        if on_session:
            on_session(local.session_id, "accepted")
    return summary
