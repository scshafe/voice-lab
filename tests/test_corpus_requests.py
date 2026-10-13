"""Decisions and trims on the PC (D-052, docs/design/RECORDING-V1.md step 4): the API queues a
request in ``incoming/``, the intake publisher applies it at the revision the client expected,
and the read model follows. Synthetic takes only (``corpus_take_fixtures``)."""

from __future__ import annotations

import http.client
import json
import os
import shutil
import stat
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from corpus_take_fixtures import NOW, make_take, seed_catalog
from home_host_fixtures import jwks, mint, policy

from cole_voice_lab import cli_home_host, corpus_requests, intake_publisher, storage_binding
from cole_voice_lab.api import corpus_request_routes, staging
from cole_voice_lab.api.auth import TokenVerifier
from cole_voice_lab.api.contracts import REFUSAL_CODES, ApiMeta
from cole_voice_lab.api.corpus_contracts import (
    CorpusRequestStatus,
    TakeDecisionRequest,
    TakeHistory,
    TakeTrimRequest,
)
from cole_voice_lab.api.corpus_routes import CorpusRoutes
from cole_voice_lab.api.routing import RouteRefusal, RouteRequest
from cole_voice_lab.api.server import ApiServer
from cole_voice_lab.api.staging import IntakeStaging
from cole_voice_lab.canonical import canonical_json_bytes, write_canonical_json
from cole_voice_lab.cli import main
from cole_voice_lab.corpus_read_model import read_library
from cole_voice_lab.corpus_requests import (
    FINALIZED_NAME,
    UPLOADS_NAME,
    CorpusRequest,
    CorpusRequestOutcome,
    DecisionChange,
    TrimChange,
    nanoseconds,
    outcome_path,
    parse_request_name,
    queue_request,
    queued_requests,
    read_outcome,
    request_name,
)
from cole_voice_lab.corpus_take_store import CorpusTakeState, read_take_state, state_path
from cole_voice_lab.corpus_takes import MAX_TAKE_FRAMES, ValidatedTake
from cole_voice_lab.errors import ContractError, SecurityError
from cole_voice_lab.intake_publisher import PublishSummary, publish_pending
from cole_voice_lab.library_classification import classify
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_contracts import CatalogReference, CatalogSnapshot

type Catalog = tuple[CatalogReference, CatalogSnapshot]
type Answer = tuple[int, dict[str, str], bytes]


@pytest.fixture
def layout(tmp_path: Path) -> AssetLayout:
    return AssetLayout.initialize(tmp_path / "srv" / "voice-lab" / "lab")


@pytest.fixture
def catalog(layout: AssetLayout) -> Catalog:
    return seed_catalog(layout)


@pytest.fixture
def work(tmp_path: Path) -> Path:
    directory = tmp_path / "work"
    directory.mkdir()
    return directory


class Ticker:
    """The API's clock in tests: ``NOW``, then a millisecond later at each call (or the times
    a test sets)."""

    def __init__(self, *times: datetime) -> None:
        self.times = list(times)
        self.now = NOW

    def __call__(self) -> datetime:
        if self.times:
            return self.times.pop(0)
        self.now += timedelta(milliseconds=1)
        return self.now


@contextmanager
def serving(
    layout: AssetLayout, tmp_path: Path, clock: Callable[[], datetime] | None = None
) -> Iterator[tuple[int, CorpusRoutes]]:
    jwks_path = tmp_path / "jwks.json"
    jwks_path.write_text(json.dumps(jwks()), encoding="utf-8")
    routes = CorpusRoutes(layout, clock=clock or Ticker())
    server = ApiServer(
        ("127.0.0.1", 0),
        staging=IntakeStaging(layout, min_free_bytes=0),
        verifier=TokenVerifier(policy(jwks_path)),
        meta=ApiMeta(server_build="test-build", minimum_client_version="0.1.0"),
        log=False,
        routes=(routes,),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1]), routes
    finally:
        server.shutdown()
        server.server_close()


