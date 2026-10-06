"""Standard English sentence sets (Harvard, CMU ARCTIC) as corpus-store prompts.

@canonical standard_prompt_sets -- the published Harvard (720) and CMU ARCTIC (1,132) sentence
lists are parsed here, strictly and completely, into one prompt-suite/v2 carrier plus the
``PromptSourceReference`` (URL, fetch time, file digest, license) the corpus store records
beside every imported prompt. Nothing here writes: ``CatalogEditor.import_prompt_suite`` is
the only writer, and it imports a whole set or nothing.
Related: corpus_store.py (PromptSourceReference), catalog_ports.py (the importer), cli.py
(``voice-lab catalog import-harvard`` / ``import-arctic``).

A parse that does not yield exactly the published sentence count, or meets anything it does
not recognize (markup inside a sentence, an escaped quote, a stray line), fails as
``CorpusStoreError`` ``CORPUS.SOURCE_INVALID``; a set is never imported partially. The bytes
must also be the published list itself (``STANDARD_SET_SHA256``) unless the caller accepts an
unpinned copy explicitly, which every prompt's source reference then records. Fetching is a
separate, explicit call (``fetch_standard_set``); reading a saved copy never touches the
network.
"""

from __future__ import annotations

import hashlib
import html
import re
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Literal, Protocol

from pydantic import ValidationError

from .canonical import canonical_json_bytes, open_regular_no_follow
from .catalog_ports import CorpusStoreError
from .corpus_store import (
    PromptSourceReference,
    spoken_words,
    validation_problems,
)
from .models import PROMPT_SUITE_V2, Prompt, PromptSuite, utc_now

type StandardSetName = Literal["harvard", "arctic"]

# Where ``fetch_standard_set`` asks for each list. A fetch records the URL the response
# actually came from (after redirects); a saved copy records the URL its operator states.
HARVARD_URL: Final = "https://www.cs.columbia.edu/~hgs/audio/harvard.html"
# festvox.org serves this list over plain http only (https refused the connection on
# 2026-09-16), so nothing about the transfer authenticates it: the pinned digest below is
# what binds the content, whether it was fetched or read from a saved copy.
ARCTIC_URL: Final = "http://festvox.org/cmu_arctic/cmuarctic.data"
# SHA-256 of the published lists as saved on 2026-09-16 (harvard.html: 720 sentences in 72
# lists; cmuarctic.data: 1,132 lines). A copy with other bytes is refused unless the caller
# accepts it explicitly (``--accept-unpinned-source``), which the source reference records.
STANDARD_SET_SHA256: Final[Mapping[StandardSetName, str]] = {
    "harvard": "f8c981aed97020642fd2423de787b3ccd2a7233f32fe07ebd26d2279c195cb75",
    "arctic": "60e3d9a4dc33732c9100baadd747312bdc1a200fc891766507397289753a25c7",
}
# Every published list starts with this sentence, so a store holding it holds the set.
STANDARD_SET_FIRST_PROMPT_IDS: Final = {"harvard": "harvard-01-01", "arctic": "arctic-a0001"}
MAX_SOURCE_URL_CHARS: Final = 2048
HARVARD_SENTENCE_COUNT: Final = 720
HARVARD_LIST_LENGTH: Final = 10
ARCTIC_SENTENCE_COUNT: Final = 1_132
STANDARD_PROMPT_CATEGORY: Final = "neutral"
STANDARD_PROMPT_STYLE: Final = "Plain reading, natural pace."
STANDARD_SET_SUITE_IDS: Final = {"harvard": "harvard-sentences", "arctic": "cmu-arctic-sentences"}
MAX_STANDARD_SET_BYTES: Final = 4 * 1024 * 1024
DEFAULT_FETCH_TIMEOUT_SECONDS: Final = 30.0
HARVARD_LICENSE: Final = (
    "Harvard sentences: IEEE Recommended Practice for Speech Quality Measurements, "
    "IEEE Std 297-1969 (DOI 10.1109/IEEESTD.1969.7405210; IEEE Transactions on Audio and "
    "Electroacoustics 17(3), 225-246, 1969, DOI 10.1109/TAU.1969.1162058), Appendix C, "
    "1965 Revised List of Phonetically Balanced Sentences. Sentence list reproduced from "
    f"{HARVARD_URL}."
)
# Verbatim, byte for byte, from the COPYING file that ships with every CMU ARCTIC voice.
ARCTIC_LICENSE: Final = """
This voice is free for use for any purpose (commercial or otherwise)
subject to the pretty light restrictions detailed below.

############################################################################
###                                                                       ##
###                     Carnegie Mellon University                        ##
###                         Copyright (c) 2003                            ##
###                        All Rights Reserved.                           ##
###                                                                       ##
###  Permission to use, copy, modify,  and licence this software and its  ##
###  documentation for any purpose, is hereby granted without fee,        ##
###  subject to the following conditions:                                 ##
###   1. The code must retain the above copyright notice, this list of    ##
###      conditions and the following disclaimer.                         ##
###   2. Any modifications must be clearly marked as such.                ##
###   3. Original authors' names are not deleted.                         ##
###                                                                       ##
###  THE AUTHORS OF THIS WORK DISCLAIM ALL WARRANTIES WITH REGARD TO      ##
###  THIS SOFTWARE, INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY   ##
###  AND FITNESS, IN NO EVENT SHALL THE AUTHORS BE LIABLE FOR ANY         ##
###  SPECIAL, INDIRECT OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES            ##
###  WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN   ##
###  AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION,          ##
###  ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF       ##
###  THIS SOFTWARE.                                                       ##
###                                                                       ##
############################################################################
###                                                                       ##
###  See http://www.festvox.org/cmu_arctic/ for more details              ##
###                                                                       ##
############################################################################

"""

