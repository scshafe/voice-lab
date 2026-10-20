"""The house panel's token: a corpus client's scope, fail-closed (D-056 step 4, HOUSE-PANEL.md §3).

A token whose audience is a client audience (``voice-lab-panel``) is scoped ``corpus-client``.
``verify_header`` keeps its meaning (full access only), so ``/view/``, the journey routes and
their intake refuse it unchanged; the server's ``/api/v1`` check takes it only on the
allow-list, and every other route answers 403 ``client_scope``. Finalize takes only a v2 take
of the panel's profile, and a decision only a take of that profile.
"""

from __future__ import annotations

import http.client
import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from corpus_take_fixtures import make_take, seed_catalog
from home_host_fixtures import AUDIENCE, ISSUER, SUBJECT, jwks, mint
from panel_take_fixtures import make_profile_take
from pydantic import ValidationError

from cole_voice_lab.api.auth import AuthError, AuthPolicy, TokenVerifier, TrustedIssuer
from cole_voice_lab.api.client_scope import client_may
from cole_voice_lab.api.contracts import ApiMeta
from cole_voice_lab.api.corpus_routes import CorpusRoutes
from cole_voice_lab.api.server import ApiServer
from cole_voice_lab.api.staging import IntakeStaging
from cole_voice_lab.intake import validate_package
from cole_voice_lab.intake_client import (
    HomeHostClient,
    UploadRefused,
    _transfer,
    session_request,
)
from cole_voice_lab.intake_publisher import publish_pending
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_contracts import CatalogReference, CatalogSnapshot

type Catalog = tuple[CatalogReference, CatalogSnapshot]
PANEL_AUDIENCE = "voice-lab-panel"
PANEL = "xvf3800-linear-4"
APPROVAL = "Cole approved take playback on the PC, 2026-10-19."


def _policy(
    jwks_path: Path, *, client_audiences: tuple[str, ...] = (PANEL_AUDIENCE,)
) -> AuthPolicy:
    return AuthPolicy(
        issuers=(
            TrustedIssuer(
                issuer=ISSUER,
                audiences=(AUDIENCE,),
                jwks_path=str(jwks_path),
                client_audiences=client_audiences,
            ),
        ),
        allowed_subjects=(SUBJECT,),
    )


def _panel() -> str:
    return mint({"aud": [PANEL_AUDIENCE]})


@pytest.fixture
def jwks_path(tmp_path: Path) -> Path:
    path = tmp_path / "jwks.json"
    path.write_text(json.dumps(jwks()), encoding="utf-8")
    return path


# -- The verifier -----------------------------------------------------------------------------


def test_a_client_audience_scopes_the_token_and_verify_header_refuses_it(
    jwks_path: Path,
) -> None:
    verifier = TokenVerifier(_policy(jwks_path))
    desktop = verifier.verify_header(f"Bearer {mint()}")
    assert desktop.scope == "full"
    panel = verifier.verify_any_header(f"Bearer {_panel()}")
    assert (panel.scope, panel.subject) == ("corpus-client", SUBJECT)
    # Every caller of verify_header (/view/, the journey routes and their intake) refuses it.
    with pytest.raises(AuthError, match="may not use this route"):
        verifier.verify_header(f"Bearer {_panel()}")
    # A token that names a client audience is scoped, whatever else it names.
    both = verifier.verify_any_header(f"Bearer {mint({'aud': [AUDIENCE, PANEL_AUDIENCE]})}")
    assert both.scope == "corpus-client"


def test_a_verifier_without_the_client_audience_refuses_its_token(jwks_path: Path) -> None:
    # The speech routes' verifier knows only their callers' audiences (D-046).
    verifier = TokenVerifier(_policy(jwks_path, client_audiences=()))
    with pytest.raises(AuthError, match="not for this API"):
        verifier.verify_any_header(f"Bearer {_panel()}")


def test_an_audience_is_full_or_a_clients_never_both(jwks_path: Path) -> None:
    with pytest.raises(ValidationError, match="never both"):
        _policy(jwks_path, client_audiences=(AUDIENCE,))


