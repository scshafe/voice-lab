"""Closed, hash-pinned access to released built-in prompt suites.

@canonical prompt_catalogs -- released suite resources are immutable and never removed.
Pins detect an edited resource in a frozen runtime; they grant no trust. Nothing loads
at import, and no path opens a resource its caller did not name (except pilot-v1.json,
which identifies sidecar-less legacy captures). Related: resources.py, recording_library.py.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import cache
from types import MappingProxyType
from typing import Final

from .canonical import canonical_sha256
from .errors import ContractError, SecurityError
from .models import PromptSuite
from .resources import resource_path

PILOT_V1_RESOURCE: Final = "pilot-v1.json"
# Release order, pilot-v1.json first. Each pin lands in the same commit as its resource.
BUILTIN_PROMPT_SUITE_SHA256: Final[Mapping[str, str]] = MappingProxyType(
    {
        PILOT_V1_RESOURCE: "eac118a995a1e184d5ed20b9ee23c3e0924c272b28d87308df2abeee1f8772d0",
        "evaluation-v1.json": "2854decad263e9e82a131f130782ff7c846d4ffd14c21eab87cab8cca2e6caf2",
        "pilot-v2.json": "0f1f399de63a6c3c0e424b10bf27532b0e316c318a398877dea178ffd969a028",
        "physical-test-v1.json": "b17cc176ee36ac5aa053c949e7ff44ce6e1b0c13b95b4cf7184693c09e48750b",
    }
)
PILOT_V1_SHA256: Final = BUILTIN_PROMPT_SUITE_SHA256[PILOT_V1_RESOURCE]


def load_builtin_prompt_suite(resource: str) -> PromptSuite:
    """Read one pinned package resource; never an alias, a path or a working-directory file."""
    expected = BUILTIN_PROMPT_SUITE_SHA256.get(resource)
    if expected is None:
        raise ContractError(f"not a released built-in prompt suite: {resource!r}")
    suite = PromptSuite.model_validate_json(resource_path(resource).read_bytes())
    if canonical_sha256(suite) != expected:
        raise SecurityError(f"built-in prompt suite differs from its release pin: {resource}")
    return suite


@cache
def _pilot_v1() -> tuple[PromptSuite, frozenset[str]]:
    # Opened at most once per process, and only when a catalog is not pilot-v1 itself.
    suite = load_builtin_prompt_suite(PILOT_V1_RESOURCE)
    return suite, frozenset(canonical_sha256(prompt) for prompt in suite.prompts)


def legacy_pilot_v1(suite: PromptSuite) -> bool:
    """The suite shares at least one exact prompt hash with pilot-v1.

    Depends only on the suite's bytes and the immutable pilot-v1 resource, never on
    which other built-ins this build ships, so every build computes the same answer.
    """
    if canonical_sha256(suite) == PILOT_V1_SHA256:
        return True
    hashes = _pilot_v1()[1]
    return any(canonical_sha256(prompt) in hashes for prompt in suite.prompts)


def legacy_pilot_v1_suite(suite: PromptSuite) -> PromptSuite:
    """The genuine pilot-v1 source suite: the catalog itself when it is pilot-v1."""
    return suite if canonical_sha256(suite) == PILOT_V1_SHA256 else _pilot_v1()[0]
