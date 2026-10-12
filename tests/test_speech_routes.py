"""``/api/v1/speech`` through a running server (speech/routes.py, docs/SPEECH_API.md §5, §6).

Tokens are signed by the throwaway issuer of ``home_host_fixtures``; the job runner is run in
the test with the fake backend; all audio is a generated tone.
"""

from __future__ import annotations

import http.client
import json
import socket
import struct
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from home_host_fixtures import mint
from speech_fixtures import (
    CALLER_AUDIENCE,
    HOOK,
    NOW,
    FakeBackend,
    OnePass,
    SpeechRig,
    build_lab,
    caller,
    caller_token,
    designate_another_subject,
    start_speech_rig,
)

from cole_voice_lab.api.contracts import REFUSAL_CODES
from cole_voice_lab.api.routing import RouteRefusal, RouteRequest
from cole_voice_lab.canonical import sha256_file
from cole_voice_lab.speech.contracts import GpuSlotHolder
from cole_voice_lab.speech.gpu import hold_slot
from cole_voice_lab.speech.routes import (
    MAX_EVENT_STREAMS,
    byte_range,
    caller_policy,
    last_event_id,
    match_caller,
)
from cole_voice_lab.speech.store import SpeechRefusal


def call(
    rig: SpeechRig,
    method: str,
    path: str,
    body: Any = None,
    *,
    token: str | None = "caller",
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", rig.port, timeout=10)
    sent = dict(headers or {})
    if token is not None:
        sent["Authorization"] = f"Bearer {caller_token() if token == 'caller' else token}"
    data = (
        None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    )
    connection.request(method, path, body=data, headers=sent)
    response = connection.getresponse()
    payload = response.read()
    connection.close()
    return response.status, dict(response.getheaders()), payload


def answer(
    rig: SpeechRig, method: str, path: str, body: Any = None, **options: Any
) -> tuple[int, Any]:
    status, _, payload = call(rig, method, path, body, **options)
    return status, json.loads(payload) if payload else {}


def run_jobs(rig: SpeechRig) -> None:
    OnePass(rig.lab.config, FakeBackend(), clock=lambda: rig.clock[0]).run_pending()


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[SpeechRig]:
    yield from start_speech_rig(build_lab(tmp_path))


def test_a_caller_submits_polls_and_fetches_its_audio(rig: SpeechRig) -> None:
    status, job = answer(
        rig,
        "POST",
        "/api/v1/speech",
        {"text": "Hello there.", "callback_url": HOOK + "/t1", "client_ref": "t1"},
    )
    assert status == 202 and job["state"] == "queued" and job["client_ref"] == "t1"
    assert job["links"]["self"] == f"/api/v1/speech/{job['id']}"
    assert answer(rig, "GET", job["links"]["self"])[1]["state"] == "queued"
    status, refusal = answer(rig, "GET", job["links"]["audio"])
    assert (status, refusal["error"]) == (409, "audio_not_ready")
    run_jobs(rig)
    status, done = answer(rig, "GET", job["links"]["self"])
    assert done["state"] == "succeeded" and done["render_ms"] == 0 and done["queue_ms"] == 0
    status, headers, audio = call(rig, "GET", job["links"]["audio"])
    assert status == 200 and headers["Content-Type"] == "audio/wav"
    assert headers["ETag"] == f'"{done["audio"]["sha256"]}"' and headers["Accept-Ranges"] == "bytes"
    stored = Path(rig.lab.config.state_root) / "jobs" / job["id"] / "speech.wav"
    assert audio == stored.read_bytes() and done["audio"]["sha256"] == sha256_file(stored)[0]
    status, headers, part = call(
        rig, "GET", job["links"]["audio"], headers={"Range": "bytes=10-19"}
    )
    assert (status, part, headers["Content-Range"]) == (
        206,
        audio[10:20],
        f"bytes 10-19/{len(audio)}",
    )
    status, _, tail = call(rig, "GET", job["links"]["audio"], headers={"Range": "bytes=-4"})
    assert (status, tail) == (206, audio[-4:])
    status, _, rest = call(rig, "GET", job["links"]["audio"], headers={"Range": "bytes=40-"})
    assert (status, rest) == (206, audio[40:])
    status, refusal = answer(rig, "GET", job["links"]["audio"], headers={"Range": "bytes=99999-"})
    assert (status, refusal["error"]) == (416, "range_not_satisfiable")
    status, headers, _ = call(rig, "GET", job["links"]["audio"], headers={"Range": "bytes=99999-"})
    assert headers["Content-Range"] == f"bytes */{len(audio)}"
    status, headers, whole = call(
        rig, "GET", job["links"]["audio"], headers={"Range": "bytes=0-1,4-5"}
    )
    assert (status, whole) == (200, audio) and "Content-Range" not in headers
    status, headers, empty = call(rig, "HEAD", job["links"]["audio"])
    assert status == 200 and empty == b"" and headers["Content-Length"] == str(len(audio))
    status, refusal = answer(rig, "POST", job["links"]["cancel"])
    assert (status, refusal["error"]) == (409, "already_finished")


def test_the_speech_routes_take_only_registered_callers(rig: SpeechRig) -> None:
    body = {"text": "Hello."}
    status, refusal = answer(rig, "POST", "/api/v1/speech", body, token=None)
    assert (status, refusal["error"]) == (401, "unauthorized")
    desktop = mint()  # Cole's desktop token: valid for the API, but no speech caller
    status, refusal = answer(rig, "POST", "/api/v1/speech", body, token=desktop)
    assert status == 401 and refusal["error"] == "unauthorized"
    status, refusal = answer(
        rig, "POST", "/api/v1/speech", body, token=caller_token(aud="voice-lab-desktop")
    )
    assert status == 401
    status, refusal = answer(rig, "POST", "/api/v1/speech", body, token=caller_token(exp=1))
    assert (status, refusal["error"]) == (401, "unauthorized")
    status, refusal = answer(rig, "GET", "/api/v1/meta", token=caller_token())
    assert (status, refusal["error"]) == (401, "unauthorized")  # a caller reaches nothing else
    status, meta = answer(rig, "GET", "/api/v1/meta", token=desktop)
    assert status == 200 and meta["server_build"] == "test-build"


def test_a_caller_never_sees_another_callers_job(tmp_path: Path) -> None:
    other = caller(
        caller_id="other", subject="client-other", webhook_urls=(), webhook_secret_file=None
    )
    lab = build_lab(tmp_path, callers=(caller(), other))
    for rig in start_speech_rig(lab):
        status, job = answer(rig, "POST", "/api/v1/speech", {"text": "Mine."})
        assert status == 202
        stranger = caller_token(sub="client-other")
        for path in (job["links"]["self"], job["links"]["audio"]):
            status, refusal = answer(rig, "GET", path, token=stranger)
            assert (status, refusal["error"]) == (404, "speech_not_found")
        status, refusal = answer(rig, "POST", job["links"]["cancel"], token=stranger)
        assert (status, refusal["error"]) == (404, "speech_not_found")
        status, refusal = answer(
            rig, "POST", "/api/v1/speech", {"text": "Mine.", "callback_url": HOOK}, token=stranger
        )
        assert (status, refusal["error"]) == (422, "callback_not_allowed")


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ({"text": ""}, 422, "invalid_request"),
        ({"text": "x" * 2001}, 422, "invalid_request"),
        ({"text": "bad​"}, 422, "invalid_request"),
        ({"text": "x", "voice": "base"}, 422, "invalid_request"),
        ({"text": "x", "format": "ogg-opus"}, 422, "unsupported_format"),
        ({"text": "x", "format": "mp3"}, 422, "invalid_request"),
        ({"text": "x", "callback_url": "https://evil.example/hook"}, 422, "callback_not_allowed"),
        (
            {"text": "x", "callback_url": "http://agent.example-tailnet.ts.net/hooks/voice-lab"},
            422,
            "callback_not_allowed",
        ),
        (
            {"text": "x", "callback_url": "https://agent.example-tailnet.ts.net/other"},
            422,
            "callback_not_allowed",
        ),
        ({"text": "x", "idempotency_key": "has space"}, 422, "invalid_request"),
    ],
)
def test_a_request_is_refused_with_a_stable_code(
    rig: SpeechRig, body: Any, status: int, code: str
) -> None:
    got, refusal = answer(rig, "POST", "/api/v1/speech", body)
    assert (got, refusal["error"]) == (status, code)
    assert code in REFUSAL_CODES


