"""Signed webhooks for ended speech jobs, delivered over the tailnet only (docs/SPEECH_API.md §8).

@canonical speech.webhooks -- ``voice-lab speech deliver`` (the host unit
``voice-lab-speech-webhooks``, whose network is the tailnet ranges only) calls
``WebhookDispatcher.deliver_due``. It adopts each new event from ``outbox/`` into
``webhooks/<event id>/`` and makes every attempt that is due.

Signing is Standard Webhooks: ``webhook-id`` (the event id, the same on every attempt),
``webhook-timestamp`` (the attempt's time, whole seconds) and ``webhook-signature``
(``v1,`` and the base64 HMAC-SHA256 of ``<id>.<timestamp>.<body>`` keyed with the
base64-decoded part of the caller's ``whsec_`` secret). The body is the same bytes on every
attempt and carries no text and no audio.

Before each attempt the URL is checked again against the caller's allowlist and the tailnet
suffix; the host is resolved and every address must be a tailnet address; the connection
goes to the address that was checked, with TLS verified for the host name; no redirect is
followed; timeouts are short; at most 4 KiB of the answer is read and discarded. A ``2xx``
delivers, ``410`` stops, anything else is retried at 0, 30 s, 2, 10, 30 and 60 minutes after
the event, then given up. The delivery log keeps each attempt's number, time, status or
error class and duration: never the secret, the signature or the body.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import http.client
import ipaddress
import os
import shutil
import socket
import ssl
import stat
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from ..canonical import canonical_json_bytes, open_regular_no_follow, write_canonical_json
from ..errors import VoiceLabError
from ..models import utc_now
from .config import SpeechCaller, SpeechConfig, parse_webhook_url
from .contracts import DeliveryAttempt, WebhookDeliveryLog, WebhookOutboxEntry
from .store import SpeechStore

RETRY_OFFSETS: Final = (0, 30, 120, 600, 1800, 3600)
CONNECT_TIMEOUT_SECONDS: Final = 5.0
TOTAL_TIMEOUT_SECONDS: Final = 10.0
MAX_ANSWER_BYTES: Final = 4096
MAX_EVENT_BYTES: Final = 64 * 1024
SECRET_PREFIX: Final = "whsec_"
TAILNET_NETWORKS: Final = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),
)
USER_AGENT: Final = "voice-lab-webhooks/1"


class WebhookRefused(Exception):
    """An attempt that must not be made (the message names why, never a secret)."""


class WebhookSecretError(VoiceLabError):
    """A caller's secret file is missing, unsafe or malformed."""


def parse_secret(text: str) -> bytes:
    """The HMAC key of a ``whsec_<base64>`` secret (24 to 64 bytes).

    Raises: ``WebhookSecretError``.
    """
    value = text.strip()
    if not value.startswith(SECRET_PREFIX):
        raise WebhookSecretError("a webhook secret starts with whsec_")
    try:
        key = base64.b64decode(value.removeprefix(SECRET_PREFIX), validate=True)
    except (binascii.Error, ValueError) as error:
        raise WebhookSecretError("a webhook secret is whsec_ and base64") from error
    if not 24 <= len(key) <= 64:
        raise WebhookSecretError("a webhook secret holds 24 to 64 bytes")
    return key


def read_secret(path: Path) -> bytes:
    """Read a caller's secret file: regular, not a link, not group-writable or world-readable.

    Raises: ``WebhookSecretError`` (naming the path, never the contents).
    """
    try:
        with open_regular_no_follow(path) as stream:
            mode = stat.S_IMODE(os.fstat(stream.fileno()).st_mode)
            if mode & 0o027:
                raise WebhookSecretError(f"the webhook secret file {path} is open to others")
            data = stream.read(1024)
    except (OSError, VoiceLabError) as error:
        if isinstance(error, WebhookSecretError):
            raise
        raise WebhookSecretError(f"the webhook secret file {path} cannot be read") from error
    try:
        return parse_secret(data.decode("ascii"))
    except UnicodeDecodeError as error:
        raise WebhookSecretError(f"the webhook secret file {path} is not ASCII") from error


