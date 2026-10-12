"""The speech configuration and its caller registry (speech/config.py, D-046)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from speech_fixtures import HOOK, TAILNET, caller, speech_config

from cole_voice_lab.canonical import canonical_json_bytes
from cole_voice_lab.errors import ContractError
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.speech.config import (
    SpeechConfig,
    load_speech_config,
    parse_webhook_url,
    webhook_url_problem,
)


@pytest.mark.parametrize(
    ("url", "problem"),
    [
        (f"http://agent.{TAILNET}/hook", "is https"),
        (f"https://user:pw@agent.{TAILNET}/hook", "no credentials"),
        (f"https://agent.{TAILNET}/hook?x=1", "no query"),
        (f"https://agent.{TAILNET}/hook#frag", "no query"),
        ("https://agent.example.com/hook", "node under"),
        ("https://100.64.0.7/hook", "node under"),
        (f"https://a.b.{TAILNET}/hook", "one node name"),
        (f"https://agent.{TAILNET}", "absolute path"),
        (f"https://agent.{TAILNET}/a/../b", "dot segments"),
        (f"https://agent.{TAILNET}:99999/hook", "must parse"),
        (f"https://agent.{TAILNET}/h ook", "no space"),
        (f"https://agent.{TAILNET}/h\\ook", "no space"),
        (f"https://agent.{TAILNET}/hök", "printable ASCII"),
        (f"https://AGENT.{TAILNET}/hook", "node under"),
        (f"https://agent.{TAILNET}.evil.example/hook", "node under"),
    ],
)
def test_webhook_urls_are_https_tailnet_nodes_only(url: str, problem: str) -> None:
    found = webhook_url_problem(url, TAILNET)
    assert found is not None and problem in found
    with pytest.raises(ValueError, match=problem):
        parse_webhook_url(url, TAILNET)


def test_a_tailnet_webhook_url_is_accepted_with_or_without_a_port() -> None:
    assert webhook_url_problem(HOOK, TAILNET) is None
    assert parse_webhook_url(f"https://agent.{TAILNET}:8443/h", TAILNET).port == 8443


def test_a_callback_must_equal_or_extend_an_allowed_url() -> None:
    registered = caller()
    assert registered.allows_callback(HOOK, TAILNET)
    assert registered.allows_callback(HOOK + "/conversation-1", TAILNET)
    assert registered.allows_callback(HOOK + "/", TAILNET)
    assert registered.allows_callback(HOOK.replace(".ts.net/", ".ts.net:443/"), TAILNET)
    assert not registered.allows_callback(HOOK + "-other", TAILNET)
    assert not registered.allows_callback(f"https://other.{TAILNET}/hooks/voice-lab", TAILNET)
    assert not registered.allows_callback(HOOK.replace(".ts.net/", ".ts.net:8443/"), TAILNET)


@pytest.mark.parametrize(
    "path",
    [
        "/hooks/voice-lab/%2e%2e/admin",
        "/hooks/voice-lab/%2E./admin",
        "/hooks/voice-lab/..%2fadmin",
        "/hooks/voice-lab%2Fadmin",
        "/hooks/voice-lab/%5c..",
        "/hooks/voice-lab//admin",
        "/hooks/voice-lab/%zz",
        "/hooks/voice-lab/./x",
    ],
)
def test_an_encoded_or_unnormalised_path_never_matches_the_allowlist(path: str) -> None:
    # The allowlist names /hooks/voice-lab: a receiver decoding these would see another path.
    url = f"https://agent.{TAILNET}{path}"
    assert webhook_url_problem(url, TAILNET) is not None
    assert not caller().allows_callback(url, TAILNET)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"webhook_secret_file": None}, "names a secret file"),
        ({"webhook_urls": (), "webhook_secret_file": "/etc/x"}, "names a secret file"),
        ({"webhook_urls": (HOOK, HOOK)}, "each webhook URL once"),
        ({"issuer": "https://id.example.invalid/"}, "without a trailing slash"),
        ({"issuer": "http://id.example.invalid"}, "https URL"),
        ({"audiences": ()}, "at least 1"),
        ({"allowed_purposes": ("private-fine-tuning",)}, "private-assistant-speech"),
        ({"max_text_characters": 2001}, "less than or equal"),
        ({"webhook_secret_file": "relative/secret"}, "absolute"),
        ({"subject": "has space"}, "pattern"),
    ],
)
def test_a_caller_is_refused_when_it_is_incomplete_or_too_broad(
    changes: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        caller(**changes)


def test_callers_are_distinct_and_their_urls_on_the_tailnet(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "lab")
    second = caller(
        caller_id="second",
        subject="client-second",
        webhook_secret_file="/etc/voice-lab/speech-webhook-secrets/second",
    )
    assert speech_config(tmp_path, layout, callers=(caller(), second)).caller("second") == second
    with pytest.raises(ValidationError, match="distinct ids"):
        speech_config(tmp_path, layout, callers=(caller(), caller()))
    with pytest.raises(ValidationError, match="never share a webhook secret"):
        speech_config(
            tmp_path,
            layout,
            callers=(
                caller(),
                second.model_copy(update={"webhook_secret_file": caller().webhook_secret_file}),
            ),
        )
    with pytest.raises(ValidationError, match="caller agent-server"):
        speech_config(tmp_path, layout, tailnet_suffix="other-tailnet.ts.net")
    with pytest.raises(ValidationError, match="ending in .ts.net"):
        speech_config(tmp_path, layout, tailnet_suffix="example.com")
    with pytest.raises(ValidationError, match="at most 4"):
        speech_config(
            tmp_path,
            layout,
            callers=tuple(
                caller(
                    caller_id=f"c{index}",
                    subject=f"s{index}",
                    webhook_urls=(),
                    webhook_secret_file=None,
                )
                for index in range(5)
            ),
        )
    assert speech_config(tmp_path, layout).caller("nobody") is None


def test_the_configuration_is_read_from_one_regular_file(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "lab")
    config = speech_config(tmp_path, layout)
    path = tmp_path / "speech.json"
    path.write_bytes(canonical_json_bytes(config))
    assert load_speech_config(path) == config
    assert config.state_path == Path(config.state_root)
    with pytest.raises(ContractError, match="cannot be read"):
        load_speech_config(tmp_path / "missing.json")
    link = tmp_path / "link.json"
    os.symlink(path, link)
    with pytest.raises(ContractError, match="cannot be read"):
        load_speech_config(link)
    path.write_text('{"schema_version": "cole-voice-lab.speech-config/v1"}')
    with pytest.raises(ContractError, match="invalid"):
        load_speech_config(path)
    path.write_bytes(b" " * (256 * 1024 + 1))
    with pytest.raises(ContractError, match="larger than"):
        load_speech_config(path)


def test_the_configuration_holds_no_secret_value() -> None:
    fields = set(SpeechConfig.model_json_schema()["$defs"]["SpeechCaller"]["properties"])
    assert "webhook_secret_file" in fields
    assert not {name for name in fields if "secret" in name} - {"webhook_secret_file"}


def test_secret_file_names_are_distinct_credential_names(tmp_path: Path) -> None:
    lab_layout = AssetLayout.initialize(tmp_path / "lab")
    first = caller(webhook_secret_file="/etc/voice-lab/a/agent-server")
    second = caller(
        caller_id="other",
        subject="client-other",
        webhook_secret_file="/etc/voice-lab/b/agent-server",
    )
    with pytest.raises(ValidationError, match="file name"):
        speech_config(tmp_path, lab_layout, callers=(first, second))
    odd = caller(webhook_secret_file="/etc/voice-lab/secrets/agent server")
    with pytest.raises(ValidationError, match="letters, digits"):
        speech_config(tmp_path, lab_layout, callers=(odd,))
