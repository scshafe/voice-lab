"""Uploading to the fake intake: resumable chunks, fail-closed finalize, retries, and the door."""

from __future__ import annotations

import time

import httpx
import pytest
from helpers import new_ritual, record_segment, record_session

from voice_lab_desktop.auth import Session, SignInRequired
from voice_lab_desktop.tokens import MemoryStore, Tokens
from voice_lab_desktop.measurement.upload import IntakeClient, upload_pending

CHUNK = 256 * 1024  # small chunks so every file takes several PUTs


@pytest.fixture
def recorded(backend, levels, pinned, sessions):
    return record_session(backend, levels, pinned, sessions, skip_free=True)


@pytest.fixture
def client(config, signed_in):
    http = httpx.Client(follow_redirects=False, timeout=30)
    yield IntakeClient(config.server_url, Session(config, signed_in, http), http, chunk_bytes=CHUNK)
    http.close()


def puts(intake):
    return [r for r in intake.requests if r.method == "PUT"]


def test_a_session_is_accepted_byte_for_byte(client, recorded, sessions, intake, config):
    summary = upload_pending(client, sessions, config.server_url)
    assert (summary.accepted, summary.pending, summary.failed) == (1, 0, 0)
    sid = recorded.session_id
    for entry in recorded.manifest["files"]:
        assert (intake.measurement / sid / entry["name"]).read_bytes() == (recorded.session_dir / entry["name"]).read_bytes()
    assert intake.accepted[sid]["capture"]["inputVolume"] == 63
    assert sessions.sessions()[0].upload_state == "accepted" and sessions.pending() == []
    ranges = [r.headers["content-range"] for r in puts(intake)]
    assert ranges[0] == f"bytes 0-{CHUNK - 1}/{recorded.manifest['files'][0]['sizeBytes']}" and len(ranges) > len(recorded.manifest["files"])
    assert all("cookie" not in r.headers for r in intake.requests)
    assert upload_pending(client, sessions, config.server_url).accepted == 0  # nothing left


def test_an_interrupted_upload_resumes_where_the_server_stopped(client, recorded, sessions, intake, config):
    intake.drop_after = 1000
    first = upload_pending(client, sessions, config.server_url)
    assert first.pending == 1 and "did not answer" in first.last_error
    assert sessions.pending()[0].upload["attempts"] == 1
    # The first chunk (start 0) was dropped: the server kept nothing of it. Interrupt a later chunk too.
    sent_before = len(puts(intake))
    intake.drop_after, intake.drop_min_start = 5000, 1
    upload_pending(client, sessions, config.server_url)
    resumed_from = [int(r.headers["content-range"].split()[1].split("-")[0]) for r in puts(intake)[sent_before:]]
    assert resumed_from[0] == 0 and resumed_from[1] == CHUNK
    before = len(puts(intake))
    third = upload_pending(client, sessions, config.server_url)
    assert third.accepted == 1
    restart = int(puts(intake)[before].headers["content-range"].split()[1].split("-")[0])
    assert restart == CHUNK + 5000  # from the server's receivedBytes, not from zero
    assert sessions.sessions()[0].upload_state == "accepted"


def test_a_corrupted_chunk_is_rejected_and_the_file_resent(client, recorded, sessions, intake, config):
    intake.corrupt_next_put = True
    small = IntakeClient(client.base.removesuffix("/api/intake/sessions"), client.session, client.http, chunk_bytes=50_000_000)
    summary = upload_pending(small, sessions, config.server_url)
    assert summary.accepted == 1
    assert [r.error for r in puts(intake)][0] == "checksum_mismatch"


def test_a_package_changed_on_disk_is_never_uploaded(client, recorded, sessions, intake, config):
    path = recorded.session_dir / recorded.manifest["files"][2]["name"]
    data = bytearray(path.read_bytes())
    data[-1] ^= 0xFF
    path.write_bytes(bytes(data))
    summary = upload_pending(client, sessions, config.server_url)
    assert summary.failed == 1 and "changed on disk" in summary.last_error
    assert intake.requests == []  # not even the manifest
    assert sessions.sessions()[0].upload_state == "failed"


def test_a_manifest_conflict_is_a_refusal(client, recorded, sessions, intake, config):
    intake.manifests[recorded.session_id] = {"sessionId": recorded.session_id, "different": True}
    summary = upload_pending(client, sessions, config.server_url)
    assert summary.failed == 1 and "session_manifest_conflict" in summary.last_error


def test_the_server_down_means_retry_later(client, recorded, sessions, intake, config):
    intake.unavailable = 1
    first = upload_pending(client, sessions, config.server_url)
    assert first.pending == 1 and "HTTP 503" in first.last_error
    assert upload_pending(client, sessions, config.server_url).accepted == 1


def test_an_already_accepted_session_needs_no_files(client, recorded, sessions, intake, config):
    upload_pending(client, sessions, config.server_url)
    sessions.retry_failed(sessions.sessions()[0].path)  # pretend we lost the "accepted" mark
    before = len(puts(intake))
    assert upload_pending(client, sessions, config.server_url).accepted == 1
    assert len(puts(intake)) == before


def test_a_refused_token_is_refreshed_once(config, issuer, intake, recorded, sessions):
    now = time.time()
    store = MemoryStore(Tokens("a.b.c", "a.b.c", now + 3600, "rt-1"))
    issuer.refresh_tokens["rt-1"] = True
    with httpx.Client(follow_redirects=False) as http:
        client = IntakeClient(config.server_url, Session(config, store, http), http)
        assert upload_pending(client, sessions, config.server_url).accepted == 1
    assert store.tokens.access_token != "a.b.c"
    assert [r.status for r in intake.requests][:2] == [401, 201]


def test_the_door_asking_for_a_browser_is_sign_in_required_and_not_followed(config, intake, recorded, sessions):
    intake.door_redirects = True
    store = MemoryStore(Tokens("x.y.z", "x.y.z", time.time() + 3600, None))
    with httpx.Client(follow_redirects=False) as http:
        client = IntakeClient(config.server_url, Session(config, store, http), http)
        with pytest.raises(SignInRequired):
            upload_pending(client, sessions, config.server_url)
    assert all(not r.path.startswith("/oauth2") for r in intake.requests)
    assert sessions.pending()[0].upload["lastError"] == "sign-in required"


def test_aborted_sessions_are_never_uploaded(client, backend, levels, pinned, sessions, intake, config):
    ritual = new_ritual(backend, levels, pinned, sessions)
    ritual.begin()
    record_segment(ritual)
    ritual.abort("stopped by you")
    assert upload_pending(client, sessions, config.server_url).accepted == 0
    assert intake.requests == []


def test_prune_accepted(client, recorded, sessions, config):
    upload_pending(client, sessions, config.server_url)
    assert sessions.prune_accepted() == 1 and not recorded.session_dir.exists()


def test_finalize_and_the_last_chunk_wait_for_the_servers_re_hash(client, recorded, sessions, config, monkeypatch):
    from voice_lab_desktop import upload

    seen: list[tuple[str, float]] = []
    real = client.http.request

    def spy(method, url, **kwargs):
        seen.append((url.rsplit("/", 1)[-1] if method == "POST" else method, kwargs["timeout"].read))
        return real(method, url, **kwargs)

    monkeypatch.setattr(client.http, "request", spy)
    upload_pending(client, sessions, config.server_url)
    assert ("finalize", upload.SLOW_TIMEOUT.read) in seen
    put_timeouts = [t for what, t in seen if what == "PUT"]
    assert set(put_timeouts) == {upload.TIMEOUT.read, upload.SLOW_TIMEOUT.read}