# -- The allow-list -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "query"),
    [
        ("GET", "/api/v1/meta", ""),
        ("GET", "/api/v1/catalog", ""),
        ("HEAD", "/api/v1/catalog", ""),
        ("GET", "/api/v1/corpus/library", f"profile={PANEL}"),
        ("HEAD", "/api/v1/corpus/prompts/harvard-001/takes", f"profile={PANEL}"),
        ("POST", "/api/v1/intake/sessions", ""),
        ("GET", "/api/v1/intake/sessions/take-1", ""),
        ("PUT", "/api/v1/intake/sessions/take-1/files/original.wav", ""),
        ("POST", "/api/v1/intake/sessions/take-1/finalize", ""),
        ("GET", "/api/v1/intake/sessions/take-1/receipt", ""),
        ("POST", "/api/v1/corpus/takes/take-1/decision", ""),
        ("GET", "/api/v1/corpus/requests/req-1", ""),
    ],
)
def test_the_allow_list(method: str, path: str, query: str) -> None:
    assert client_may(method, path, query)


@pytest.mark.parametrize(
    ("method", "path", "query"),
    [
        # Another profile's view, or the default one, and any other query.
        ("GET", "/api/v1/corpus/library", ""),
        ("GET", "/api/v1/corpus/library", "profile=umik-1"),
        ("GET", "/api/v1/corpus/prompts/harvard-001/takes", ""),
        ("GET", "/api/v1/corpus/library", f"profile={PANEL}&x=1"),
        ("GET", "/api/v1/catalog", "x=1"),
        # Audio, trims and anything not on the list.
        ("GET", "/api/v1/corpus/takes/take-1/audio", ""),
        ("PUT", "/api/v1/corpus/takes/take-1/trim", ""),
        ("GET", "/api/v1/corpus/takes/take-1/decision", ""),
        ("POST", "/api/v1/catalog", ""),
        ("DELETE", "/api/v1/intake/sessions/take-1", ""),
        ("PUT", "/api/v1/intake/sessions/take-1/files/a/b", ""),
        ("POST", "/api/v1/intake/sessions/take-1/other", ""),
        ("GET", "/api/v1/journey/chains", ""),
        ("GET", "/api/v1/speech/jobs", ""),
        ("GET", "/api/v1/meta/extra", ""),
    ],
)
def test_everything_else_is_refused(method: str, path: str, query: str) -> None:
    assert not client_may(method, path, query)


# -- The server, end to end -----------------------------------------------------------------------


@pytest.fixture
def layout(tmp_path: Path) -> AssetLayout:
    return AssetLayout.initialize(tmp_path / "srv" / "voice-lab" / "lab")


@pytest.fixture
def catalog(layout: AssetLayout) -> Catalog:
    return seed_catalog(layout)


