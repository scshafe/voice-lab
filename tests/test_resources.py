from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

import pytest

from cole_voice_lab.canonical import canonical_sha256
from cole_voice_lab.models import Prompt
from cole_voice_lab.prompt_catalogs import PILOT_V1_SHA256, load_builtin_prompt_suite
from cole_voice_lab.resources import load_prompt_suite, resource_path

# docs/PROMPT_CATALOG_V2.md §2.5 goldens.
PILOT_V2_SHA256 = "0f1f399de63a6c3c0e424b10bf27532b0e316c318a398877dea178ffd969a028"
PILOT_V2_ROOM_TONE_SHA256 = {
    "room-tone-start": "0377c02d0dd8bfbee08367edc73e016b0d156a9fc112aa7dc099a4f116eb90fb",
    "room-tone-end": "105b37fa88783ade0bb9fb995ae528ca814a54e935759b8978008507fb1f60d1",
}
# docs/PROMPT_CATALOG_V2.md §4: one prompt per physical-gate procedure step, in order.
PHYSICAL_TEST_STEPS = (
    ("p01-setup-identity", "procedure", 120.0),
    ("p02-microphone-denial", "procedure", 120.0),
    ("p02a-permission-reset", "procedure", 120.0),
    ("p03-grant-and-immediate-stop", "procedure", 2.0),
    ("p04-room-tone", "room-tone", 10.0),
    ("p05-spoken-sentence", "neutral", 10.0),
    ("p06-native-playback", "procedure", 10.0),
    ("p07-independent-playback", "procedure", 10.0),
    ("p08-continuous-capture", "conversational", 60.0),
    ("p09-rapid-cycles", "procedure", 3.0),
    ("p10-unplug-while-capturing", "procedure", 10.0),
    ("p11-reconnect-and-recover", "procedure", 3.0),
    ("p12-sleep-wake", "procedure", 3.0),
    ("p13-draft-reopen", "procedure", 3.0),
    ("p13a-interrupted-control", "procedure", 3.0),
    ("p14-package-and-intake", "procedure", 120.0),
)
PHYSICAL_TEST_EXTRA_LABELS = frozenset(
    {
        "setup",
        "permission",
        "transport",
        "waveform",
        "playback",
        "continuity",
        "device-loss",
        "recovery",
        "package",
    }
)
# Built-in text ships in the wheel and the app bundle, and the script view is selectable.
FORBIDDEN_SUBSTRINGS = ("/Users/", "~/", "<GATE>", "`", "sudo", "rm ", "private.cole.")
AGENT_ONLY_SUBSTRINGS = ("tccutil", "afplay", "intake validate", "intake accept", "exact output")
# Canonical hashes of the v1 prompts in the one-off catalog cole-physical-diagnostic-20260914
# (read-only from its settings.json); P04, P05 and P08 copy their content verbatim.
DIAGNOSTIC_PROMPT_SHA256 = {
    "diagnostic-room-tone": "05b57bfd05a56aa357e676c176969842772e43ddfed1934b1455ed73e5bfe81e",
    "diagnostic-spoken": "b3145ca87b620ab2e82a5fdd15d0334090de3a3c959bafc09aa00a4c03ca2a89",
    "diagnostic-continuous": "e0c6edcaaba5fc3054beba0ae444b77ace1468a35c116f43f024118463f44f23",
}


class _CaptureConsoleHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.inline_scripts = 0
        self.style_elements = 0
        self.inline_attributes: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)
        if tag == "script" and "src" not in attributes:
            self.inline_scripts += 1
        if tag == "style":
            self.style_elements += 1
        self.inline_attributes.extend(
            name for name, _value in attrs if name == "style" or name.startswith("on")
        )


def test_resource_path_resolves_bundled_file() -> None:
    path = resource_path("pilot-v1.json")
    assert path.is_file()
    assert path.parent.name == "resources"


def test_pilot_suite_is_versioned_and_near_five_minutes() -> None:
    suite = load_prompt_suite("pilot")

    assert suite.purpose == "capture-pilot"
    assert 270 <= sum(prompt.expected_seconds for prompt in suite.prompts) <= 360
    assert sum(prompt.reference_candidate for prompt in suite.prompts) >= 3
    assert suite.prompts[0].category == "room-tone"
    assert suite.prompts[-1].category == "room-tone"


