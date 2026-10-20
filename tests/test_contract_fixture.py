"""The contract fixture: the PC's API and a fake Pocket ID for a corpus client's live check
(D-056 step 5, HOUSE-PANEL.md §4).

The fake behaves as Pocket ID v2.13.0 does where the panel meets it (device code, rotation
without grace, ``aud`` an array); its tokens pass the API's real verifier; and the bundle's
golden take, sent as PROTOCOL.md says, is published, shown in the panel's view and decided.
The switch that admits simulated takes is the fixture's alone.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import re
import signal
import ssl
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from panel_take_fixtures import make_profile_take

from cole_voice_lab.api.auth import AuthPolicy, TokenVerifier, TrustedIssuer
from cole_voice_lab.contract_bundle import (
    BUNDLE_DIRECTORY,
    CLIENT_AUDIENCE,
    fixture_catalog,
    golden_wav,
)
from cole_voice_lab.contract_fixture import (
    DEVICE_CODE_SECONDS,
    FIXTURE_USER,
    NOMINAL_ISSUER,
    TOKEN_SECONDS,
    ContractFixture,
    FakePocketId,
    SigningKey,
    _probable_prime,
    build_fixture,
    main,
)
from cole_voice_lab.recording_contracts import CatalogSnapshot

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / BUNDLE_DIRECTORY
PROFILE = "xvf3800-linear-4"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


@pytest.fixture(scope="module")
def key() -> SigningKey:
    return SigningKey.generate()


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


def _claims(token: str) -> tuple[dict[str, Any], dict[str, Any]]:
    header, claims, _ = token.split(".")
    return json.loads(_unpadded(header)), json.loads(_unpadded(claims))


def _unpadded(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _signed_in(idp: FakePocketId) -> dict[str, Any]:
    _, started = idp.authorize({"client_id": CLIENT_AUDIENCE, "scope": "openid"}, "https://x/d")
    form = {"grant_type": DEVICE_GRANT, "device_code": started["device_code"]}
    form["client_id"] = CLIENT_AUDIENCE
    for _ in range(idp.approve_after):
        assert idp.token(form) == (400, _pending())
    status, tokens = idp.token(form)
    assert status == 200
    return tokens


def _pending() -> dict[str, str]:
    return {"error": "authorization_pending", "error_description": "the user has not approved yet"}


# -- The fake Pocket ID ----------------------------------------------------------------------------


def test_its_tokens_pass_the_apis_verifier_as_the_panels(key: SigningKey, tmp_path: Path) -> None:
    clock = Clock()
    idp = FakePocketId(issuer=NOMINAL_ISSUER, key=key, clock=clock)
    tokens = _signed_in(idp)
    header, claims = _claims(tokens["access_token"])
    assert header == {"alg": "RS256", "typ": "at+jwt", "kid": key.kid}
    assert claims["aud"] == [CLIENT_AUDIENCE]
    assert claims["sub"] == FIXTURE_USER
    assert claims["exp"] - claims["iat"] == TOKEN_SECONDS == tokens["expires_in"]
    jwks = tmp_path / "jwks.json"
    jwks.write_text(json.dumps(key.jwks()))
    verifier = TokenVerifier(
        AuthPolicy(
            issuers=(
                TrustedIssuer(
                    issuer=NOMINAL_ISSUER,
                    audiences=("voice-lab-desktop",),
                    client_audiences=(CLIENT_AUDIENCE,),
                    jwks_path=str(jwks),
                ),
            ),
            allowed_subjects=(FIXTURE_USER,),
        ),
        clock=clock,
    )
    principal = verifier.verify_any_header(f"Bearer {tokens['access_token']}")
    assert principal.scope == "corpus-client"
    assert principal.subject == FIXTURE_USER


def test_device_authorization_refuses_a_resource_and_unknown_clients(key: SigningKey) -> None:
    idp = FakePocketId(issuer=NOMINAL_ISSUER, key=key)
    status, answer = idp.authorize(
        {"client_id": CLIENT_AUDIENCE, "resource": "https://voice-lab"}, "https://x/d"
    )
    assert (status, answer["error"]) == (400, "invalid_request")
    status, answer = idp.authorize({"client_id": "voice-lab-desktop"}, "https://x/d")
    assert (status, answer["error"]) == (400, "invalid_client")
    status, answer = idp.authorize({"client_id": CLIENT_AUDIENCE}, "https://x/d")
    assert status == 200
    assert answer["expires_in"] == DEVICE_CODE_SECONDS
    assert answer["verification_uri_complete"].endswith(answer["user_code"])
    assert re.fullmatch(r"E[A-Z2-9]{7}", answer["user_code"])


def test_polling_stays_pending_without_slow_down_even_past_expiry(key: SigningKey) -> None:
    clock = Clock()
    idp = FakePocketId(issuer=NOMINAL_ISSUER, key=key, clock=clock, approve_after=3)
    _, started = idp.authorize({"client_id": CLIENT_AUDIENCE}, "https://x/d")
    form = {"grant_type": DEVICE_GRANT, "device_code": started["device_code"]}
    form["client_id"] = CLIENT_AUDIENCE
    # Polling faster than the interval earns no slow_down: v2.13 has none.
    assert idp.token(form) == (400, _pending())
    clock.now += DEVICE_CODE_SECONDS
    for _ in range(5):
        assert idp.token(form) == (400, _pending())
    assert idp.token({**form, "client_id": "voice-lab-desktop"})[1]["error"] == "invalid_grant"
    assert idp.token({**form, "device_code": "nope"})[1]["error"] == "invalid_grant"


def test_a_device_code_is_redeemed_once(key: SigningKey) -> None:
    idp = FakePocketId(issuer=NOMINAL_ISSUER, key=key, approve_after=0)
    _, started = idp.authorize({"client_id": CLIENT_AUDIENCE}, "https://x/d")
    form = {"grant_type": DEVICE_GRANT, "device_code": started["device_code"]}
    form["client_id"] = CLIENT_AUDIENCE
    assert idp.token(form)[0] == 200
    assert idp.token(form)[1]["error"] == "invalid_grant"


def test_refresh_rotates_and_a_reused_token_revokes_the_family(key: SigningKey) -> None:
    idp = FakePocketId(issuer=NOMINAL_ISSUER, key=key)
    first = _signed_in(idp)
    refresh = {"grant_type": "refresh_token", "client_id": CLIENT_AUDIENCE}
    status, second = idp.token({**refresh, "refresh_token": first["refresh_token"]})
    assert status == 200
    assert second["refresh_token"] != first["refresh_token"]
    assert _claims(second["access_token"])[1]["aud"] == [CLIENT_AUDIENCE]
    # The rotated token used again: refused, and the family's current token with it.
    status, answer = idp.token({**refresh, "refresh_token": first["refresh_token"]})
    assert (status, answer["error"]) == (400, "invalid_grant")
    status, answer = idp.token({**refresh, "refresh_token": second["refresh_token"]})
    assert (status, answer["error"]) == (400, "invalid_grant")
    # A new sign-in is a new family.
    third = _signed_in(idp)
    assert idp.token({**refresh, "refresh_token": third["refresh_token"]})[0] == 200
    assert idp.token({**refresh, "refresh_token": "unknown"})[1]["error"] == "invalid_grant"
    assert idp.token({"grant_type": "password"})[1]["error"] == "unsupported_grant_type"


def test_the_primality_test() -> None:
    # 561 is a Carmichael number: Fermat's test passes it, Miller-Rabin does not.
    assert [n for n in range(-2, 40) if _probable_prime(n)] == [
        2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37,
    ]  # fmt: skip
    assert not _probable_prime(561)
    assert _probable_prime(2**127 - 1)
    assert not _probable_prime((2**61 - 1) * (2**31 - 1))


def test_the_signing_key_is_a_valid_rsa_key(key: SigningKey) -> None:
    assert key.modulus.bit_length() == 2048
    assert pow(pow(12345, key.private, key.modulus), key.exponent, key.modulus) == 12345
    assert "private" not in repr(key)
    public = rsa.RSAPublicNumbers(key.exponent, key.modulus).public_key()
    data = b"signed by the fixture"
    public.verify(key.sign(data), data, padding.PKCS1v15(), hashes.SHA256())


# -- The fixture, live ---------------------------------------------------------------------------


@contextmanager
def running(root: Path, key: SigningKey, **options: Any) -> Iterator[ContractFixture]:
    root.mkdir()
    fixture = build_fixture(root, key=key, interval=1, **options)
    fixture.start()
    try:
        yield fixture
    finally:
        fixture.close()


class Http:
    """One client connection to a URL of the fixture's report."""

    def __init__(self, url: str, context: ssl.SSLContext | None = None) -> None:
        parts = urlsplit(url)
        host, port = parts.hostname or "", parts.port or 0
        self.connection = (
            http.client.HTTPSConnection(host, port, context=context, timeout=60)
            if parts.scheme == "https"
            else http.client.HTTPConnection(host, port, timeout=60)
        )
        self.token: str | None = None

    def call(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[int, Any]:
        sent = dict(headers or {})
        if self.token is not None:
            sent["Authorization"] = f"Bearer {self.token}"
        self.connection.request(method, path, body=body, headers=sent)
        response = self.connection.getresponse()
        payload = response.read()
        kind = response.getheader("Content-Type") or ""
        return response.status, json.loads(payload) if "json" in kind and payload else payload

    def form(self, path: str, fields: Mapping[str, str]) -> tuple[int, Any]:
        content = {"Content-Type": "application/x-www-form-urlencoded"}
        return self.call("POST", path, urlencode(fields).encode(), content)


def _sign_in(fixture: ContractFixture, context: ssl.SSLContext | None = None) -> Http:
    """The device flow over HTTP, as the panel runs it from the discovery document."""
    oidc = fixture.report["oidc"]
    idp = Http(oidc["token_endpoint"], context)
    assert idp.call("GET", "/.well-known/openid-configuration") == (200, oidc)
    status, jwks = idp.call("GET", "/.well-known/jwks.json")
    assert (status, jwks) == (200, fixture.idp.key.jwks())
    status, started = idp.form(
        urlsplit(oidc["device_authorization_endpoint"]).path,
        {"client_id": CLIENT_AUDIENCE, "scope": "openid profile"},
    )
    assert status == 200
    assert started["verification_uri"].startswith(urlsplit(oidc["token_endpoint"]).scheme)
    token_path = urlsplit(oidc["token_endpoint"]).path
    poll = {"grant_type": DEVICE_GRANT, "device_code": started["device_code"]}
    poll["client_id"] = CLIENT_AUDIENCE
    assert idp.form(token_path, poll) == (400, _pending())
    status, tokens = idp.form(token_path, poll)
    assert status == 200
    assert idp.call("POST", "/elsewhere", b"")[0] == 404
    assert idp.call("GET", "/elsewhere")[0] == 404
    assert idp.call("POST", token_path, b"x" * 20_000)[0] == 413
    api = Http(fixture.report["api_url"], context)
    api.token = tokens["access_token"]
    return api


def _golden_files() -> dict[str, bytes]:
    take = BUNDLE / "examples" / "take"
    files = {
        path.name: path.read_bytes()
        for path in take.iterdir()
        if path.name != "original.wav.recipe.json"
    }
    files["original.wav"] = golden_wav()
    return files


def _upload(api: Http, request: bytes, files: Mapping[str, bytes]) -> dict[str, Any]:
    """PROTOCOL.md §4: declare, send each file in two ranges, finalize, read the outcome."""
    declared = json.loads(request)
    session = declared["session_id"]
    json_type = {"Content-Type": "application/json"}
    status, opened = api.call("POST", "/api/v1/intake/sessions", request, json_type)
    assert status == 201, opened
    for name, data in sorted(files.items()):
        half = len(data) // 2
        for start, end in ((0, half), (half, len(data))):
            status, _ = api.call(
                "PUT",
                f"/api/v1/intake/sessions/{session}/files/{name}",
                data[start:end],
                {"Content-Range": f"bytes {start}-{end - 1}/{len(data)}"},
            )
            assert status == (200 if end == len(data) else 202)
    status, finalized = api.call("POST", f"/api/v1/intake/sessions/{session}/finalize", b"")
    assert (status, finalized["state"]) == (200, "finalized")
    # The fixture's publisher ran within finalize: the outcome is there already.
    status, outcome = api.call("GET", f"/api/v1/intake/sessions/{session}")
    assert status == 200
    return outcome  # type: ignore[no-any-return]


def _row(api: Http, prompt_id: str) -> dict[str, Any]:
    status, library = api.call("GET", f"/api/v1/corpus/library?profile={PROFILE}")
    assert status == 200
    assert library["profile"] == PROFILE
    rows: list[dict[str, Any]] = library["rows"]
    return next(row for row in rows if row["prompt_id"] == prompt_id)


def test_the_golden_take_is_published_shown_and_decided(key: SigningKey, tmp_path: Path) -> None:
    with running(tmp_path / "fixture", key) as fixture:
        api = _sign_in(fixture)
        status, meta = api.call("GET", "/api/v1/meta")
        assert (status, meta["server_build"]) == (200, "contract-fixture")
        status, catalog = api.call("GET", "/api/v1/catalog")
        reference, _ = fixture_catalog()
        assert (status, catalog["reference"]) == (200, reference.model_dump(mode="json"))
        assert fixture.report["catalog"] == catalog["reference"]
        # The default view stays out of a corpus client's reach.
        status, refusal = api.call("GET", "/api/v1/corpus/library")
        assert (status, refusal["error"]) == (403, "client_scope")
        assert _row(api, "reference-neutral-01")["status"] == "missing"
        request = (BUNDLE / "examples" / "intake-session-request.json").read_bytes()
        outcome = _upload(api, request, _golden_files())
        assert outcome["state"] == "accepted", outcome
        take_id = json.loads(request)["session_id"]
        status, receipt = api.call("GET", f"/api/v1/intake/sessions/{take_id}/receipt")
        assert (status, receipt) == (200, outcome["receipt"])
        row = _row(api, "reference-neutral-01")
        assert (row["status"], row["latest_take_id"]) == ("draft", take_id)
        decision = (BUNDLE / "examples" / "take-decision-request.json").read_bytes()
        status, queued = api.call(
            "POST",
            f"/api/v1/corpus/takes/{take_id}/decision",
            decision,
            {"Content-Type": "application/json"},
        )
        assert (status, queued["status"]) == (202, "queued")
        status, applied = api.call("GET", f"/api/v1/corpus/requests/{queued['request_id']}")
        assert (status, applied["status"]) == (200, "applied"), applied
        assert _row(api, "reference-neutral-01")["status"] == "kept"
        # Only the panel's subject is the fixture's user: the desktop's audience is full.
        status, refusal = api.call("GET", f"/api/v1/corpus/takes/{take_id}/audio")
        assert (status, refusal["error"]) == (403, "client_scope")


def _simulated_take(tmp_path: Path) -> tuple[bytes, dict[str, bytes]]:
    reference, suite = fixture_catalog()
    take = make_profile_take(
        tmp_path / "simulated",
        catalog=(reference, CatalogSnapshot.embedded(suite)),
        simulated=True,
    )
    files = {
        path.name: path.read_bytes()
        for path in (tmp_path / "simulated").iterdir()
        if path.is_file()
    }
    request = {
        "schema_version": "cole-voice-lab.api.intake-session/v1",
        "session_id": take.ready.session_id,
        "manifest_sha256": take.manifest_sha256,
        "files": [
            {"path": name, "sha256": _sha256(data), "size_bytes": len(data)}
            for name, data in sorted(files.items())
        ],
    }
    return json.dumps(request).encode(), files


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("admit", [False, True])
def test_only_the_fixtures_switch_admits_a_simulated_take(
    key: SigningKey, tmp_path: Path, admit: bool
) -> None:
    request, files = _simulated_take(tmp_path)
    with running(tmp_path / "fixture", key, admit_simulated=admit) as fixture:
        assert fixture.report["admit_simulated"] is admit
        outcome = _upload(_sign_in(fixture), request, files)
    if admit:
        assert outcome["state"] == "accepted", outcome
    else:
        assert outcome["state"] == "refused"
        assert "simulated" in outcome["refusal"]


def test_no_production_module_admits_a_simulated_take() -> None:
    """The switch is defined in corpus_takes and set by the contract fixture alone."""
    package = ROOT / "src" / "cole_voice_lab"
    users = sorted(
        str(path.relative_to(package))
        for path in package.rglob("*.py")
        if re.search(r"admitting_simulated_takes|_ADMIT_SIMULATED", path.read_text("utf-8"))
    )
    assert users == ["contract_fixture.py", "corpus_takes.py"]
    fixture = (package / "contract_fixture.py").read_text("utf-8")
    assert "contract_fixture" not in (package / "cli.py").read_text("utf-8")
    assert fixture.count("admitting_simulated_takes()") == 1


def _tls(tmp_path: Path) -> tuple[Path, Path, ssl.SSLContext]:
    signer = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(signer.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(signer, hashes.SHA256())
    )
    cert, private = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    private.write_bytes(
        signer.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert, private, ssl.create_default_context(cafile=str(cert))


def test_over_tls_the_issuer_is_the_fakes_own_url(key: SigningKey, tmp_path: Path) -> None:
    cert, private, context = _tls(tmp_path)
    with running(tmp_path / "fixture", key, host="localhost", tls=(cert, private)) as fixture:
        issuer = fixture.report["issuer"]
        assert issuer.startswith("https://localhost:")
        assert fixture.report["oidc"]["issuer"] == issuer
        assert fixture.report["api_url"].startswith("https://localhost:")
        api = _sign_in(fixture, context)
        assert api.call("GET", "/api/v1/meta")[0] == 200


# -- The command ---------------------------------------------------------------------------------


def test_serve_once_reports_and_refuses_a_used_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "fixture"
    root.mkdir()
    report = tmp_path / "report.json"
    assert main(["serve", "--root", str(root), "--report", str(report), "--once"]) == 0
    written = json.loads(report.read_text())
    assert written["contract_id"] == "corpus-client.v1"
    assert written["client_id"] == CLIENT_AUDIENCE
    assert written["capture_profile"] == PROFILE
    assert written["issuer"] == NOMINAL_ISSUER
    assert written["root"] == str(root)
    assert not list(tmp_path.glob(".report.json.tmp-*"))
    # Its root is used now: refused, and the report printed when none is named.
    assert main(["serve", "--root", str(root), "--once"]) == 1
    assert "must be an empty directory" in capsys.readouterr().err
    other = tmp_path / "other"
    other.mkdir()
    assert main(["serve", "--root", str(other), "--once"]) == 0
    assert json.loads(capsys.readouterr().out)["root"] == str(other)
    with pytest.raises(SystemExit):
        main(["serve", "--root", str(other), "--tls-cert", str(report)])


def test_serve_runs_until_terminated(tmp_path: Path) -> None:
    root, report = tmp_path / "fixture", tmp_path / "report.json"
    root.mkdir()
    child = subprocess.Popen(
        [sys.executable, "-I", "-B", "-m", "cole_voice_lab.contract_fixture", "serve"]
        + ["--root", str(root), "--report", str(report)],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    try:
        # A child's start is slow on a loaded PC: the deadline is generous.
        deadline = time.monotonic() + 90
        while not report.exists():
            assert child.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.05)
        api = Http(json.loads(report.read_text())["api_url"])
        assert api.call("GET", "/api/v1/meta")[0] == 401
        child.send_signal(signal.SIGTERM)
        assert child.wait(timeout=60) == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
