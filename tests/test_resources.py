from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

import pytest

from cole_voice_lab.resources import load_prompt_suite, resource_path


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
