"""Typed failures exposed by the CLI boundary."""

from __future__ import annotations


class VoiceLabError(Exception):
    """Base class for expected, safely reportable failures."""


class ContractError(VoiceLabError):
    """A versioned input or evidence contract is invalid."""


class SecurityError(VoiceLabError):
    """A path, permission, consent, or transfer invariant failed."""


class ExternalToolError(VoiceLabError):
    """A required local process returned an invalid or failed result."""


class RuntimeUnavailableError(VoiceLabError):
    """An optional candidate runtime is not ready."""