_HARVARD_LIST_HEADING = re.compile(r"<h2>List (\d+)</h2>")
_HARVARD_ITEM = re.compile(r"<li>(.*?)</li>", re.DOTALL)
_ARCTIC_LINE = re.compile(r'^\(\s*(arctic_[ab]\d{4})\s+"(.*)"\s*\)\s*$')


@dataclass(frozen=True, slots=True)
class StandardSentence:
    """One sentence of a standard set under its corpus prompt id."""

    prompt_id: str
    text: str


@dataclass(frozen=True, slots=True)
class StandardPromptSet:
    """A whole parsed set, ready for ``CorpusStore.import_prompt_suite``.

    ``suite`` is only a carrier (the store imports its prompts in order; the suite id and
    purpose are never published); ``source_reference`` is the canonical
    ``PromptSourceReference`` JSON stored beside each prompt. ``sha256`` is the source's
    digest and ``pinned_sha256`` the published list's; ``unpinned_source_accepted`` is true
    when they differ (the caller accepted the copy explicitly).
    """

    name: StandardSetName
    suite: PromptSuite
    source_reference: str
    sha256: str
    pinned_sha256: str
    unpinned_source_accepted: bool
    fetched_at: datetime
    url: str


class _Response(Protocol):
    status: int

    def read(self, size: int = -1, /) -> bytes: ...

    def geturl(self) -> str: ...

    def __enter__(self) -> _Response: ...

    def __exit__(self, *arguments: object) -> object: ...


type UrlOpener = Callable[..., _Response]


def _invalid(message: str) -> CorpusStoreError:
    return CorpusStoreError("CORPUS.SOURCE_INVALID", message)


def standard_expected_seconds(text: str) -> float:
    """The reading time a standard sentence is budgeted: ``round(0.42 * words + 0.8, 1)``.

    Params: ``text`` is the spoken sentence; words are whitespace-separated tokens.
    Returns: seconds, rounded to one decimal.
    Raises: nothing.
    """
    return round(0.42 * len(text.split()) + 0.8, 1)


