"""Access to wheel-bundled prompt and candidate resources."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from .models import PromptSuite


def resource_path(name: str) -> Path:
    resource = files("cole_voice_lab").joinpath("resources", name)
    return Path(str(resource))


def load_prompt_suite(name_or_path: str | Path) -> PromptSuite:
    requested = Path(name_or_path)
    if requested.exists():
        path = requested
    else:
        # `pilot` stays v1 permanently: the live Qt launcher runs `desktop --suite pilot`.
        # New suites get versioned names only.
        aliases = {
            "pilot": "pilot-v1.json",
            "pilot-v1": "pilot-v1.json",
            "pilot-v2": "pilot-v2.json",
            "physical-test-v1": "physical-test-v1.json",
            "evaluation": "evaluation-v1.json",
            "evaluation-v1": "evaluation-v1.json",
        }
        name = aliases.get(str(name_or_path), str(name_or_path))
        path = resource_path(name)
    return PromptSuite.model_validate_json(path.read_bytes())