def test_a_shorter_caller_limit_and_a_non_json_body_are_refused(tmp_path: Path) -> None:
    lab = build_lab(tmp_path, callers=(caller(max_text_characters=5),))
    for rig in start_speech_rig(lab):
        assert (
            answer(rig, "POST", "/api/v1/speech", {"text": "123456"})[1]["error"]
            == "invalid_request"
        )
        assert answer(rig, "POST", "/api/v1/speech", b"{not json")[1]["error"] == "invalid_request"
        assert answer(rig, "POST", "/api/v1/speech", {"text": "12345"})[0] == 202


@pytest.mark.parametrize(
    ("method", "path", "status", "code"),
    [
        ("GET", "/api/v1/speech", 405, "method_not_allowed"),
        ("DELETE", "/api/v1/speech/sp-000000000000000000000000", 405, "method_not_allowed"),
        ("GET", "/api/v1/speech/sp-000000000000000000000000", 404, "speech_not_found"),
        ("GET", "/api/v1/speech/../meta", 404, "not_found"),
        ("GET", "/api/v1/speech/sp-000000000000000000000000/other", 404, "not_found"),
        ("GET", "/api/v1/speech/sp-000000000000000000000000/audio/x", 404, "not_found"),
        ("GET", "/api/v1/speechx", 401, "unauthorized"),
    ],
)
def test_unknown_paths_and_methods_under_the_prefix(
    rig: SpeechRig, method: str, path: str, status: int, code: str
) -> None:
    assert answer(rig, method, path) == (status, answer(rig, method, path)[1])
    got, refusal = answer(rig, method, path)
    assert (got, refusal["error"]) == (status, code)