def source_url(url: str) -> str:
    """Check the URL a copy of a set is recorded as coming from.

    Params: ``url`` as the operator states it, or as a fetch's response reports it.
    Returns: the same URL.
    Raises: ``CorpusStoreError`` ``CORPUS.SOURCE_REFERENCE_INVALID`` unless it is an
    ``http``/``https`` URL with a host, at most ``MAX_SOURCE_URL_CHARS`` characters, with no
    whitespace or control characters.
    """
    try:
        parsed = urllib.parse.urlsplit(url)
        located = parsed.scheme in ("http", "https") and bool(parsed.hostname)
    except ValueError:  # e.g. an unbalanced IPv6 bracket
        located = False
    if (
        not located
        or len(url) > MAX_SOURCE_URL_CHARS
        or any(character.isspace() or not character.isprintable() for character in url)
    ):
        raise CorpusStoreError(
            "CORPUS.SOURCE_REFERENCE_INVALID",
            f"source URL {url!r} is not an http(s) URL of at most {MAX_SOURCE_URL_CHARS} "
            "characters",
        )
    return url


def _decoded(payload: bytes, label: str) -> str:
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _invalid(f"{label} is not UTF-8: {error}") from error


def _require_count(
    sentences: list[StandardSentence], expected: int, label: str
) -> tuple[StandardSentence, ...]:
    if len(sentences) != expected:
        raise _invalid(
            f"{label} yields {len(sentences)} sentences, not the {expected} the published set has"
        )
    ids: set[str] = set()
    readings: dict[tuple[str, ...], str] = {}
    for sentence in sentences:
        if not sentence.text:
            raise _invalid(f"{label} sentence {sentence.prompt_id} is empty")
        if sentence.prompt_id in ids:
            raise _invalid(f"{label} repeats sentence id {sentence.prompt_id}")
        ids.add(sentence.prompt_id)
        # Two sentences that read identically would be one recording counted twice; the
        # store applies the same normalization against its live prompts at import.
        reading = spoken_words(sentence.text)
        if reading in readings:
            raise _invalid(
                f"{label} sentences {readings[reading]} and {sentence.prompt_id} read the same"
            )
        readings[reading] = sentence.prompt_id
    return tuple(sentences)


def parse_harvard_sentences(
    payload: bytes, *, expected_count: int | None = None
) -> tuple[StandardSentence, ...]:
    """Parse the Harvard sentence page into ``harvard-LL-NN`` sentences, in page order.

    The page is split at each ``<h2>List N</h2>`` heading; every ``<li>`` item of a list is
    one sentence (whitespace collapsed, HTML entities decoded). Lists must be numbered 1, 2,
    ... in order and hold exactly ``HARVARD_LIST_LENGTH`` sentences each, and nothing before
    the first heading may be a list item.
    Params: ``payload`` is the page's bytes; ``expected_count`` is the number of sentences the
    input must yield (``HARVARD_SENTENCE_COUNT`` for the published page when ``None``; an
    excerpt names its own).
    Returns: the sentences, list 1 item 1 first.
    Raises: ``CorpusStoreError`` ``CORPUS.SOURCE_INVALID`` for non-UTF-8 input, a list item
    before the first list, a list out of sequence or of another length, markup inside a
    sentence, an empty or repeated sentence (also ignoring case and punctuation), or a
    sentence count other than ``expected_count``.
    """
    expected = HARVARD_SENTENCE_COUNT if expected_count is None else expected_count
    parts = _HARVARD_LIST_HEADING.split(_decoded(payload, "Harvard page"))
    if _HARVARD_ITEM.search(parts[0]):
        raise _invalid("Harvard page has list items before its first list")
    sentences: list[StandardSentence] = []
    for position, (number, body) in enumerate(zip(parts[1::2], parts[2::2], strict=True), 1):
        if int(number) != position:
            raise _invalid(f"Harvard list {number} appears where list {position} belongs")
        items = _HARVARD_ITEM.findall(body)
        if len(items) != HARVARD_LIST_LENGTH:
            raise _invalid(
                f"Harvard list {number} holds {len(items)} sentences, not {HARVARD_LIST_LENGTH}"
            )
        for item_number, item in enumerate(items, 1):
            prompt_id = f"harvard-{position:02d}-{item_number:02d}"
            if "<" in item or ">" in item:
                raise _invalid(f"Harvard sentence {prompt_id} contains markup")
            text = " ".join(html.unescape(item).split())
            sentences.append(StandardSentence(prompt_id, text))
    return _require_count(sentences, expected, "Harvard page")