def sign(key: bytes, message_id: str, timestamp: int, body: bytes) -> str:
    """The ``webhook-signature`` value for one attempt."""
    content = f"{message_id}.{timestamp}.".encode("ascii") + body
    digest = hmac.new(key, content, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode("ascii")


def verify_signature(
    key: bytes,
    message_id: str,
    timestamp: str,
    body: bytes,
    signature_header: str,
    *,
    now: float,
    tolerance_seconds: int = 300,
) -> bool:
    """What a receiver does: a fresh timestamp and one matching ``v1`` signature."""
    if not timestamp.isdigit() or abs(now - int(timestamp)) > tolerance_seconds:
        return False
    expected = sign(key, message_id, int(timestamp), body)
    return any(
        hmac.compare_digest(candidate, expected) for candidate in signature_header.split(" ")
    )


type Resolver = Callable[[str, int], Sequence[str]]


def system_resolver(host: str, port: int) -> Sequence[str]:
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as error:
        raise WebhookRefused("dns-failed") from error
    return [str(answer[4][0]) for answer in answers]


def tailnet_address(host: str, port: int, resolver: Resolver) -> str:
    """One address of ``host``, once every address it resolves to is on the tailnet.

    Raises: ``WebhookRefused`` ``dns-failed`` or ``address-not-tailnet``.
    """
    addresses = list(resolver(host, port))
    if not addresses:
        raise WebhookRefused("dns-failed")
    parsed = []
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError as error:
            raise WebhookRefused("address-not-tailnet") from error
        if not any(ip in network for network in TAILNET_NETWORKS):
            raise WebhookRefused("address-not-tailnet")
        parsed.append(str(ip))
    return parsed[0]


@dataclass(frozen=True, slots=True)
class Delivery:
    """One attempt, ready to send: where (the checked address), what, and the headers."""

    host: str
    port: int
    path: str
    address: str
    headers: tuple[tuple[str, str], ...]
    body: bytes


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """An HTTPS connection to a checked address, with TLS verified for the host name."""

    def __init__(self, host: str, port: int, address: str, context: ssl.SSLContext) -> None:
        super().__init__(host, port, timeout=CONNECT_TIMEOUT_SECONDS, context=context)
        self._address = address
        self._pinned_context = context

    def connect(self) -> None:  # pragma: no cover - exercised against a real tailnet host only
        raw = socket.create_connection((self._address, self.port), timeout=CONNECT_TIMEOUT_SECONDS)
        raw.settimeout(TOTAL_TIMEOUT_SECONDS)
        self.sock = self._pinned_context.wrap_socket(raw, server_hostname=self.host)


def https_transport(delivery: Delivery) -> int:  # pragma: no cover - needs a tailnet peer
    """POST ``delivery`` and return the status; redirects are never followed."""
    connection = _PinnedHTTPSConnection(
        delivery.host, delivery.port, delivery.address, ssl.create_default_context()
    )
    try:
        connection.request(
            "POST", delivery.path, body=delivery.body, headers=dict(delivery.headers)
        )
        response = connection.getresponse()
        response.read(MAX_ANSWER_BYTES)
        return response.status
    finally:
        connection.close()


type Transport = Callable[[Delivery], int]


class WebhookDispatcher:
    """Delivers the events of one configuration's state root."""

    def __init__(
        self,
        config: SpeechConfig,
        *,
        transport: Transport = https_transport,
        resolver: Resolver = system_resolver,
        secret_reader: Callable[[Path], bytes] = read_secret,
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.store = SpeechStore(config)
        self.transport = transport
        self.resolver = resolver
        self.secret_reader = secret_reader
        self.clock = clock
        self.monotonic = monotonic

    # -- the outbox ----------------------------------------------------------------------------

    def _adopt(self) -> None:
        for name in sorted(os.listdir(self.store.outbox_root)):
            if name.startswith(".") or not name.endswith(".json"):
                continue
            event_id = name.removesuffix(".json")
            source = self.store.outbox_root / name
            try:
                with open_regular_no_follow(source) as stream:
                    entry = WebhookOutboxEntry.model_validate_json(stream.read(MAX_EVENT_BYTES))
            except (ValueError, VoiceLabError):
                source.unlink(missing_ok=True)  # not an event this process wrote; never sent
                continue
            if entry.event_id != event_id:
                source.unlink(missing_ok=True)
                continue
            directory = self.store.webhooks_root / event_id
            directory.mkdir(mode=0o700, exist_ok=True)
            if not (directory / "delivery.json").exists():
                write_canonical_json(
                    directory / "delivery.json",
                    WebhookDeliveryLog(
                        event_id=event_id,
                        caller_id=entry.caller_id,
                        outcome="pending",
                        attempts=(),
                    ),
                )
            os.replace(source, directory / "event.json")

    def _pending(self) -> list[tuple[Path, WebhookOutboxEntry, WebhookDeliveryLog]]:
        found = []
        for name in sorted(os.listdir(self.store.webhooks_root)):
            directory = self.store.webhooks_root / name
            try:
                with open_regular_no_follow(directory / "event.json") as stream:
                    entry = WebhookOutboxEntry.model_validate_json(stream.read(MAX_EVENT_BYTES))
                with open_regular_no_follow(directory / "delivery.json") as stream:
                    log = WebhookDeliveryLog.model_validate_json(stream.read(MAX_EVENT_BYTES))
            except (ValueError, VoiceLabError):
                continue
            found.append((directory, entry, log))
        return found

    # -- attempts ------------------------------------------------------------------------------

    def deliver_due(self) -> dict[str, str]:
        """Adopt new events, make every due attempt, sweep ended logs; return each outcome."""
        self.store.ensure()
        self._adopt()
        outcomes: dict[str, str] = {}
        now = self.clock()
        for directory, entry, log in self._pending():
            if log.outcome != "pending":
                if (
                    log.ended_at is not None
                    and log.ended_at + timedelta(days=self.config.record_retention_days) <= now
                ):
                    shutil.rmtree(directory)
                continue
            due = entry.created_at + timedelta(seconds=RETRY_OFFSETS[len(log.attempts)])
            if now < due:
                outcomes[entry.event_id] = "waiting"
                continue
            log = self._attempt(entry, log, now)
            write_canonical_json(directory / "delivery.json", log)
            outcomes[entry.event_id] = log.outcome
        return outcomes

    def _caller(self, entry: WebhookOutboxEntry) -> SpeechCaller:
        caller = self.config.caller(entry.caller_id)
        if caller is None:
            raise WebhookRefused("caller-not-registered")
        if not caller.allows_callback(entry.url):
            raise WebhookRefused("url-not-allowed")
        return caller

    def _attempt(
        self, entry: WebhookOutboxEntry, log: WebhookDeliveryLog, now: datetime
    ) -> WebhookDeliveryLog:
        number = len(log.attempts) + 1
        started = self.monotonic()
        status: int | None = None
        error: str | None = None
        final: str | None = None
        try:
            caller = self._caller(entry)
            parts = parse_webhook_url(entry.url, self.config.tailnet_suffix)
            assert caller.webhook_secret_file is not None
            key = self.secret_reader(Path(caller.webhook_secret_file))
        except (WebhookRefused, ValueError, WebhookSecretError) as refusal:
            error = (
                str(refusal)
                if isinstance(refusal, WebhookRefused)
                else (
                    "secret-unavailable"
                    if isinstance(refusal, WebhookSecretError)
                    else "url-invalid"
                )
            )
            final = "refused"
        else:
            host = parts.hostname or ""
            port = parts.port or 443
            try:
                address = tailnet_address(host, port, self.resolver)
                body = canonical_json_bytes(entry.event)
                timestamp = int(now.timestamp())
                delivery = Delivery(
                    host=host,
                    port=port,
                    path=parts.path,
                    address=address,
                    headers=(
                        ("Content-Type", "application/json"),
                        ("User-Agent", USER_AGENT),
                        ("webhook-id", entry.event_id),
                        ("webhook-timestamp", str(timestamp)),
                        ("webhook-signature", sign(key, entry.event_id, timestamp, body)),
                    ),
                    body=body,
                )
                status = self.transport(delivery)
            except WebhookRefused as refusal:
                error = str(refusal)
            except (OSError, ssl.SSLError, http.client.HTTPException) as failure:
                error = type(failure).__name__[:200]
            if status is not None:
                if 200 <= status <= 299:
                    final = "delivered"
                elif status == 410:
                    final = "gone"
        attempt = DeliveryAttempt(
            number=number,
            at=now,
            status=status if status is not None and 100 <= status <= 599 else None,
            error=error,
            duration_ms=max(0, round((self.monotonic() - started) * 1000)),
        )
        attempts = (*log.attempts, attempt)
        if final is None and number >= len(RETRY_OFFSETS):
            final = "given-up"
        return log.model_validate(
            {
                **log.model_dump(),
                "attempts": attempts,
                "outcome": final or "pending",
                "ended_at": None if final is None else now,
            }
        )