def test_an_idempotent_replay_answers_the_same_job(rig: SpeechRig) -> None:
    body = {"text": "Once.", "idempotency_key": "reply-1"}
    first_status, first = answer(rig, "POST", "/api/v1/speech", body)
    again_status, again = answer(rig, "POST", "/api/v1/speech", body)
    assert (first_status, again_status, again["id"]) == (202, 200, first["id"])
    with hold_slot(
        Path(rig.lab.config.gpu_slot_root),
        GpuSlotHolder(kind="finetune", job_id="finetune-1", started_at=NOW),
        timeout_seconds=1,
    ):
        assert answer(rig, "POST", "/api/v1/speech", body)[0] == 200  # replays ignore the slot
    status, refusal = answer(rig, "POST", "/api/v1/speech", {**body, "text": "Twice."})
    assert (status, refusal["error"]) == (409, "idempotency_conflict")


def test_a_long_job_in_the_gpu_slot_refuses_speech_at_once(rig: SpeechRig) -> None:
    holder = GpuSlotHolder(
        kind="finetune",
        job_id="finetune-1",
        started_at=NOW,
        expected_end_at=NOW + timedelta(hours=2),
    )
    with hold_slot(Path(rig.lab.config.gpu_slot_root), holder, timeout_seconds=1):
        status, headers, payload = call(rig, "POST", "/api/v1/speech", {"text": "Now?"})
    assert (status, json.loads(payload)["error"]) == (503, "gpu_busy")
    assert headers["Retry-After"] == "7201"
    assert answer(rig, "POST", "/api/v1/speech", {"text": "Now?"})[0] == 202


def test_without_a_release_speech_is_unavailable_and_no_consent_is_needed(
    tmp_path: Path,
) -> None:
    lab = build_lab(tmp_path, release=False)
    for rig in start_speech_rig(lab):
        status, refusal = answer(rig, "POST", "/api/v1/speech", {"text": "Hi."})
        assert (status, refusal["error"]) == (503, "voice_release_unavailable")
        # Designating is all it takes: the lab holds no consent record (D-050).
        lab.designate()
        assert not (lab.layout.root / "provenance/consents").exists()
        status, job = answer(rig, "POST", "/api/v1/speech", {"text": "Hi."})
        assert (status, job["state"]) == (202, "queued")