def parse_arctic_sentences(
    payload: bytes, *, expected_count: int | None = None
) -> tuple[StandardSentence, ...]:
    """Parse the CMU ARCTIC prompt list into ``arctic-a0001``-style sentences, in file order.

    Every line must be exactly ``( arctic_a0001 "Sentence." )``; the utterance id becomes the
    prompt id with ``_`` replaced by ``-``.
    Params: ``payload`` is the ``cmuarctic.data`` bytes; ``expected_count`` is the number of
    sentences the input must yield (``ARCTIC_SENTENCE_COUNT`` when ``None``).
    Returns: the sentences in file order.
    Raises: ``CorpusStoreError`` ``CORPUS.SOURCE_INVALID`` for non-UTF-8 input, any line of
    another form (blank lines included), a sentence carrying a quote or backslash escape, an
    empty or repeated sentence or id, or a sentence count other than ``expected_count``.
    """
    expected = ARCTIC_SENTENCE_COUNT if expected_count is None else expected_count
    sentences: list[StandardSentence] = []
    for line_number, line in enumerate(_decoded(payload, "ARCTIC list").splitlines(), 1):
        matched = _ARCTIC_LINE.match(line)
        if matched is None:
            raise _invalid(f"ARCTIC list line {line_number} is not an utterance entry")
        utterance, raw_text = matched.groups()
        if '"' in raw_text or "\\" in raw_text:
            raise _invalid(f"ARCTIC utterance {utterance} carries an escaped character")
        sentences.append(StandardSentence(utterance.replace("_", "-"), " ".join(raw_text.split())))
    return _require_count(sentences, expected, "ARCTIC list")


def standard_prompt_set(
    name: StandardSetName,
    payload: bytes,
    *,
    fetched_at: datetime,
    url: str,
    expected_count: int | None = None,
    accept_unpinned_source: bool = False,
) -> StandardPromptSet:
    """Parse one set and attach the provenance every one of its prompts records.

    Every sentence becomes a ``neutral`` prompt with style ``STANDARD_PROMPT_STYLE`` and
    ``standard_expected_seconds(text)``. The source must be the published list
    (``STANDARD_SET_SHA256[name]``): neither a plain-http fetch nor an operator's copy is
    otherwise authenticated.
    Params: ``name`` selects the parser, license and pinned digest; ``payload`` is the exact
    source bytes (their sha256 is recorded); ``fetched_at`` is when that copy was fetched
    (timezone-aware); ``url`` is where it came from, recorded as given (never assumed: a
    saved copy's operator states it, a fetch reports the URL its response came from);
    ``expected_count`` as for the parser; ``accept_unpinned_source`` accepts bytes other
    than the published list's (the reference then records ``unpinned_source_accepted``).
    Returns: the parsed set.
    Raises: ``CorpusStoreError`` ``CORPUS.SOURCE_INVALID`` from the parser, when a sentence
    does not form a valid prompt-suite/v2 prompt (length, control characters), or, after
    every other check, when the digest is not the pinned one and ``accept_unpinned_source``
    is false (the message names both digests); ``CORPUS.SOURCE_REFERENCE_INVALID`` when
    ``fetched_at`` is naive or ``url`` fails ``source_url``.
    """
    if name == "harvard":
        sentences = parse_harvard_sentences(payload, expected_count=expected_count)
        license_text = HARVARD_LICENSE
    else:
        sentences = parse_arctic_sentences(payload, expected_count=expected_count)
        license_text = ARCTIC_LICENSE
    digest = hashlib.sha256(payload).hexdigest()
    pinned = STANDARD_SET_SHA256[name]
    unpinned = digest != pinned
    try:
        reference = PromptSourceReference(
            url=source_url(url),
            fetched_at=fetched_at,
            sha256=digest,
            license=license_text,
            unpinned_source_accepted=unpinned,
        )
    except ValidationError as error:
        raise CorpusStoreError(
            "CORPUS.SOURCE_REFERENCE_INVALID",
            f"{name} source reference is invalid: {validation_problems(error)}",
        ) from error
    try:
        suite = PromptSuite(
            schema_version=PROMPT_SUITE_V2,
            suite_id=STANDARD_SET_SUITE_IDS[name],
            purpose="capture-corpus",
            prompts=tuple(
                Prompt(
                    prompt_id=sentence.prompt_id,
                    text=sentence.text,
                    category=STANDARD_PROMPT_CATEGORY,
                    style=STANDARD_PROMPT_STYLE,
                    expected_seconds=standard_expected_seconds(sentence.text),
                )
                for sentence in sentences
            ),
        )
    except ValidationError as error:
        raise _invalid(
            f"{name} sentences do not form valid prompts: {validation_problems(error)}"
        ) from error
    if unpinned and not accept_unpinned_source:
        raise _invalid(
            f"the {name} copy has sha256 {digest}, not {pinned}, the published list's; "
            "nothing was imported. Use a copy of the published list, or pass "
            "--accept-unpinned-source to import this one (every prompt's source reference "
            "then records it)"
        )
    return StandardPromptSet(
        name=name,
        suite=suite,
        source_reference=canonical_json_bytes(reference).decode(),
        sha256=digest,
        pinned_sha256=pinned,
        unpinned_source_accepted=unpinned,
        fetched_at=reference.fetched_at,
        url=reference.url,
    )


