"""The optional container spec cannot address a real vault or consume public credentials."""

from pathlib import Path

import pytest
from storage_conformance import ContainerHttpSpec, container_http_spec, container_settings


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:1234",
        "http://localhost:1234",
        "http://vault.example:1234",
        "http://100.64.0.1:1234",
        "http://0.0.0.0:1234",
        "http://127.0.0.1",
        "http://127.0.0.1:0",
        "http://127.0.0.1:1234/path",
        "http://127.0.0.1:1234?x=y",
        "http://user@127.0.0.1:1234",
        "http://127.0.0.1:1234#fragment",
    ],
)
def test_container_opt_in_refuses_nonliteral_or_decorated_loopback_url(url: str) -> None:
    with pytest.raises(ValueError, match="loopback"):
        container_settings(
            {
                "COLE_VOICE_LAB_VAULT_CONTAINER_URL": url,
                "COLE_VOICE_LAB_VAULT_CONTAINER_TOKEN_FILE": "/synthetic/token",
            }
        )


def test_container_opt_in_requires_both_fields_and_test_namespace() -> None:
    assert container_settings({}) is None
    with pytest.raises(ValueError, match="both"):
        container_settings({"COLE_VOICE_LAB_VAULT_CONTAINER_URL": "http://127.0.0.1:1234"})
    with pytest.raises(ValueError, match="namespace test"):
        container_settings(
            {
                "COLE_VOICE_LAB_VAULT_CONTAINER_URL": "http://127.0.0.1:1234",
                "COLE_VOICE_LAB_VAULT_CONTAINER_TOKEN_FILE": "/synthetic/token",
                "COLE_VOICE_LAB_VAULT_CONTAINER_NAMESPACE": "production",
            }
        )


@pytest.mark.parametrize("address", ["127.0.0.1", "[::1]"])
def test_container_opt_in_accepts_only_test_loopback(address: str) -> None:
    parsed = container_settings(
        {
            "COLE_VOICE_LAB_VAULT_CONTAINER_URL": f"http://{address}:1234",
            "COLE_VOICE_LAB_VAULT_CONTAINER_TOKEN_FILE": "/synthetic/token",
        }
    )
    assert parsed == (address.strip("[]"), 1234, Path("/synthetic/token"))


def test_container_spec_has_no_backing_capability_and_hides_token(tmp_path: Path) -> None:
    spec = ContainerHttpSpec(
        root=tmp_path, scratch=tmp_path, port=1234, token="synthetic credential", namespace="test"
    )
    assert spec.backing_root is None
    assert "synthetic credential" not in repr(spec)


def test_container_spec_refuses_a_nonprivate_token_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = tmp_path / "token"
    token.write_text("invalid synthetic placeholder")
    token.chmod(0o644)
    monkeypatch.setenv("COLE_VOICE_LAB_VAULT_CONTAINER_URL", "http://127.0.0.1:1234")
    monkeypatch.setenv("COLE_VOICE_LAB_VAULT_CONTAINER_TOKEN_FILE", str(token))
    with pytest.raises(ValueError, match="0600"):
        with container_http_spec(tmp_path):
            pytest.fail("unsafe token was accepted")


@pytest.mark.parametrize(
    "order",
    [
        ("missing", "prefix"),
        ("prefix", "missing"),
        ("prefix", "prefix"),
        ("missing", "prefix", "missing", "prefix"),
    ],
)
@pytest.mark.serial
def test_real_http_ref_cases_are_independent_in_reverse_and_repeat_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    order: tuple[str, ...],
) -> None:
    import storage_conformance as suite

    monkeypatch.setattr(suite, "HTTP_NAMESPACE", "test")
    base = tmp_path.resolve() / "loopback"
    base.mkdir(mode=0o700)
    cases = {
        "missing": suite.test_a_missing_ref_reads_as_none,
        "prefix": suite.test_refs_with_shared_prefixes_are_independent,
    }
    with suite.loopback_vault(base) as (root, port, token):
        spec = ContainerHttpSpec(root=root, scratch=base, port=port, token=token, namespace="test")
        for name in order:
            with monkeypatch.context() as scope:
                suite.scope_container_case(scope)
                cases[name](spec)