def test_a_release_of_another_subject_is_unavailable(tmp_path: Path) -> None:
    lab = build_lab(tmp_path)
    designate_another_subject(lab)  # the configuration pins cole-self, in code
    for rig in start_speech_rig(lab):
        status, refusal = answer(rig, "POST", "/api/v1/speech", {"text": "Hi."})
        assert (status, refusal["error"]) == (503, "voice_release_unavailable")
        assert "cole-self" not in refusal["detail"]


def test_cancel_through_the_api(rig: SpeechRig) -> None:
    status, job = answer(rig, "POST", "/api/v1/speech", {"text": "Never mind."})
    status, cancelled = answer(rig, "POST", job["links"]["cancel"])
    assert (status, cancelled["state"]) == (202, "cancelled")
    status, refusal = answer(rig, "GET", job["links"]["audio"])
    assert (status, refusal["error"]) == (409, "audio_not_ready")


def test_expired_audio_answers_410(rig: SpeechRig) -> None:
    _, job = answer(rig, "POST", "/api/v1/speech", {"text": "Soon gone."})
    run_jobs(rig)
    rig.clock[0] = NOW + timedelta(days=8)
    run_jobs(rig)
    assert answer(rig, "GET", job["links"]["self"])[1]["state"] == "expired"
    status, refusal = answer(rig, "GET", job["links"]["audio"])
    assert (status, refusal["error"]) == (410, "audio_expired")


def test_byte_ranges() -> None:
    assert byte_range(None, 10) is None
    assert byte_range("bytes=0-", 10) == (0, 9)
    assert byte_range("bytes=2-100", 10) == (2, 9)
    assert byte_range("bytes=-100", 10) == (0, 9)
    # Several ranges, another unit or a malformed header: the whole file (RFC 9110).
    for header in ("bytes=0-1,3-4", "items=0-1", "bytes=-", "bytes=x-1", "bytes=١-٢"):
        assert byte_range(header, 10) is None
    for header in ("bytes=-0", "bytes=5-2", "bytes=10-"):
        with pytest.raises(SpeechRefusal) as refused:
            byte_range(header, 10)
        assert refused.value.headers == (("Content-Range", "bytes */10"),)


def test_last_event_id_is_ascii_digits_only() -> None:
    assert [last_event_id(value) for value in (None, "", "3", "0012", "12345", "²", "١", "-1")] == [
        0,
        0,
        3,
        12,
        0,
        0,
        0,
        0,
    ]


def test_the_caller_policy_and_match(tmp_path: Path) -> None:
    lab = build_lab(tmp_path, release=False)
    policy = caller_policy(lab.config, "/jwks.json")
    assert policy.issuers[0].audiences == (CALLER_AUDIENCE,)
    other = caller(
        caller_id="o",
        subject="s",
        issuer="https://other.example.invalid",
        webhook_urls=(),
        webhook_secret_file=None,
    )
    with pytest.raises(ValueError, match="one identity provider"):
        caller_policy(lab.config.model_copy(update={"callers": (caller(), other)}), "/jwks.json")
    from cole_voice_lab.api.auth import Principal

    registered = lab.config.callers[0]
    assert (
        match_caller(
            lab.config, Principal(registered.issuer, registered.subject, (CALLER_AUDIENCE,))
        )
        == registered
    )
    assert (
        match_caller(lab.config, Principal(registered.issuer, registered.subject, ("other",)))
        is None
    )


# -- segments and the event stream -----------------------------------------------------------

THREE = "The first sentence is here. A second one follows it! And is there a third?"


def parse_events(payload: bytes) -> list[dict[str, str]]:
    events = []
    for block in payload.decode().split("\n\n"):
        fields = dict(
            line.split(": ", 1)
            for line in block.splitlines()
            if ": " in line and not line.startswith(":")
        )
        if "event" in fields:
            events.append(fields)
    return events