def read_standard_set_file(path: Path) -> bytes:
    """Read a saved copy of a standard set: one regular file, never through a final symlink.

    Params: ``path`` is the operator's local copy.
    Returns: its bytes.
    Raises: ``SecurityError`` when the path is a symlink or not a regular file (a FIFO or
    device is refused without blocking), or cannot be opened; ``CorpusStoreError``
    ``CORPUS.SOURCE_INVALID`` when it exceeds ``MAX_STANDARD_SET_BYTES``.
    """
    source = path.expanduser()
    with open_regular_no_follow(source, nonblocking=True) as stream:
        payload = stream.read(MAX_STANDARD_SET_BYTES + 1)
    if len(payload) > MAX_STANDARD_SET_BYTES:
        raise _invalid(f"standard set copy exceeds {MAX_STANDARD_SET_BYTES} bytes: {source}")
    return payload


def fetch_standard_set(
    name: StandardSetName,
    *,
    timeout: float = DEFAULT_FETCH_TIMEOUT_SECONDS,
    opener: UrlOpener = urllib.request.urlopen,
    clock: Callable[[], datetime] = utc_now,
) -> tuple[bytes, datetime, str]:
    """Download one set from its published location (a network action; callers gate it).

    Params: ``name`` selects the URL; ``timeout`` bounds the request in seconds; ``opener``
    performs it (``urllib.request.urlopen``); ``clock`` stamps the fetch.
    Returns: ``(payload, fetched_at, url)``; ``url`` is where the response came from (the
    requested URL, or where it redirected to). The payload is not trusted yet:
    ``standard_prompt_set`` checks it against the pinned digest.
    Raises: ``CorpusStoreError`` ``CORPUS.SOURCE_UNAVAILABLE`` when the request fails or does
    not answer 200, ``CORPUS.SOURCE_INVALID`` when the body exceeds
    ``MAX_STANDARD_SET_BYTES``, ``CORPUS.SOURCE_REFERENCE_INVALID`` when the response's URL
    fails ``source_url``.
    """
    url = HARVARD_URL if name == "harvard" else ARCTIC_URL
    request = urllib.request.Request(url, headers={"User-Agent": "voice-lab"})
    fetched_at = clock()
    try:
        with opener(request, timeout=timeout) as response:
            if response.status != 200:
                raise CorpusStoreError(
                    "CORPUS.SOURCE_UNAVAILABLE", f"{url} answered HTTP {response.status}"
                )
            served_from = response.geturl()
            payload = response.read(MAX_STANDARD_SET_BYTES + 1)
    except (OSError, ValueError) as error:
        # URLError, HTTPError and timeouts are OSErrors; a malformed response is a ValueError.
        message = f"cannot fetch {url}: {error}"
        raise CorpusStoreError("CORPUS.SOURCE_UNAVAILABLE", message) from error
    if len(payload) > MAX_STANDARD_SET_BYTES:
        raise _invalid(f"{url} is larger than {MAX_STANDARD_SET_BYTES} bytes")
    return payload, fetched_at, source_url(served_from)
