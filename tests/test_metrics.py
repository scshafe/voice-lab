from __future__ import annotations

import pytest

from cole_voice_lab.errors import ContractError
from cole_voice_lab.metrics import word_error_rate, word_tokens


def test_tokenization_normalizes_case_unicode_and_punctuation() -> None:
    assert word_tokens("We’re READY—now.") == ("we're", "ready", "now")


@pytest.mark.parametrize(
    ("reference", "hypothesis", "substitutions", "deletions", "insertions"),
    (
        ("one two three", "one two three", 0, 0, 0),
        ("one two three", "one four three", 1, 0, 0),
        ("one two three", "one three", 0, 1, 0),
        ("one three", "one two three", 0, 0, 1),
    ),
)
def test_word_error_rate_operations(
    reference: str,
    hypothesis: str,
    substitutions: int,
    deletions: int,
    insertions: int,
) -> None:
    result = word_error_rate(reference, hypothesis)
    assert result.substitutions == substitutions
    assert result.deletions == deletions
    assert result.insertions == insertions
    assert result.value == result.errors / max(result.reference_words, 1)


def test_empty_reference_is_bounded() -> None:
    assert word_error_rate("", "").value == 0.0
    assert word_error_rate("", "unexpected").value == 1.0


def test_word_error_rate_rejects_quadratic_abuse() -> None:
    with pytest.raises(ContractError, match="limited"):
        word_error_rate("word " * 4_097, "word")
