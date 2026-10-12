"""Signed webhooks and their delivery (speech/webhooks.py, docs/SPEECH_API.md §8).

The transport and the resolver are injected: no test connects anywhere. Signatures are
checked against an independent implementation of the Standard Webhooks algorithm.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from speech_fixtures import KEY, NOW, SECRET, FakeBackend, Lab, OnePass, build_lab, caller

from cole_voice_lab.speech.contracts import SpeechRequest, WebhookDeliveryLog
from cole_voice_lab.speech.store import SpeechStore
from cole_voice_lab.speech.webhooks import (
    RETRY_OFFSETS,
    Delivery,
    WebhookDispatcher,
    WebhookRefused,
    WebhookSecretError,
    parse_secret,
    read_secret,
    sign,
    tailnet_address,
    verify_signature,
)

TAILNET_ADDRESS = "100.101.102.103"


def standard_webhooks_signature(secret: str, message_id: str, timestamp: str, body: bytes) -> str:
    """The Standard Webhooks reference algorithm, written out independently."""
    key = base64.b64decode(secret.split("_", 1)[1])
    content = message_id.encode() + b"." + timestamp.encode() + b"." + body
    return "v1," + base64.b64encode(hmac.new(key, content, hashlib.sha256).digest()).decode()


def test_signing_follows_standard_webhooks() -> None:
    body = b'{"type":"speech.succeeded"}'
    signature = sign(KEY, "msg_sp-1", 1_760_184_000, body)
    assert signature == standard_webhooks_signature(SECRET, "msg_sp-1", "1760184000", body)
    assert verify_signature(KEY, "msg_sp-1", "1760184000", body, signature, now=1_760_184_100)
    assert verify_signature(
        KEY, "msg_sp-1", "1760184000", body, f"v1,other {signature}", now=1_760_184_000
    )
    assert not verify_signature(
        KEY, "msg_sp-1", "1760184000", body + b" ", signature, now=1_760_184_000
    )
    assert not verify_signature(KEY, "msg_sp-1", "1760184000", body, signature, now=1_760_184_301)
    assert not verify_signature(KEY, "msg_sp-1", "-1", body, signature, now=0)


def test_the_published_standard_webhooks_example_verifies() -> None:
    # The example of the Standard Webhooks specification (standard-webhooks/standard-webhooks).
    key = parse_secret("whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw")
    body = b'{"test": 2432232314}'
    assert sign(key, "msg_p5jXN8AQM9LWM0D4loKWxJek", 1614265330, body) == (
        "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="
    )


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("secret", "starts with whsec_"),
        ("whsec_not base64!", "base64"),
        ("whsec_" + base64.b64encode(b"short").decode(), "24 to 64"),
    ],
)
def test_a_malformed_secret_is_refused(text: str, message: str) -> None:
    with pytest.raises(WebhookSecretError, match=message):
        parse_secret(text)


def test_a_secret_file_must_be_private_and_regular(tmp_path: Path) -> None:
    path = tmp_path / "secret"
    path.write_text(SECRET + "\n")
    path.chmod(0o640)
    assert read_secret(path) == KEY
    path.chmod(0o644)
    with pytest.raises(WebhookSecretError, match="open to others"):
        read_secret(path)
    path.chmod(0o600)
    link = tmp_path / "link"
    os.symlink(path, link)
    with pytest.raises(WebhookSecretError, match="cannot be read"):
        read_secret(link)
    path.write_bytes(b"whsec_\xff")
    with pytest.raises(WebhookSecretError, match="ASCII"):
        read_secret(path)
    with pytest.raises(WebhookSecretError, match="cannot be read"):
        read_secret(tmp_path / "missing")


@pytest.mark.parametrize(
    ("addresses", "allowed"),
    [
        ([TAILNET_ADDRESS], True),
        (["fd7a:115c:a1e0::1234"], True),
        (["127.0.0.1"], False),
        (["10.0.0.5"], False),
        (["169.254.169.254"], False),
        (["93.184.216.34"], False),
        ([TAILNET_ADDRESS, "127.0.0.1"], False),
        (["::1"], False),
        (["not-an-address"], False),
        ([], False),
    ],
)
def test_only_tailnet_addresses_are_ever_contacted(addresses: list[str], allowed: bool) -> None:
    if allowed:
        assert tailnet_address("agent", 443, lambda host, port: addresses) == addresses[0]
    else:
        with pytest.raises(WebhookRefused):
            tailnet_address("agent", 443, lambda host, port: addresses)


class Recorder:
    def __init__(self, *statuses: int | Exception) -> None:
        self.statuses = list(statuses)
        self.sent: list[Delivery] = []

    def __call__(self, delivery: Delivery) -> int:
        self.sent.append(delivery)
        outcome = self.statuses.pop(0) if self.statuses else 200
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> Any:
        return self.now


def ended_job(lab: Lab, **fields: Any) -> str:
    store = SpeechStore(lab.config)
    store.ensure()
    fields.setdefault("callback_url", lab.config.callers[0].webhook_urls[0] + "/turn-1")
    job, _ = store.submit(lab.config.callers[0], SpeechRequest(text="Hello.", **fields), now=NOW)
    OnePass(lab.config, FakeBackend(), clock=lambda: NOW).run_pending()
    return job.record.job_id


def dispatcher(lab: Lab, transport: Recorder, clock: Clock, **options: Any) -> WebhookDispatcher:
    options.setdefault("resolver", lambda host, port: [TAILNET_ADDRESS])
    options.setdefault("secret_reader", lambda path: KEY)
    return WebhookDispatcher(lab.config, transport=transport, clock=clock, **options)


def delivery_log(lab: Lab, job_id: str) -> WebhookDeliveryLog:
    path = Path(lab.config.state_root) / "webhooks" / f"msg_{job_id}" / "delivery.json"
    return WebhookDeliveryLog.model_validate_json(path.read_bytes())


@pytest.fixture
def lab(tmp_path: Path) -> Lab:
    return build_lab(tmp_path)


def test_an_ended_job_is_delivered_once_signed_with_no_text_or_audio(lab: Lab) -> None:
    job_id = ended_job(lab, client_ref="turn-1")
    transport, clock = Recorder(204), Clock()
    assert dispatcher(lab, transport, clock).deliver_due() == {f"msg_{job_id}": "delivered"}
    (sent,) = transport.sent
    headers = dict(sent.headers)
    assert (sent.host, sent.port, sent.path, sent.address) == (
        "agent.example-tailnet.ts.net",
        443,
        "/hooks/voice-lab/turn-1",
        TAILNET_ADDRESS,
    )
    assert headers["webhook-id"] == f"msg_{job_id}"
    assert headers["webhook-timestamp"] == str(int(NOW.timestamp()))
    assert headers["webhook-signature"] == standard_webhooks_signature(
        SECRET, f"msg_{job_id}", headers["webhook-timestamp"], sent.body
    )
    event = json.loads(sent.body)
    assert event["type"] == "speech.succeeded" and event["data"]["id"] == job_id
    assert event["data"]["client_ref"] == "turn-1" and event["data"]["audio"]["sha256"]
    assert b"Hello" not in sent.body
    log = delivery_log(lab, job_id)
    assert (log.outcome, len(log.attempts), log.attempts[0].status) == ("delivered", 1, 204)
    assert dispatcher(lab, transport, clock).deliver_due() == {}
    assert len(transport.sent) == 1
    log_text = json.dumps(log.model_dump(mode="json"))
    assert headers["webhook-signature"] not in log_text and "Hello" not in log_text


def test_failed_attempts_are_retried_on_schedule_then_given_up(lab: Lab) -> None:
    job_id = ended_job(lab)
    event = f"msg_{job_id}"
    transport = Recorder(500, OSError("refused"), 302, 404, 503, 500)
    clock = Clock()
    deliver = dispatcher(lab, transport, clock)
    assert deliver.deliver_due() == {event: "pending"}
    for offset in RETRY_OFFSETS[1:]:
        clock.now = NOW + timedelta(seconds=offset - 1)
        assert deliver.deliver_due() == {event: "waiting"}
        clock.now = NOW + timedelta(seconds=offset)
        deliver.deliver_due()
    log = delivery_log(lab, job_id)
    assert log.outcome == "given-up" and len(log.attempts) == len(RETRY_OFFSETS) == 6
    assert [attempt.status for attempt in log.attempts] == [500, None, 302, 404, 503, 500]
    assert log.attempts[1].error == "OSError"
    assert [attempt.at - NOW for attempt in log.attempts] == [
        timedelta(seconds=offset) for offset in RETRY_OFFSETS
    ]
    assert RETRY_OFFSETS[-1] == 3600
    timestamps = {dict(sent.headers)["webhook-timestamp"] for sent in transport.sent}
    assert len(timestamps) == 6 and len({sent.body for sent in transport.sent}) == 1
    clock.now = NOW + timedelta(days=31)
    deliver.deliver_due()
    assert not (Path(lab.config.state_root) / "webhooks" / event).exists()


def test_410_stops_and_a_later_success_delivers(lab: Lab) -> None:
    gone = ended_job(lab)
    dispatcher(lab, Recorder(410), Clock()).deliver_due()
    assert delivery_log(lab, gone).outcome == "gone"
    later = ended_job(lab, idempotency_key="second")
    clock = Clock()
    deliver = dispatcher(lab, Recorder(502, 200), clock)
    deliver.deliver_due()
    clock.now = NOW + timedelta(seconds=30)
    deliver.deliver_due()
    assert delivery_log(lab, later).outcome == "delivered"


def test_an_address_off_the_tailnet_is_never_contacted_and_is_retried(lab: Lab) -> None:
    job_id = ended_job(lab)
    transport = Recorder()
    dispatcher(lab, transport, Clock(), resolver=lambda host, port: ["127.0.0.1"]).deliver_due()
    assert transport.sent == []
    log = delivery_log(lab, job_id)
    assert (log.outcome, log.attempts[0].error) == ("pending", "address-not-tailnet")


@pytest.mark.parametrize("change", ["caller", "url", "secret"])
def test_a_registry_change_refuses_the_event_without_sending(tmp_path: Path, change: str) -> None:
    lab = build_lab(tmp_path)
    job_id = ended_job(lab)
    options: dict[str, Any] = {}
    config = lab.config
    if change == "caller":
        other = caller(caller_id="other", subject="client-other")
        config = config.model_copy(update={"callers": (other,)})
    elif change == "url":
        moved = caller(webhook_urls=("https://agent.example-tailnet.ts.net/elsewhere",))
        config = config.model_copy(update={"callers": (moved,)})
    else:
        options["secret_reader"] = read_secret  # the configured file does not exist here
    transport = Recorder()
    lab.config = config
    dispatcher(lab, transport, Clock(), **options).deliver_due()
    assert transport.sent == []
    log = delivery_log(lab, job_id)
    expected = {
        "caller": "caller-not-registered",
        "url": "url-not-allowed",
        "secret": "secret-unavailable",
    }
    assert (log.outcome, log.attempts[0].error) == ("refused", expected[change])


def test_only_this_process_events_are_adopted(lab: Lab) -> None:
    outbox = Path(lab.config.state_root) / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    (outbox / "msg_sp-000000000000000000000000.json").write_text("{}")
    (outbox / ".hidden.json").write_text("{}")
    job_id = ended_job(lab)
    event = outbox / f"msg_{job_id}.json"
    event.rename(outbox / "msg_sp-111111111111111111111111.json")
    transport = Recorder()
    assert dispatcher(lab, transport, Clock()).deliver_due() == {}
    assert transport.sent == [] and sorted(os.listdir(outbox)) == [".hidden.json"]


def test_a_job_without_a_callback_queues_no_event(lab: Lab) -> None:
    ended_job(lab, callback_url=None)
    assert os.listdir(Path(lab.config.state_root) / "outbox") == []