def test_each_sentence_is_served_as_soon_as_it_is_published(rig: SpeechRig) -> None:
    _, job = answer(rig, "POST", "/api/v1/speech", {"text": THREE})
    first = f"{job['links']['self']}/segments/1/audio"
    status, refusal = answer(rig, "GET", first)
    assert (status, refusal["error"]) == (409, "audio_not_ready")
    run_jobs(rig)
    _, done = answer(rig, "GET", job["links"]["self"])
    assert done["segment_count"] == 3 and [s["index"] for s in done["segments"]] == [1, 2, 3]
    status, headers, audio = call(rig, "GET", done["segments"][1]["audio"])
    assert status == 200 and headers["ETag"] == f'"{done["segments"][1]["sha256"]}"'
    stored = Path(rig.lab.config.state_root) / "jobs" / job["id"] / "segments" / "0002.wav"
    assert audio == stored.read_bytes()
    status, _, part = call(rig, "GET", first, headers={"Range": "bytes=0-9"})
    assert status == 206 and len(part) == 10
    for path, code in (
        (f"{job['links']['self']}/segments/4/audio", "not_found"),
        (f"{job['links']['self']}/segments/0/audio", "not_found"),
        (f"{job['links']['self']}/segments/x/audio", "not_found"),
        (f"{job['links']['self']}/segments/1/other", "not_found"),
    ):
        assert answer(rig, "GET", path)[1]["error"] == code
    assert answer(rig, "POST", first)[0] == 405


def test_the_event_stream_announces_segments_then_the_end(rig: SpeechRig) -> None:
    _, job = answer(rig, "POST", "/api/v1/speech", {"text": THREE})
    received: list[bytes] = []

    def listen() -> None:
        received.append(call(rig, "GET", job["links"]["events"])[2])

    import threading

    listener = threading.Thread(target=listen)
    listener.start()
    time.sleep(0.5)
    run_jobs(rig)
    listener.join(10)
    events = parse_events(received[0])
    assert [event["event"] for event in events] == [
        "segment.ready",
        "segment.ready",
        "segment.ready",
        "speech.succeeded",
    ]
    assert [event["id"] for event in events] == ["1", "2", "3", "end"]
    assert json.loads(events[0]["data"])["audio"].endswith("/segments/1/audio")
    assert json.loads(events[-1]["data"])["state"] == "succeeded"
    assert b"first sentence" not in received[0]
    status, headers, resumed = call(
        rig, "GET", job["links"]["events"], headers={"Last-Event-ID": "2"}
    )
    assert status == 200 and headers["Content-Type"].startswith("text/event-stream")
    assert [event["id"] for event in parse_events(resumed)] == ["3", "end"]
    status, _, unicode_digit = call(
        rig, "GET", job["links"]["events"], headers={"Last-Event-ID": "²"}
    )
    assert status == 200 and [event["id"] for event in parse_events(unicode_digit)] == [
        "1",
        "2",
        "3",
        "end",
    ]
    status, refusal = answer(rig, "GET", "/api/v1/speech/sp-000000000000000000000000/events")
    assert (status, refusal["error"]) == (404, "speech_not_found")


def test_the_event_stream_keeps_alive_and_ends_on_time(rig: SpeechRig) -> None:
    _, job = answer(rig, "POST", "/api/v1/speech", {"text": "Waiting forever."})
    ticks = iter([0.0, 0.0, 16.0, 16.0, 700.0])
    routes = rig.routes
    routes.monotonic = lambda: next(ticks)
    routes.sleep = lambda _: None
    registered = rig.lab.config.callers[0]
    chunks = list(routes._events(registered, job["id"], 0))
    assert chunks[0].startswith(b"retry:") and b": keepalive\n\n" in chunks
    assert not any(b"event:" in chunk for chunk in chunks)


# -- second review ------------------------------------------------------------------------------