def test_evaluation_suite_is_held_out() -> None:
    suite = load_prompt_suite("evaluation")

    assert suite.purpose == "held-out-evaluation"
    assert len(suite.prompts) >= 10
    assert all(prompt.holdout for prompt in suite.prompts)
    assert not (
        {prompt.text for prompt in suite.prompts}
        & {prompt.text for prompt in load_prompt_suite("pilot").prompts}
    )


def test_versioned_aliases_load_released_resources_and_pilot_stays_v1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TR1: `pilot` never moves off v1; TQ1 separately proves the live launcher path."""
    monkeypatch.chdir(tmp_path)
    for name in ("pilot", "pilot-v1"):
        assert canonical_sha256(load_prompt_suite(name)) == PILOT_V1_SHA256
    assert load_prompt_suite("pilot-v2") == load_builtin_prompt_suite("pilot-v2.json")
    assert load_prompt_suite("physical-test-v1") == load_builtin_prompt_suite(
        "physical-test-v1.json"
    )
    with pytest.raises(FileNotFoundError):
        load_prompt_suite("physical-test")


def test_pilot_v2_resource_follows_its_authoring_contract() -> None:
    """TR1: only the room tones change form, so 34 spoken prompt hashes keep their coverage."""
    pilot, pilot_v2 = load_builtin_prompt_suite("pilot-v1.json"), load_prompt_suite("pilot-v2")

    assert canonical_sha256(pilot_v2) == PILOT_V2_SHA256
    assert pilot_v2.schema_version == "cole-voice-lab.prompt-suite/v2"
    assert (pilot_v2.suite_id, pilot_v2.purpose) == ("cole-pilot-v2", "capture-pilot")
    assert [p.prompt_id for p in pilot_v2.prompts] == [p.prompt_id for p in pilot.prompts]
    assert sum(prompt.expected_seconds for prompt in pilot_v2.prompts) == 312.0
    spoken = 0
    for before, after in zip(pilot.prompts, pilot_v2.prompts, strict=True):
        if before.category != "room-tone":
            assert after == before and not after.labels and after.instructions is None
            assert canonical_sha256(after) == canonical_sha256(before)
            spoken += 1
            continue
        assert after.text is None and after.instructions == before.text and not after.labels
        assert after.model_dump(exclude={"instructions"}) == before.model_dump(exclude={"text"})
        assert canonical_sha256(after) == PILOT_V2_ROOM_TONE_SHA256[after.prompt_id]
    assert spoken == 34


def test_physical_test_resource_follows_its_authoring_contract() -> None:
    """TR1: procedure steps, labels, verbatim diagnostic content and operator-only wording."""
    suite = load_prompt_suite("physical-test-v1")
    prompts = {prompt.prompt_id: prompt for prompt in suite.prompts}

    assert suite.schema_version == "cole-voice-lab.prompt-suite/v2"
    assert (suite.suite_id, suite.purpose) == ("cole-physical-test-v1", "physical-test")
    assert [(p.prompt_id, p.category, p.expected_seconds) for p in suite.prompts] == list(
        PHYSICAL_TEST_STEPS
    )
    for prompt in suite.prompts:
        assert not prompt.holdout and not prompt.reference_candidate
        assert prompt.labels[0] == "test" and len(prompt.labels) <= 3
        assert set(prompt.labels[1:]) <= PHYSICAL_TEST_EXTRA_LABELS
        assert prompt.instructions is not None
        if prompt.category == "procedure":
            assert prompt.style == "Follow the steps; speak only if a step says so."
        for value in (prompt.text, prompt.instructions, prompt.style):
            for forbidden in (*FORBIDDEN_SUBSTRINGS, *AGENT_ONLY_SUBSTRINGS):
                assert forbidden not in (value or ""), (prompt.prompt_id, forbidden)

    sources = {
        "diagnostic-room-tone": Prompt(
            prompt_id="diagnostic-room-tone",
            text=prompts["p04-room-tone"].instructions,
            category="room-tone",
            style=prompts["p04-room-tone"].style,
            expected_seconds=10.0,
        ),
        "diagnostic-spoken": Prompt(
            prompt_id="diagnostic-spoken",
            text=prompts["p05-spoken-sentence"].text,
            category="neutral",
            style=prompts["p05-spoken-sentence"].style,
            expected_seconds=10.0,
        ),
        "diagnostic-continuous": Prompt(
            prompt_id="diagnostic-continuous",
            text=prompts["p08-continuous-capture"].text,
            category="conversational",
            style=prompts["p08-continuous-capture"].style,
            expected_seconds=60.0,
        ),
    }
    assert {name: canonical_sha256(p) for name, p in sources.items()} == DIAGNOSTIC_PROMPT_SHA256

    def instructions(prompt_id: str) -> str:
        return prompts[prompt_id].instructions or ""

    agent_step = "The agent performs this step outside the app; selecting it is optional."
    reselect = "After each Save, select this step again before the next take."
    assert all(
        agent_step in instructions(i) for i in ("p02a-permission-reset", "p13a-interrupted-control")
    )
    assert all(reselect in instructions(i) for i in ("p09-rapid-cycles", "p12-sleep-wake"))
    assert (
        "Record fresh P04 then P05 first, in this app session, then prepare the package."
        in instructions("p14-package-and-intake")
    )


def test_prompt_suite_can_load_an_explicit_file(tmp_path: Path) -> None:
    custom = tmp_path / "custom-prompts.json"
    custom.write_bytes(resource_path("pilot-v1.json").read_bytes())

    suite = load_prompt_suite(custom)
    assert suite.purpose == "capture-pilot"


def test_unknown_bundled_prompt_suite_fails_closed() -> None:
    with pytest.raises(FileNotFoundError):
        load_prompt_suite("unknown-suite.json")


def test_capture_console_resources_are_bundled() -> None:
    for name in (
        "capture-console.html",
        "capture-console.js",
        "capture-console.css",
    ):
        path = resource_path(name)
        assert path.is_file()
        assert path.parent.name == "resources"
        assert path.stat().st_size > 0


def test_capture_console_markup_is_compatible_with_strict_csp() -> None:
    parser = _CaptureConsoleHtmlParser()
    parser.feed(resource_path("capture-console.html").read_text(encoding="utf-8"))

    assert parser.inline_scripts == 0
    assert parser.style_elements == 0
    assert parser.inline_attributes == []


def test_capture_console_resources_are_local_and_never_record_in_the_browser() -> None:
    contents = "\n".join(
        resource_path(name).read_text(encoding="utf-8")
        for name in (
            "capture-console.html",
            "capture-console.js",
            "capture-console.css",
        )
    )

    assert "http://" not in contents.casefold()
    assert "https://" not in contents.casefold()
    for recording_api in (
        "audiocontext",
        "enumeratedevices",
        "getusermedia",
        "mediadevices",
        "mediarecorder",
        "webkitaudiocontext",
    ):
        assert recording_api not in contents.casefold()


def test_capture_console_script_preserves_shortcuts_and_primary_focus() -> None:
    script = resource_path("capture-console.js").read_text(encoding="utf-8")
    keyboard = script.split("document.addEventListener('keydown'", maxsplit=1)[1]
    busy_state = script.split("const setBusy =", maxsplit=1)[1].split(
        "const clearPreview =", maxsplit=1
    )[0]
    transition = script.split("const transition =", maxsplit=1)[1].split(
        "const playPreview =", maxsplit=1
    )[0]

    assert "target instanceof HTMLAudioElement || isEditableTarget(target)" in keyboard
    assert "if (target instanceof HTMLButtonElement" not in keyboard
    assert "event.code === 'Space'" in keyboard
    assert "key === 'p'" in keyboard
    assert "key === 'r'" in keyboard
    assert "event.key === 'Enter'" in keyboard
    assert "&& !(target instanceof HTMLButtonElement)" in keyboard
    assert "button.disabled = value;" in busy_state
    assert "button.hidden" not in busy_state
    assert transition.index("await refreshState();") < transition.index("setBusy(false);")
    assert transition.index("setBusy(false);") < transition.index("focusPrimary(state);")


def test_capture_console_script_handles_automatic_stop_and_manual_preview_playback() -> None:
    script = resource_path("capture-console.js").read_text(encoding="utf-8")

    assert "state.status === 'recording'\n        && captureState.status === 'review'" in script
    assert "Preview loaded. Press Play in the audio control to listen." in script
    assert "if (manualPlayback) {\n        elements.preview.focus();" in script
    assert "captureState.kind !== 'room-tone'\n      || !['ready', 'recording']" in script
    assert "recording: captureState.elapsed_seconds > 0" in script
    assert "Starting microphone — wait" in script