def send(port: int, method: str, path: str, body: dict[str, Any] | bytes | None = None) -> Answer:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    payload = json.dumps(body).encode() if isinstance(body, dict) else body
    headers = {"Authorization": f"Bearer {mint()}"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    connection.request(method, path, body=payload, headers=headers)
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response.status, {k.lower(): v for k, v in response.getheaders()}, data


def rid(number: int) -> str:
    return f"req-{number:032x}"


def decide(
    port: int,
    take_id: str,
    request_id: str,
    decision: str,
    expected: int,
    reason: str | None = None,
) -> Answer:
    body = {
        "request_id": request_id,
        "decision": decision,
        "reason": reason,
        "expected_revision": expected,
    }
    return send(port, "POST", f"/api/v1/corpus/takes/{take_id}/decision", body)


def trim(
    port: int, take_id: str, request_id: str, start: int | None, end: int | None, expected: int
) -> Answer:
    body = {
        "request_id": request_id,
        "start_frame": start,
        "end_frame": end,
        "expected_revision": expected,
    }
    return send(port, "PUT", f"/api/v1/corpus/takes/{take_id}/trim", body)


def accepted(answer: Answer) -> CorpusRequestStatus:
    status, headers, body = answer
    assert status == 202, body
    view = CorpusRequestStatus.model_validate_json(body)
    assert headers["location"] == f"/api/v1/corpus/requests/{view.request_id}"
    return view


def request_status(port: int, request_id: str) -> CorpusRequestStatus:
    status, _headers, body = send(port, "GET", f"/api/v1/corpus/requests/{request_id}")
    assert status == 200, body
    return CorpusRequestStatus.model_validate_json(body)


def refusal(answer: Answer) -> tuple[int, str]:
    code = json.loads(answer[2])["error"]
    assert code in REFUSAL_CODES
    return answer[0], code


def published(layout: AssetLayout, catalog: Catalog, work: Path, seed: int = 0) -> ValidatedTake:
    take = make_take(work / f"take-{seed}", catalog=catalog, seed=seed, frequency=200.0 + seed)
    os.rename(work / f"take-{seed}", layout.directory("incoming") / take.session_id)
    assert len(publish_pending(layout, min_free_bytes=0).accepted) == 1
    return take


def state_of(layout: AssetLayout, take: ValidatedTake) -> CorpusTakeState:
    state = read_take_state(layout, take.session_id)
    assert state is not None
    return state


def history(port: int, take: ValidatedTake) -> TakeHistory:
    path = f"/api/v1/corpus/prompts/{take.manifest.prompt.prompt_id}/takes"
    status, _headers, body = send(port, "GET", path)
    assert status == 200, body
    return TakeHistory.model_validate_json(body)


def waiting_files(layout: AssetLayout) -> list[str]:
    return sorted(
        name for name in os.listdir(layout.directory("incoming")) if name.startswith(".corpus")
    )


def a_pass(layout: AssetLayout) -> PublishSummary:
    return publish_pending(layout, min_free_bytes=0)


# -- end to end ---------------------------------------------------------------------------------


def test_a_decision_is_applied_and_a_stale_revision_is_refused(
    layout: AssetLayout, catalog: Catalog, work: Path, tmp_path: Path
) -> None:
    take = published(layout, catalog, work)
    take_id = take.session_id
    with serving(layout, tmp_path) as (port, _routes):
        queued = accepted(decide(port, take_id, rid(1), "kept", 0))
        assert (queued.status, queued.take_id, queued.outcome) == ("queued", take_id, None)
        assert queued.received_at is not None
        assert request_status(port, rid(1)) == queued
        [name] = waiting_files(layout)
        assert name == request_name(rid(1), nanoseconds(queued.received_at))
        path = layout.directory("incoming") / name
        assert stat.S_IMODE(path.lstat().st_mode) == 0o600
        written = CorpusRequest.model_validate_json(path.read_bytes())
        assert path.read_bytes() == canonical_json_bytes(written) + b"\n"
        assert written.change == DecisionChange(decision="kept", reason=None, expected_revision=0)
        assert state_of(layout, take).decision.revision == 0  # the API never writes the store

        summary = a_pass(layout)
        assert (summary.requests_applied, summary.requests_refused) == ([rid(1)], [])
        assert summary.requests_waiting == [] and summary.read_model == "current"
        assert waiting_files(layout) == []
        applied = request_status(port, rid(1))
        assert applied.status == "applied" and applied.outcome is not None
        assert (applied.outcome.revision, applied.outcome.code) == (1, None)
        state = state_of(layout, take)
        assert (state.decision.decision, state.decision.revision) == ("kept", 1)
        assert state.decision.decided_at == applied.outcome.decided_at
        [listed] = history(port, take).takes
        assert (listed.decision, listed.decision_revision) == ("kept", 1)
        expected_status = "kept" if state.qc_status == "pass" else "needs-attention"
        assert history(port, take).row.status == expected_status

        # A client that has not seen revision 1 rejects the take: refused, nothing changes.
        accepted(decide(port, take_id, rid(2), "rejected", 0, reason="a cough"))
        summary = a_pass(layout)
        assert summary.requests_refused == [
            {"request": rid(2), "take": take_id, "code": "revision_conflict"}
        ]
        refused = request_status(port, rid(2))
        assert refused.status == "refused" and refused.outcome is not None
        assert (refused.outcome.code, refused.outcome.revision) == ("revision_conflict", 1)
        assert state_of(layout, take) == state
        assert history(port, take).takes == (listed,)
        # Applied at the revision it saw, the rejection goes through.
        accepted(decide(port, take_id, rid(3), "rejected", 1, reason="a cough"))
        assert a_pass(layout).requests_applied == [rid(3)]
        rejected = state_of(layout, take).decision
        assert (rejected.decision, rejected.revision, rejected.reason) == ("rejected", 2, "a cough")
        # Removed is a state; a restore sets the decision back. Nothing is deleted.
        accepted(decide(port, take_id, rid(4), "removed", 2))
        accepted(decide(port, take_id, rid(5), "kept", 3))
        assert a_pass(layout).requests_applied == [rid(4), rid(5)]
        assert state_of(layout, take).decision.decision == "kept"
        assert (layout.directory("raw") / take.manifest_sha256).is_dir()


def test_trims_are_set_and_reset_and_bounded_by_the_take(
    layout: AssetLayout, catalog: Catalog, work: Path, tmp_path: Path
) -> None:
    take = published(layout, catalog, work)
    take_id = take.session_id
    frames = take.manifest.audio.audio.frames
    with serving(layout, tmp_path) as (port, _routes):
        accepted(trim(port, take_id, rid(1), 480, frames - 480, 0))
        assert a_pass(layout).requests_applied == [rid(1)]
        cut = state_of(layout, take).trim
        assert (cut.revision, cut.start_frame, cut.end_frame) == (1, 480, frames - 480)
        assert history(port, take).takes[0].trim == cut
        accepted(trim(port, take_id, rid(2), None, None, 1))  # reset
        assert a_pass(layout).requests_applied == [rid(2)]
        reset = state_of(layout, take).trim
        assert (reset.revision, reset.start_frame, reset.end_frame) == (2, None, None)
        # Past the take's last frame: refused by the API, nothing queued.
        assert refusal(trim(port, take_id, rid(3), 0, frames + 1, 2)) == (
            422, "trim_out_of_bounds",
        )  # fmt: skip
        assert waiting_files(layout) == []
        assert accepted(trim(port, take_id, rid(4), 0, frames, 2)).status == "queued"
        # A stale trim revision is the publisher's to refuse.
        accepted(trim(port, take_id, rid(5), 10, 20, 1))
        summary = a_pass(layout)
        assert summary.requests_applied == [rid(4)]
        assert summary.requests_refused == [
            {"request": rid(5), "take": take_id, "code": "revision_conflict"}
        ]
        assert state_of(layout, take).trim.revision == 3
        for body in (
            {"start_frame": 5, "end_frame": 5},
            {"start_frame": 5, "end_frame": None},
            {"start_frame": -1, "end_frame": 5},
            {"start_frame": 0, "end_frame": MAX_TAKE_FRAMES + 1},
        ):
            payload = {"request_id": rid(6), "expected_revision": 3, **body}
            assert refusal(send(port, "PUT", f"/api/v1/corpus/takes/{take_id}/trim", payload)) == (
                422, "invalid_request",
            )  # fmt: skip


def test_the_publisher_bounds_a_trim_the_api_could_not(
    layout: AssetLayout, catalog: Catalog, work: Path, tmp_path: Path
) -> None:
    # The take is still on its way when its trim arrives, so only the publisher knows its
    # frames.
    take = make_take(work / "take", catalog=catalog)
    frames = take.manifest.audio.audio.frames
    with serving(layout, tmp_path) as (port, _routes):
        os.rename(work / "take", layout.directory("incoming") / take.session_id)
        accepted(trim(port, take.session_id, rid(1), 0, frames + 1, 0))
        summary = a_pass(layout)
        assert [receipt.session_id for receipt in summary.accepted] == [take.session_id]
        assert summary.requests_refused == [
            {"request": rid(1), "take": take.session_id, "code": "trim_out_of_bounds"}
        ]
        outcome = request_status(port, rid(1)).outcome
        assert outcome is not None and outcome.revision == 0
        assert state_of(layout, take).trim.revision == 0


# -- the API's checks -------------------------------------------------------------------------


def test_a_request_id_answers_as_before_and_refuses_another_change(
    layout: AssetLayout, catalog: Catalog, work: Path, tmp_path: Path
) -> None:
    take = published(layout, catalog, work)
    other = published(layout, catalog, work, seed=1)
    with serving(layout, tmp_path) as (port, _routes):
        first = accepted(decide(port, take.session_id, rid(1), "kept", 0))
        assert accepted(decide(port, take.session_id, rid(1), "kept", 0)) == first
        assert len(waiting_files(layout)) == 1
        for answer in (
            decide(port, take.session_id, rid(1), "removed", 0),
            decide(port, take.session_id, rid(1), "kept", 1),
            decide(port, other.session_id, rid(1), "kept", 0),
            trim(port, take.session_id, rid(1), None, None, 0),
        ):
            assert refusal(answer) == (409, "request_conflict")
        a_pass(layout)
        again = accepted(decide(port, take.session_id, rid(1), "kept", 0))
        assert again.status == "applied" and again == request_status(port, rid(1))
        assert waiting_files(layout) == []  # answered, not queued again
        assert refusal(decide(port, take.session_id, rid(1), "removed", 1)) == (
            409, "request_conflict",
        )  # fmt: skip
        # A refused request answers as refused.
        accepted(decide(port, other.session_id, rid(2), "kept", 5))
        a_pass(layout)
        assert accepted(decide(port, other.session_id, rid(2), "kept", 5)).status == "refused"


def test_unknown_takes_bad_bodies_and_routes_are_refused(
    layout: AssetLayout, catalog: Catalog, work: Path, tmp_path: Path
) -> None:
    take = published(layout, catalog, work)
    decision = f"/api/v1/corpus/takes/{take.session_id}/decision"
    body = {"request_id": rid(1), "decision": "kept", "expected_revision": 0}
    with serving(layout, tmp_path) as (port, _routes):
        for take_id in ("take-" + "0" * 64, "not-a-take", "TAKE-" + "a" * 64):
            assert refusal(decide(port, take_id, rid(1), "kept", 0)) == (404, "take_not_found")
            assert refusal(trim(port, take_id, rid(1), None, None, 0)) == (
                404, "take_not_found",
            )  # fmt: skip
        assert refusal(send(port, "POST", decision, b"{not json")) == (400, "invalid_json")
        for change in (
            {"decision": "rejected"},  # a rejection says why
            {"decision": "kept", "reason": "fine"},  # and only a rejection
            {"decision": "restored"},  # not a decision: a restore sends the one before
            {"request_id": "req-1"},
            {"request_id": rid(1).upper()},
            {"expected_revision": -1},
            {"expected_revision": "0"},
            {"take_id": take.session_id},
            {"reason": ""},
        ):
            assert refusal(send(port, "POST", decision, body | change)) == (
                422, "invalid_request",
            ), change  # fmt: skip
        assert refusal(send(port, "POST", decision, b"x" * 9000)) == (413, "body_too_large")
        assert refusal(send(port, "GET", decision)) == (405, "method_not_allowed")
        assert refusal(send(port, "PUT", decision, body)) == (405, "method_not_allowed")
        assert refusal(send(port, "POST", decision.replace("decision", "trim"), body)) == (
            405, "method_not_allowed",
        )  # fmt: skip
        for request_id in (rid(9), "req-x", "%2e%2e"):
            path = f"/api/v1/corpus/requests/{request_id}"
            assert refusal(send(port, "GET", path)) == (404, "request_not_found")
        assert refusal(send(port, "POST", f"/api/v1/corpus/requests/{rid(9)}", body)) == (
            405, "method_not_allowed",
        )  # fmt: skip
        assert waiting_files(layout) == []
        # The model the server reads is the published contract's.
        TakeDecisionRequest.model_validate_json(json.dumps(body))
        TakeTrimRequest.model_validate_json(
            json.dumps({"request_id": rid(1), "start_frame": None, "end_frame": None,
                        "expected_revision": 0})
        )  # fmt: skip


def test_the_queue_is_bounded(
    layout: AssetLayout, catalog: Catalog, work: Path, tmp_path: Path
) -> None:
    take = published(layout, catalog, work)
    with serving(layout, tmp_path) as (port, routes):
        routes.requests.max_queued = 2
        accepted(decide(port, take.session_id, rid(1), "kept", 0))
        accepted(decide(port, take.session_id, rid(2), "removed", 1))
        status, headers, body = decide(port, take.session_id, rid(3), "kept", 2)
        assert (status, json.loads(body)["error"]) == (503, "request_queue_full")
        assert headers["retry-after"] == "60"
        # A request already queued still answers; once the publisher drains, the queue opens.
        assert accepted(decide(port, take.session_id, rid(2), "removed", 1)).status == "queued"
        assert a_pass(layout).requests_applied == [rid(1), rid(2)]
        assert accepted(decide(port, take.session_id, rid(3), "kept", 2)).status == "queued"


# -- the publisher ------------------------------------------------------------------------------


def test_requests_of_a_take_apply_in_the_order_they_arrived(
    layout: AssetLayout, catalog: Catalog, work: Path, tmp_path: Path
) -> None:
    take = published(layout, catalog, work)
    # The API's clock steps back between the two: the second is still queued after the first.
    clock = Ticker(NOW + timedelta(seconds=10), NOW + timedelta(seconds=5))
    with serving(layout, tmp_path, clock) as (port, _routes):
        first = accepted(decide(port, take.session_id, rid(9), "kept", 0))
        second = accepted(decide(port, take.session_id, rid(1), "removed", 1))
        assert first.received_at is not None and second.received_at is not None
        assert second.received_at < first.received_at
        names = waiting_files(layout)
        assert [parse_request_name(name) for name in names] == list(queued_requests(layout))
        assert [entry.request_id for entry in queued_requests(layout)] == [rid(9), rid(1)]
        summary = a_pass(layout)
        assert summary.requests_applied == [rid(9), rid(1)]
        outcomes = [request_status(port, rid(n)).outcome for n in (9, 1)]
        assert [outcome.revision for outcome in outcomes if outcome] == [1, 2]
        decision = state_of(layout, take).decision
        assert (decision.decision, decision.revision) == ("removed", 2)


def test_a_request_for_a_take_on_its_way_waits_for_it(
    layout: AssetLayout, catalog: Catalog, work: Path, tmp_path: Path
) -> None:
    take = make_take(work / "take", catalog=catalog)
    take_id = take.session_id
    uploads = layout.directory("incoming") / ".uploads" / take_id
    with serving(layout, tmp_path) as (port, _routes):
        # Still uploading: the request is queued, and waits with every later one of its take.
        uploads.mkdir(parents=True)
        accepted(decide(port, take_id, rid(1), "kept", 0))
        accepted(trim(port, take_id, rid(2), 0, 4800, 0))
        accepted(decide(port, take_id, rid(3), "removed", 1))
        summary = a_pass(layout)
        assert summary.requests_applied == [] and summary.requests_refused == []
        assert [entry["request"] for entry in summary.requests_waiting] == [rid(1), rid(2), rid(3)]
        assert request_status(port, rid(2)).status == "queued"
        # Finalized, but its catalog's snapshot is damaged here: the take is deferred, so the
        # requests still wait.
        shutil.rmtree(uploads)
        os.rename(work / "take", layout.directory("incoming") / take_id)
        snapshot = layout.root / catalog[0].snapshot_path
        original, mode = snapshot.read_bytes(), stat.S_IMODE(snapshot.stat().st_mode)
        os.chmod(snapshot, 0o600)
        snapshot.write_bytes(original.replace(b"quiet", b"QUIET", 1))
        summary = a_pass(layout)
        assert [entry["session"] for entry in summary.deferred] == [take_id]
        assert len(summary.requests_waiting) == 3 and len(waiting_files(layout)) == 3
        # Repaired: the take is published and recorded, then its requests apply in order.
        snapshot.write_bytes(original)
        os.chmod(snapshot, mode)
        summary = a_pass(layout)
        assert [receipt.session_id for receipt in summary.accepted] == [take_id]
        assert summary.takes_recorded == [take_id]
        assert summary.requests_applied == [rid(1), rid(2), rid(3)]
        state = state_of(layout, take)
        assert (state.decision.decision, state.decision.revision, state.trim.revision) == (
            "removed", 2, 1,
        )  # fmt: skip
        [listed] = history(port, take).takes
        assert (listed.decision, listed.trim.end_frame) == ("removed", 4800)


def test_a_request_for_a_take_the_pc_refused_is_refused(
    layout: AssetLayout, catalog: Catalog, work: Path, tmp_path: Path
) -> None:
    take = make_take(work / "take", catalog=catalog, simulated=True)
    # Its upload session stays, finalized, after the package moved on (api.staging).
    session = layout.directory("incoming") / UPLOADS_NAME / take.session_id
    session.mkdir(parents=True)
    (session / FINALIZED_NAME).write_bytes(b"finalized\n")
    assert (UPLOADS_NAME, FINALIZED_NAME) == (staging.UPLOADS_NAME, staging.FINALIZED_NAME)
    with serving(layout, tmp_path) as (port, _routes):
        os.rename(work / "take", layout.directory("incoming") / take.session_id)
        accepted(decide(port, take.session_id, rid(1), "kept", 0))
        summary = a_pass(layout)
        assert [record.session_id for record in summary.refused] == [take.session_id]
        assert summary.requests_refused == [
            {"request": rid(1), "take": take.session_id, "code": "take_not_found"}
        ]
        outcome = request_status(port, rid(1)).outcome
        assert outcome is not None and outcome.revision is None
        # The same request again answers as before; a new one is refused at once.
        assert accepted(decide(port, take.session_id, rid(1), "kept", 0)).status == "refused"
        assert refusal(decide(port, take.session_id, rid(2), "kept", 0)) == (
            404, "take_not_found",
        )  # fmt: skip


def queued(layout: AssetLayout, request_id: str, take_id: str, at: datetime) -> Path:
    request = CorpusRequest(
        request_id=request_id,
        take_id=take_id,
        received_at=at,
        change=DecisionChange(decision="kept", reason=None, expected_revision=0),
    )
    return layout.directory("incoming") / queue_request(layout, request, nanoseconds(at))


def test_unsafe_request_files_are_set_aside_without_blocking_the_pass(
    layout: AssetLayout, catalog: Catalog, work: Path, tmp_path: Path
) -> None:
    take = published(layout, catalog, work)
    incoming = layout.directory("incoming")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    def name(number: int) -> Path:
        return incoming / request_name(rid(number), nanoseconds(NOW) + number)

    good = queued(layout, rid(1), take.session_id, NOW)
    payload = good.read_bytes()
    name(2).write_bytes(b"{not json\n")
    name(3).write_bytes(payload.replace(b'{"change"', b'{ "change"'))  # not canonical
    name(4).write_bytes(payload)  # named for another request
    (elsewhere / "linked.json").write_bytes(payload.replace(rid(1).encode(), rid(5).encode()))
    os.symlink(elsewhere / "linked.json", name(5))
    (elsewhere / "hard.json").write_bytes(payload.replace(rid(1).encode(), rid(6).encode()))
    os.link(elsewhere / "hard.json", name(6))
    os.mkfifo(name(7))
    name(8).write_bytes(b" " * (corpus_requests.MAX_REQUEST_BYTES + 1))
    (incoming / f"{corpus_requests.TEMPORARY_PREFIX}{'a' * 32}").write_bytes(b"half")
    outcome: list[PublishSummary] = []
    worker = threading.Thread(target=lambda: outcome.append(a_pass(layout)), daemon=True)
    worker.start()
    worker.join(timeout=30)
    assert not worker.is_alive(), "a request file blocked the intake pass"
    [summary] = outcome
    assert summary.requests_applied == [rid(1)]
    assert [entry["request"] for entry in summary.requests_refused] == [
        rid(number) for number in range(2, 9)
    ]
    assert {entry["code"] for entry in summary.requests_refused} == {"invalid_request"}
    set_aside = sorted(os.listdir(incoming / ".refused"))
    assert [entry.split(".json.")[0] for entry in set_aside] == [
        name(number).name.removeprefix(".").removesuffix(".json") for number in range(2, 9)
    ]
    assert stat.S_ISFIFO((incoming / ".refused" / set_aside[5]).lstat().st_mode)
    for number in range(2, 9):
        record = read_outcome(layout, rid(number))
        assert record is not None and record.take_id is None and record.change is None
        assert (record.status, record.code) == ("refused", "invalid_request")
    # Only the API's temporary stays: the publisher never takes one.
    assert waiting_files(layout) == [f"{corpus_requests.TEMPORARY_PREFIX}{'a' * 32}"]
    assert (elsewhere / "linked.json").exists()
    assert state_of(layout, take).decision.revision == 1


def test_an_interrupted_pass_is_finished_from_the_outcome(
    layout: AssetLayout, catalog: Catalog, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    take = published(layout, catalog, work)
    queued(layout, rid(1), take.session_id, NOW)
    later = CorpusRequest(
        request_id=rid(2),
        take_id=take.session_id,
        received_at=NOW + timedelta(seconds=1),
        change=TrimChange(start_frame=0, end_frame=100, expected_revision=0),
    )
    queue_request(layout, later, nanoseconds(later.received_at))

    def broken(*_args: object, **_options: object) -> str:
        raise OSError("the disk hiccuped")

    # The outcome is written, then the record cannot be: the request and the take's later
    # requests wait, the record is unchanged, and the read model is still built.
    monkeypatch.setattr(corpus_requests, "write_canonical_json", broken)
    summary = a_pass(layout)
    assert summary.requests_applied == [] and summary.read_model == "current"
    assert [entry["request"] for entry in summary.requests_waiting] == [rid(1), rid(2)]
    assert summary.requests_waiting[0]["reason"] == "the disk hiccuped"
    first = read_outcome(layout, rid(1))
    assert first is not None and (first.status, first.revision) == ("applied", 1)
    assert state_of(layout, take).decision.revision == 0
    monkeypatch.undo()
    # The next pass makes the record what the outcome says, once, and goes on.
    summary = a_pass(layout)
    assert summary.requests_applied == [rid(1), rid(2)]
    state = state_of(layout, take)
    assert (state.decision.revision, state.decision.decided_at) == (1, first.decided_at)
    assert state.trim.revision == 1
    assert read_outcome(layout, rid(1)) == first
    assert waiting_files(layout) == []


def test_a_spent_request_id_is_answered_once(
    layout: AssetLayout, catalog: Catalog, work: Path
) -> None:
    take = published(layout, catalog, work)
    queued(layout, rid(1), take.session_id, NOW)
    assert a_pass(layout).requests_applied == [rid(1)]
    first = read_outcome(layout, rid(1))
    record = outcome_path(layout, rid(1)).read_bytes()
    # The same request again (a retry the API raced) is settled without a second change; the
    # id with another change is set aside, and the outcome stands.
    queued(layout, rid(1), take.session_id, NOW + timedelta(seconds=1))
    other = CorpusRequest(
        request_id=rid(1),
        take_id=take.session_id,
        received_at=NOW + timedelta(seconds=2),
        change=DecisionChange(decision="removed", reason=None, expected_revision=1),
    )
    queue_request(layout, other, nanoseconds(other.received_at))
    summary = a_pass(layout)
    assert summary.requests_applied == [rid(1)]
    assert summary.requests_refused == [
        {"request": rid(1), "take": take.session_id, "code": "invalid_request"}
    ]
    assert outcome_path(layout, rid(1)).read_bytes() == record and first is not None
    assert state_of(layout, take).decision.revision == 1
    assert waiting_files(layout) == []
    assert len(os.listdir(layout.directory("incoming") / ".refused")) == 1


def test_the_api_writes_only_its_requests_and_never_opens_the_store(
    layout: AssetLayout,
    catalog: Catalog,
    work: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    take = published(layout, catalog, work)
    a_pass(layout)

    def tree() -> dict[str, tuple[int, int, int]]:
        found: dict[str, tuple[int, int, int]] = {}
        for path in layout.root.rglob("*"):
            relative = path.relative_to(layout.root).as_posix()
            if relative.split("/")[0] != "incoming":
                metadata = path.lstat()
                found[relative] = (metadata.st_mode, metadata.st_size, metadata.st_mtime_ns)
        return found

    def opened(*_args: object, **_options: object) -> object:
        raise AssertionError("the API opened the corpus store")

    before = tree()
    monkeypatch.setattr(storage_binding, "open_catalog_reader", opened)
    with serving(layout, tmp_path) as (port, _routes):
        accepted(decide(port, take.session_id, rid(1), "rejected", 0, reason="a cough"))
        accepted(trim(port, take.session_id, rid(2), 0, 100, 0))
        refusal(decide(port, take.session_id, rid(1), "kept", 0))
        refusal(trim(port, take.session_id, rid(3), 0, MAX_TAKE_FRAMES, 0))
        refusal(decide(port, "take-" + "1" * 64, rid(4), "kept", 0))
        request_status(port, rid(2))
        send(port, "GET", "/api/v1/corpus/library")
    assert tree() == before
    assert waiting_files(layout) == [
        request_name(rid(1), nanoseconds(NOW + timedelta(milliseconds=1))),
        request_name(rid(2), nanoseconds(NOW + timedelta(milliseconds=2))),
    ]
    monkeypatch.undo()
    assert a_pass(layout).requests_applied == [rid(1), rid(2)]


def test_the_publish_command_prints_the_requests(
    layout: AssetLayout,
    catalog: Catalog,
    work: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    take = published(layout, catalog, work)
    queued(layout, rid(1), take.session_id, NOW)
    stale = CorpusRequest(
        request_id=rid(2),
        take_id=take.session_id,
        received_at=NOW + timedelta(seconds=1),
        change=DecisionChange(decision="kept", reason=None, expected_revision=7),
    )
    queue_request(layout, stale, nanoseconds(stale.received_at))
    waiting = make_take(work / "waiting", catalog=catalog, seed=3, frequency=330.0)
    (layout.directory("incoming") / ".uploads" / waiting.session_id).mkdir(parents=True)
    queued(layout, rid(3), waiting.session_id, NOW + timedelta(seconds=2))
    real = intake_publisher.publish_pending
    monkeypatch.setattr(
        cli_home_host, "publish_pending", lambda chosen: real(chosen, min_free_bytes=0)
    )
    capsys.readouterr()
    assert main(["intake", "publish", "--asset-root", str(layout.root)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["requests_applied"] == [rid(1)]
    assert printed["requests_refused"] == [
        {"request": rid(2), "take": take.session_id, "code": "revision_conflict"}
    ]
    assert printed["requests_waiting"] == [
        {
            "request": rid(3),
            "take": waiting.session_id,
            "reason": "the take is not recorded on the PC yet",
        }
    ]


# -- contracts and classification --------------------------------------------------------------


def test_requests_and_outcomes_hold_their_rules() -> None:
    with pytest.raises(ValueError, match="only a rejection"):
        DecisionChange(decision="rejected", reason=None, expected_revision=0)
    with pytest.raises(ValueError, match="only a rejection"):
        DecisionChange(decision="removed", reason="why", expected_revision=0)
    with pytest.raises(ValueError, match="both ends"):
        TrimChange(start_frame=None, end_frame=4, expected_revision=0)
    with pytest.raises(ValueError, match="at least one frame"):
        TrimChange(start_frame=4, end_frame=4, expected_revision=0)
    with pytest.raises(ValueError, match="explicit timezone"):
        CorpusRequest(
            request_id=rid(1),
            take_id="take-" + "a" * 64,
            received_at=datetime(2026, 10, 12, 12, 0),
            change=TrimChange(start_frame=None, end_frame=None, expected_revision=0),
        )
    common: dict[str, Any] = {
        "request_id": rid(1),
        "take_id": "take-" + "a" * 64,
        "change": DecisionChange(decision="kept", reason=None, expected_revision=0),
        "received_at": NOW,
        "decided_at": NOW,
    }
    for fields, problem in (
        ({"status": "applied", "code": None, "detail": None, "revision": None}, "names its take"),
        ({"status": "applied", "code": "take_not_found", "detail": "x", "revision": 1}, "no "),
        ({"status": "refused", "code": None, "detail": None, "revision": 0}, "says why"),
        (
            {"status": "refused", "code": "take_not_found", "detail": "x", "revision": None,
             "take_id": None},
            "or none",
        ),
        (
            {"status": "refused", "code": "take_not_found", "detail": "x", "revision": None,
             "take_id": None, "change": None, "received_at": None},
            "unreadable",
        ),
    ):  # fmt: skip
        with pytest.raises(ValueError, match=problem):
            CorpusRequestOutcome(**(common | fields))
    stamp = nanoseconds(NOW) + 123
    name = request_name(rid(1), stamp)
    assert name == f".corpus-request-20261012T120000000000123Z-{rid(1)}.json"
    parsed = parse_request_name(name)
    assert parsed is not None and (parsed.request_id, parsed.stamp_ns) == (rid(1), stamp)
    for other in (
        f"{corpus_requests.TEMPORARY_PREFIX}{'a' * 32}",
        name.removesuffix(".json"),
        name.replace("20261012", "20261399"),
        "take-" + "a" * 64,
    ):
        assert parse_request_name(other) is None


def test_requests_and_outcomes_are_classified() -> None:
    outcome = classify(f"provenance/corpus-requests/{rid(1)}.json")
    assert (outcome.rule.name, outcome.rule.disposition) == ("corpus-request-outcomes", "records")
    for path in (
        f"incoming/{request_name(rid(1), nanoseconds(NOW))}",
        f"incoming/{corpus_requests.TEMPORARY_PREFIX}{'a' * 32}",
        f"incoming/.refused/corpus-request-20261012T120000000000000Z-{rid(1)}.json.x",
    ):
        assert (classify(path).rule.name, classify(path).copied) == ("incoming-sessions", False)
    temporary = classify(f"provenance/corpus-requests/.{rid(1)}.json.tmp-0123456789abcdef")
    assert temporary.rule.disposition == "mac-only"


def test_the_vault_reads_the_outcomes_before_the_takes(
    layout: AssetLayout, catalog: Catalog, work: Path
) -> None:
    from cole_voice_lab.library_replication import ReplicationItem, library_sources

    take = published(layout, catalog, work)
    queued(layout, rid(1), take.session_id, NOW)
    a_pass(layout)
    names = [source.name for source in library_sources()]
    assert names.index("corpus-requests") < names.index("corpus-takes")
    [source] = [source for source in library_sources() if source.name == "corpus-requests"]
    paths = [item.path for item in source.items(layout) if isinstance(item, ReplicationItem)]
    assert paths == [f"provenance/corpus-requests/{rid(1)}.json"]
    assert state_path(layout, take.session_id).exists()


# -- failures the publisher and the API survive -------------------------------------------------


def route_request(
    method: str, path: str, body: dict[str, Any] | None = None, *, length: bool = True
) -> RouteRequest:
    payload = b"" if body is None else json.dumps(body).encode()

    def read_body(limit: int) -> bytes:
        assert len(payload) <= limit
        return payload

    return RouteRequest(
        method=method,
        path=path,
        query="",
        header=lambda _name: None,
        content_length=len(payload) if length else None,
        read_body=read_body,
    )


def refused_by(routes: CorpusRoutes, request: RouteRequest) -> tuple[int, str]:
    with pytest.raises(RouteRefusal) as refused:
        routes(request)
    return refused.value.status, refused.value.code


def test_the_api_refuses_what_it_cannot_read_or_write(
    layout: AssetLayout, catalog: Catalog, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    take = published(layout, catalog, work)
    routes = CorpusRoutes(layout, clock=Ticker())
    path = f"/api/v1/corpus/takes/{take.session_id}/decision"
    body = {"request_id": rid(1), "decision": "kept", "expected_revision": 0}
    assert refused_by(routes, route_request("POST", path, body, length=False)) == (
        411, "content_length_required",
    )  # fmt: skip

    def broken(*_args: object, **_options: object) -> object:
        raise OSError("the disk hiccuped")

    with monkeypatch.context() as patched:
        patched.setattr(corpus_request_routes, "queue_request", broken)
        assert refused_by(routes, route_request("POST", path, body)) == (500, "internal_error")
    assert waiting_files(layout) == []
    with monkeypatch.context() as patched:
        calls = iter((list, broken))
        real = corpus_request_routes.queued_requests
        patched.setattr(
            corpus_request_routes,
            "queued_requests",
            lambda chosen: real(chosen) if next(calls) is list else broken(),
        )
        assert refused_by(routes, route_request("POST", path, body)) == (500, "internal_error")
    # A request file the API wrote but cannot read back, or a damaged outcome: 500.
    answer = routes(route_request("POST", path, body))
    assert answer is not None and answer.status == 202
    [name] = waiting_files(layout)
    (layout.directory("incoming") / name).write_bytes(b"{}\n")
    status_path = f"/api/v1/corpus/requests/{rid(1)}"
    assert refused_by(routes, route_request("GET", status_path)) == (500, "internal_error")
    (layout.directory("incoming") / name).unlink()
    outcome_path(layout, rid(1)).parent.mkdir(mode=0o700)
    outcome_path(layout, rid(1)).write_bytes(b"{}\n")
    assert refused_by(routes, route_request("GET", status_path)) == (500, "internal_error")
    # A take record that fails its checks, or names another subject.
    record = state_path(layout, take.session_id)
    original = record.read_bytes()
    record.write_bytes(original.replace(b'{"accepted_at"', b'{ "accepted_at"'))
    other = {**body, "request_id": rid(2)}
    assert refused_by(routes, route_request("POST", path, other)) == (500, "take_invalid")
    record.unlink()
    state = CorpusTakeState.model_validate_json(original)
    write_canonical_json(record, state.model_copy(update={"subject_id": "someone-else"}))
    assert refused_by(routes, route_request("POST", path, other)) == (404, "take_not_found")


def test_the_publisher_waits_when_the_take_store_or_the_queue_fails(
    layout: AssetLayout, catalog: Catalog, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    take = published(layout, catalog, work)
    queued(layout, rid(1), take.session_id, NOW)
    queued(layout, rid(2), "take-" + "2" * 64, NOW + timedelta(seconds=1))

    def unrecorded(_layout: AssetLayout) -> object:
        raise ContractError("the take store is damaged")

    # Without every take recorded, a request whose take has no record waits; one whose take
    # has a record is applied; the read model reports the store.
    with monkeypatch.context() as patched:
        patched.setattr(intake_publisher, "record_takes", unrecorded)
        summary = a_pass(layout)
    assert summary.requests_applied == [rid(1)]
    assert [entry["request"] for entry in summary.requests_waiting] == [rid(2)]
    assert summary.read_model == "error: the take store is damaged"
    # Once the store is whole again, the unknown take is refused.
    summary = a_pass(layout)
    assert summary.requests_refused == [
        {"request": rid(2), "take": "take-" + "2" * 64, "code": "take_not_found"}
    ]

    def unlisted(*_args: object, **_options: object) -> object:
        raise OSError("incoming/ cannot be listed")

    with monkeypatch.context() as patched:
        patched.setattr(intake_publisher, "apply_requests", unlisted)
        patched.setattr(intake_publisher, "queued_requests", unlisted)
        summary = a_pass(layout)
    assert summary.requests_waiting == [
        {"request": "", "take": "", "reason": "incoming/ cannot be listed"}
    ]
    assert summary.read_model == "current"


def test_a_request_file_that_cannot_be_read_or_set_aside_waits(
    layout: AssetLayout,
    catalog: Catalog,
    work: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    take = published(layout, catalog, work)
    path = queued(layout, rid(1), take.session_id, NOW)

    def unreadable(*_args: object, **_options: object) -> object:
        raise OSError("an I/O error")

    with monkeypatch.context() as patched:
        patched.setattr(corpus_requests, "read_request_file", unreadable)
        summary = a_pass(layout)
    assert summary.requests_waiting == [{"request": rid(1), "take": "", "reason": "an I/O error"}]
    assert path.exists()
    # A link where the refused directory belongs: nothing is moved through it.
    path.write_bytes(b"{not json\n")
    (tmp_path / "elsewhere").mkdir()
    os.symlink(tmp_path / "elsewhere", layout.directory("incoming") / ".refused")
    summary = a_pass(layout)
    [waiting] = summary.requests_waiting
    assert waiting["request"] == rid(1) and "is a link" in waiting["reason"]
    assert path.exists() and os.listdir(tmp_path / "elsewhere") == []
    assert read_outcome(layout, rid(1)) is None


def test_settled_outcomes_and_records_the_publisher_cannot_finish(
    layout: AssetLayout, catalog: Catalog, work: Path
) -> None:
    take = published(layout, catalog, work)
    # A refused request queued again (a retry the API raced) is answered by its outcome.
    stale = CorpusRequest(
        request_id=rid(1),
        take_id=take.session_id,
        received_at=NOW,
        change=DecisionChange(decision="kept", reason=None, expected_revision=3),
    )
    queue_request(layout, stale, nanoseconds(NOW))
    assert a_pass(layout).requests_refused[0]["code"] == "revision_conflict"
    queue_request(layout, stale, nanoseconds(NOW) + 1)
    summary = a_pass(layout)
    assert summary.requests_refused == [
        {"request": rid(1), "take": take.session_id, "code": "revision_conflict"}
    ]
    assert waiting_files(layout) == []
    # An applied outcome whose take has no record cannot be finished: it waits.
    lost = "take-" + "3" * 64
    request = CorpusRequest(
        request_id=rid(2),
        take_id=lost,
        received_at=NOW,
        change=DecisionChange(decision="kept", reason=None, expected_revision=0),
    )
    payload = canonical_json_bytes(
        CorpusRequestOutcome(
            request_id=rid(2),
            take_id=lost,
            change=request.change,
            received_at=NOW,
            status="applied",
            code=None,
            detail=None,
            revision=1,
            decided_at=NOW,
        )
    )
    outcome_path(layout, rid(2)).write_bytes(payload + b"\n")
    queue_request(layout, request, nanoseconds(NOW) + 2)
    [waiting] = a_pass(layout).requests_waiting
    assert waiting["request"] == rid(2) and "has no record" in waiting["reason"]


def test_request_files_and_outcomes_are_read_strictly(layout: AssetLayout, tmp_path: Path) -> None:
    request = CorpusRequest(
        request_id=rid(1),
        take_id="take-" + "a" * 64,
        received_at=NOW,
        change=TrimChange(start_frame=None, end_frame=None, expected_revision=0),
    )
    name = queue_request(layout, request, nanoseconds(NOW))
    with pytest.raises(FileExistsError):
        queue_request(layout, request, nanoseconds(NOW))
    assert waiting_files(layout) == [name]  # the temporary is gone
    with pytest.raises(ValueError, match="not a request id"):
        outcome_path(layout, "req-1")
    path = outcome_path(layout, rid(1))
    path.parent.mkdir(mode=0o700)
    outcome = CorpusRequestOutcome(
        request_id=rid(1),
        take_id=None,
        change=None,
        received_at=None,
        status="refused",
        code="invalid_request",
        detail="unreadable",
        revision=None,
        decided_at=NOW,
    )
    payload = canonical_json_bytes(outcome) + b"\n"
    for damage, error in (
        (lambda: path.write_bytes(b" " * (corpus_requests.MAX_OUTCOME_BYTES + 1)), ContractError),
        (lambda: path.write_bytes(b"{}\n"), ContractError),
        (lambda: path.write_bytes(payload.replace(b"{", b"{ ", 1)), ContractError),
        (
            lambda: path.write_bytes(payload.replace(rid(1).encode(), rid(2).encode())),
            ContractError,
        ),
        (lambda: os.symlink(tmp_path / "elsewhere.json", path), SecurityError),
    ):
        if path.is_symlink() or path.exists():
            path.unlink()
        damage()
        with pytest.raises(error):
            read_outcome(layout, rid(1))
    path.unlink()
    path.write_bytes(payload)
    assert read_outcome(layout, rid(1)) == outcome


def test_requests_that_arrive_during_a_pass_are_applied_in_it(
    layout: AssetLayout, catalog: Catalog, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The path unit does not start a pass for what changes while one runs, so the pass looks
    # again: after the requests it listed, and after it rebuilt the library.
    take = published(layout, catalog, work)
    queued(layout, rid(1), take.session_id, NOW)

    def arrive(number: int, decision: str, expected: int) -> None:
        at = NOW + timedelta(seconds=number)
        change = DecisionChange(decision=decision, reason=None, expected_revision=expected)
        request = CorpusRequest(
            request_id=rid(number), take_id=take.session_id, received_at=at, change=change
        )
        queue_request(layout, request, nanoseconds(at))

    real_judge = corpus_requests.judge

    def judging(
        state: CorpusTakeState, request: CorpusRequest, now: datetime
    ) -> tuple[CorpusRequestOutcome, CorpusTakeState | None]:
        if request.request_id == rid(1):
            arrive(2, "removed", 1)
        return real_judge(state, request, now)

    real_refresh = intake_publisher.refresh_library
    rebuilt: list[int] = []

    def refreshing(*args: Any, **options: Any) -> Any:
        rebuilt.append(len(rebuilt))
        if len(rebuilt) == 1:
            arrive(3, "kept", 2)
        return real_refresh(*args, **options)

    monkeypatch.setattr(corpus_requests, "judge", judging)
    monkeypatch.setattr(intake_publisher, "refresh_library", refreshing)
    summary = a_pass(layout)
    assert summary.requests_applied == [rid(1), rid(2), rid(3)]
    assert len(rebuilt) == 2 and waiting_files(layout) == []
    found = read_library(layout, take.manifest.subject_id)
    assert found is not None
    [listed] = [entry for prompt in found[0].histories for entry in prompt.takes]
    assert (listed.decision, listed.decision_revision) == ("kept", 3)