def test_the_caller_policy_logs_the_audiences_it_drops(tmp_path: Path) -> None:
    lab = build_lab(tmp_path, release=False)
    callers = tuple(
        caller(
            caller_id=f"c{number}",
            subject=f"s{number}",
            audiences=tuple(f"aud-{number}-{index}" for index in range(3)),
            webhook_urls=(),
            webhook_secret_file=None,
        )
        for number in range(3)
    )
    logged: list[str] = []
    config = lab.config.model_copy(update={"callers": callers})
    policy = caller_policy(config, "/jwks.json", log=logged.append)
    assert len(policy.issuers[0].audiences) == 8
    assert len(logged) == 1
    assert "9 audiences" in logged[0] and "the other 1" in logged[0] and "c2" in logged[0]
    assert "c0" not in logged[0] and "aud-" not in logged[0]
    quiet: list[str] = []
    caller_policy(lab.config, "/jwks.json", log=quiet.append)
    assert quiet == []


def _events_request(path: str) -> RouteRequest:
    token = f"Bearer {caller_token()}"
    return RouteRequest(
        method="GET",
        path=path,
        query="",
        header=lambda name: token if name == "Authorization" else None,
        content_length=None,
        read_body=lambda _limit: b"",
    )


def test_a_caller_holds_at_most_four_event_streams(rig: SpeechRig) -> None:
    _, job = answer(rig, "POST", "/api/v1/speech", {"text": "Wait for me."})
    routes = rig.routes
    request = _events_request(job["links"]["events"])
    streams = [routes(request) for _ in range(MAX_EVENT_STREAMS)]
    assert MAX_EVENT_STREAMS == 4 and routes.open_streams("agent-server") == 4
    with pytest.raises(RouteRefusal) as refusal:
        routes(request)
    assert (refusal.value.status, refusal.value.code) == (429, "too_many_streams")
    assert "too_many_streams" in REFUSAL_CODES
    # Over HTTP too, before any header of a stream is sent.
    status, body = answer(rig, "GET", job["links"]["events"])
    assert (status, body["error"]) == (429, "too_many_streams")
    # A stream the server closes gives its place back, once, even if it never started.
    first = streams[0]
    assert first is not None and first.events is not None
    close = first.events.close
    close()
    close()
    assert routes.open_streams("agent-server") == 3
    # One that ends (the job ended) does too.
    run_jobs(rig)
    second = streams[1]
    assert second is not None and second.events is not None
    assert b"event: speech.succeeded" in b"".join(second.events)
    assert routes.open_streams("agent-server") == 2
    for response in streams[2:]:
        assert response is not None and response.events is not None
        response.events.close()
    assert routes.open_streams("agent-server") == 0


# -- third review -------------------------------------------------------------------------------


def _reset_mid_body(rig: SpeechRig, path: str) -> None:
    """A GET with a declared body that the client resets part-way through."""
    with socket.create_connection(("127.0.0.1", rig.port), timeout=5) as connection:
        connection.sendall(
            (
                f"GET {path} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {caller_token()}\r\n"
                "Content-Length: 10\r\n\r\nabc"
            ).encode()
        )
        time.sleep(0.2)  # the server is reading the rest of the body
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))


def test_a_stream_whose_client_resets_before_it_starts_gives_its_place_back(
    rig: SpeechRig,
) -> None:
    _, job = answer(rig, "POST", "/api/v1/speech", {"text": "Wait for me."})
    for _ in range(MAX_EVENT_STREAMS + 1):
        _reset_mid_body(rig, job["links"]["events"])
    deadline = time.monotonic() + 5
    while rig.routes.open_streams("agent-server") and time.monotonic() < deadline:
        time.sleep(0.05)
    assert rig.routes.open_streams("agent-server") == 0
    status, _ = answer(rig, "GET", job["links"]["self"])
    assert status == 200


def test_a_stream_response_that_is_dropped_unsent_gives_its_place_back(rig: SpeechRig) -> None:
    _, job = answer(rig, "POST", "/api/v1/speech", {"text": "Wait for me."})
    response = rig.routes(_events_request(job["links"]["events"]))
    assert response is not None and rig.routes.open_streams("agent-server") == 1
    del response  # never sent and never closed: collected
    assert rig.routes.open_streams("agent-server") == 0
