"""Provider-neutral diagnostic metrics used after generation."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .errors import ContractError

_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", flags=re.UNICODE)
MAX_WER_TOKENS = 4_096


def word_tokens(text: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", text).casefold().replace("’", "'")
    return tuple(_WORD.findall(normalized))


@dataclass(frozen=True, slots=True)
class WordErrorRate:
    substitutions: int
    deletions: int
    insertions: int
    reference_words: int

    @property
    def errors(self) -> int:
        return self.substitutions + self.deletions + self.insertions

    @property
    def value(self) -> float:
        if self.reference_words == 0:
            return 0.0 if self.errors == 0 else 1.0
        return self.errors / self.reference_words

    def to_json(self) -> dict[str, int | float]:
        return {
            "substitutions": self.substitutions,
            "deletions": self.deletions,
            "insertions": self.insertions,
            "reference_words": self.reference_words,
            "errors": self.errors,
            "wer": self.value,
        }


@dataclass(frozen=True, slots=True)
class _Cell:
    errors: int
    substitutions: int
    deletions: int
    insertions: int


def word_error_rate(reference: str, hypothesis: str) -> WordErrorRate:
    """Compute deterministic Levenshtein WER with stable tie-breaking."""

    expected = word_tokens(reference)
    actual = word_tokens(hypothesis)
    if len(expected) > MAX_WER_TOKENS or len(actual) > MAX_WER_TOKENS:
        raise ContractError(f"WER inputs are limited to {MAX_WER_TOKENS} tokens each")
    previous = [_Cell(index, 0, 0, index) for index in range(len(actual) + 1)]
    for reference_index, reference_word in enumerate(expected, start=1):
        current = [_Cell(reference_index, 0, reference_index, 0)]
        for hypothesis_index, hypothesis_word in enumerate(actual, start=1):
            if reference_word == hypothesis_word:
                current.append(previous[hypothesis_index - 1])
                continue
            substitute_from = previous[hypothesis_index - 1]
            delete_from = previous[hypothesis_index]
            insert_from = current[hypothesis_index - 1]
            candidates = (
                _Cell(
                    substitute_from.errors + 1,
                    substitute_from.substitutions + 1,
                    substitute_from.deletions,
                    substitute_from.insertions,
                ),
                _Cell(
                    delete_from.errors + 1,
                    delete_from.substitutions,
                    delete_from.deletions + 1,
                    delete_from.insertions,
                ),
                _Cell(
                    insert_from.errors + 1,
                    insert_from.substitutions,
                    insert_from.deletions,
                    insert_from.insertions + 1,
                ),
            )
            # Stable preference: substitution, then deletion, then insertion.
            current.append(min(candidates, key=lambda cell: cell.errors))
        previous = current
    final = previous[-1]
    return WordErrorRate(
        substitutions=final.substitutions,
        deletions=final.deletions,
        insertions=final.insertions,
        reference_words=len(expected),
    )