@contextmanager
def serving(layout: AssetLayout, jwks_path: Path) -> Iterator[int]:
    server = ApiServer(
        ("127.0.0.1", 0),
        staging=IntakeStaging(layout, min_free_bytes=0),
        verifier=TokenVerifier(_policy(jwks_path)),
        meta=ApiMeta(server_build="test-build", minimum_client_version="0.1.0"),
        log=False,
        routes=(CorpusRoutes(layout, playback_approval=APPROVAL),),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()


def _call(
    connection: http.client.HTTPConnection,
    token: str,
    method: str,
    path: str,
    body: bytes = b"",
) -> tuple[int, dict[str, object]]:
    connection.request(
        method,
        path,
        body=body,
        headers={"Authorization": f"Bearer {token}", "Content-Length": str(len(body))},
    )
    response = connection.getresponse()
    data = response.read()
    return response.status, (json.loads(data) if data else {})


def _publish(layout: AssetLayout, package: Path, take_id: str) -> None:
    os.rename(package, layout.directory("incoming") / take_id)
    assert len(publish_pending(layout, min_free_bytes=0).accepted) == 1


def test_the_panel_reaches_its_routes_only(
    layout: AssetLayout, catalog: Catalog, jwks_path: Path, tmp_path: Path
) -> None:
    desktop = make_take(tmp_path / "desktop", catalog=catalog)
    _publish(layout, tmp_path / "desktop", desktop.session_id)
    prompt = desktop.manifest.prompt.prompt_id
    panel = _panel()
    with serving(layout, jwks_path) as port:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        for path in (
            "/api/v1/meta",
            "/api/v1/catalog",
            f"/api/v1/corpus/library?profile={PANEL}",
            f"/api/v1/corpus/prompts/{prompt}/takes?profile={PANEL}",
        ):
            status, _ = _call(connection, panel, "GET", path)
            assert status == 200, path
        for method, path in (
            ("GET", "/api/v1/corpus/library"),
            ("GET", "/api/v1/corpus/library?profile=umik-1"),
            ("GET", f"/api/v1/corpus/prompts/{prompt}/takes"),
            ("GET", f"/api/v1/corpus/takes/{desktop.session_id}/audio"),
            ("PUT", f"/api/v1/corpus/takes/{desktop.session_id}/trim"),
            ("GET", "/api/v1/journey/chains"),
        ):
            status, answer = _call(connection, panel, method, path)
            assert (status, answer.get("error")) == (403, "client_scope"), path
        # On the same connection, the desktop's token is not held to the panel's list: the
        # scope never carries over from one request to the next.
        status, _ = _call(connection, mint(), "GET", "/api/v1/corpus/library")
        assert status == 200
        status, answer = _call(connection, panel, "GET", "/api/v1/corpus/library")
        assert (status, answer.get("error")) == (403, "client_scope")
        # A request id the server never saw: past the allow-list, the route answers.
        status, answer = _call(connection, panel, "GET", "/api/v1/corpus/requests/" + "a" * 32)
        assert status != 403
        connection.close()


def _decide(request_id: str) -> bytes:
    return json.dumps(
        {"request_id": request_id, "decision": "kept", "reason": None, "expected_revision": 0}
    ).encode()


def test_the_panel_decides_only_its_own_profiles_takes(
    layout: AssetLayout, catalog: Catalog, jwks_path: Path, tmp_path: Path
) -> None:
    desktop = make_take(tmp_path / "desktop", catalog=catalog)
    _publish(layout, tmp_path / "desktop", desktop.session_id)
    panel_take = make_profile_take(tmp_path / "panel", catalog=catalog)
    _publish(layout, tmp_path / "panel", panel_take.session_id)
    with serving(layout, jwks_path) as port:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        path = f"/api/v1/corpus/takes/{desktop.session_id}/decision"
        status, answer = _call(connection, _panel(), "POST", path, _decide("req-" + "1" * 32))
        assert (status, answer.get("error")) == (403, "client_scope")
        path = f"/api/v1/corpus/takes/{panel_take.session_id}/decision"
        status, answer = _call(connection, _panel(), "POST", path, _decide("req-" + "2" * 32))
        assert status == 202, answer
        # A take the server has no record of yet: the panel asks again once it is published.
        path = "/api/v1/corpus/takes/take-" + "e" * 64 + "/decision"
        status, answer = _call(connection, _panel(), "POST", path, _decide("req-" + "3" * 32))
        assert (status, answer.get("error")) == (404, "take_not_found")
        connection.close()


def test_finalize_takes_only_a_v2_take_of_the_panels_profile(
    layout: AssetLayout, catalog: Catalog, jwks_path: Path, tmp_path: Path
) -> None:
    desktop = make_take(tmp_path / "desktop", catalog=catalog)
    panel_take = make_profile_take(tmp_path / "panel", catalog=catalog)
    with serving(layout, jwks_path) as port:
        client = HomeHostClient.loopback_for_tests(port, _panel)
        package = validate_package(tmp_path / "desktop")
        with pytest.raises(UploadRefused) as refused:
            _transfer(client, tmp_path / "desktop", package, session_request(package))
        assert refused.value.code == "take_profile_not_allowed"
        assert not (layout.directory("incoming") / desktop.session_id).exists()
        package = validate_package(tmp_path / "panel")
        status = _transfer(client, tmp_path / "panel", package, session_request(package))
        assert status.state == "finalized"
        assert (layout.directory("incoming") / panel_take.session_id).is_dir()
        # The desktop's token still finalizes a desktop take.
        desktop_client = HomeHostClient.loopback_for_tests(port, mint)
        package = validate_package(tmp_path / "desktop")
        status = _transfer(desktop_client, tmp_path / "desktop", package, session_request(package))
        assert status.state == "finalized"
