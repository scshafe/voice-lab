"""`voice-lab catalog` and the standard sentence-set importers (corpus-store Step 5).

The CLI is a thin client of the corpus store: `catalog init` seeds a built-in and publishes
and activates version 1 in one store transaction, the importers parse the published Harvard
and CMU ARCTIC lists completely or not at all, `edit-prompt` states what happens to the text
and to the recordings that count for a prompt before it writes, and `--catalog` /
`--baseline` accept a published `<catalog_id>[@<version>]` beside the built-in names. Every
refusal exits 2 and leads with its code (`voice-lab: CORPUS.X: ...`).

Fixture excerpts under tests/fixtures/standard_sets are cut from the published files:
harvard-excerpt.html is the page header plus lists 1 and 2 of
https://www.cs.columbia.edu/~hgs/audio/harvard.html (sha256 f8c981ae...), arctic-excerpt.data
the first 20 lines of the CMU ARCTIC cmuarctic.data (sha256 60e3d9a4...), distributed under
arctic-COPYING.txt. The full files are checked only where a saved copy is present
(the directory named by ``COLE_VOICE_LAB_STANDARD_SETS``; skipped when it is unset).
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from test_corpus_store_recording_integration import Library
from test_corpus_store_recording_integration import library as library
from test_pilot_v2_coverage import (
    PILOT_V2_SHA256,
    ROOM_TONES,
    SUBJECT,
    add_foreign_package,
    build_legacy_root,
    prepare_pilot_v2_package,
    released_pilot_v2,
    spoken,
)
from test_pilot_v2_coverage import analyzer_build as analyzer_build
from test_pilot_v2_coverage import foreign_pilot_v2_package as foreign_pilot_v2_package

import cole_voice_lab.cli as cli_module
import cole_voice_lab.prompt_coverage as prompt_coverage
import cole_voice_lab.standard_prompt_sets as standard_sets
from cole_voice_lab.canonical import canonical_json_bytes, canonical_sha256
from cole_voice_lab.cli import build_parser, main
from cole_voice_lab.corpus_store import (
    MAX_CATALOG_VERSION,
    CorpusStore,
    CorpusStoreError,
    PromptSourceReference,
    open_corpus_store,
)
from cole_voice_lab.diagnostic_roots import (
    DIAGNOSTIC_ROOT_REFUSAL,
    DIAGNOSTIC_SUBJECT_REFUSAL,
    PHYSICAL_TEST_CATALOG_REFUSAL,
)
from cole_voice_lab.errors import SecurityError
from cole_voice_lab.models import PROMPT_SUITE_V2, Prompt, PromptSuite
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.prompt_catalogs import BUILTIN_PROMPT_SUITE_SHA256
from cole_voice_lab.prompt_coverage import prompt_coverage_report
from cole_voice_lab.recording_contracts import CatalogReference
from cole_voice_lab.resources import resource_path
from cole_voice_lab.standard_prompt_sets import (
    ARCTIC_LICENSE,
    ARCTIC_URL,
    HARVARD_LICENSE,
    HARVARD_URL,
    fetch_standard_set,
    parse_arctic_sentences,
    parse_harvard_sentences,
    read_standard_set_file,
    source_url,
    standard_expected_seconds,
    standard_prompt_set,
)

FIXTURES = Path(__file__).parent / "fixtures" / "standard_sets"
HARVARD_EXCERPT = FIXTURES / "harvard-excerpt.html"
ARCTIC_EXCERPT = FIXTURES / "arctic-excerpt.data"
ARCTIC_COPYING = FIXTURES / "arctic-COPYING.txt"
# A directory holding saved copies of the published files (harvard.html, cmuarctic.data and,
# optionally, a reference parse parsed.json); the full-file tests skip without it.
SAVED_SETS_VARIABLE = "COLE_VOICE_LAB_STANDARD_SETS"
SAVED_SETS = Path(os.environ[SAVED_SETS_VARIABLE]) if os.environ.get(SAVED_SETS_VARIABLE) else None
HARVARD_FILE_SHA256 = "f8c981aed97020642fd2423de787b3ccd2a7233f32fe07ebd26d2279c195cb75"
ARCTIC_FILE_SHA256 = "60e3d9a4dc33732c9100baadd747312bdc1a200fc891766507397289753a25c7"
FETCHED_AT = "2026-09-16T04:17:51Z"
# Where a saved copy came from is stated by the operator; tests state these.
COPY_URLS = {
    "import-harvard": HARVARD_URL,
    # A mirror spelling, so a test can tell a stated URL from the one --fetch requests.
    "import-arctic": "http://www.festvox.org/cmu_arctic/cmuarctic.data",
}
UNKNOWN = "not a released built-in prompt suite or a published catalog"
BUILT_INS = "built-in names: pilot-v1, evaluation-v1, pilot-v2, physical-test-v1"
DIAGNOSTIC_SUBJECT = "cole-physical-diagnostic-01"


@pytest.fixture(autouse=True)
def _no_ambient_asset_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COLE_VOICE_LAB_ASSET_ROOT", raising=False)


@dataclass
class Cli:
    capsys: pytest.CaptureFixture[str]

    def __call__(self, *arguments: str) -> tuple[int, str, str]:
        status = main(list(arguments))
        captured = self.capsys.readouterr()
        return status, captured.out, captured.err

    def json(self, *arguments: str) -> Any:
        status, out, err = self(*arguments)
        assert status == 0, err
        return json.loads(out)

    def refused(self, *arguments: str, code: str | None = None) -> str:
        """The refusal's stderr; with ``code``, it must lead with that code."""
        status, out, err = self(*arguments)
        assert status == 2, out
        if code is not None:
            assert err.startswith(f"voice-lab: {code}: "), err
        return err


@pytest.fixture
def cli(capsys: pytest.CaptureFixture[str]) -> Cli:
    return Cli(capsys)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return AssetLayout.initialize(tmp_path.resolve() / "assets").root


def store_of(root: Path) -> CorpusStore:
    return open_corpus_store(AssetLayout.validate(root), create=False)


def init(cli: Cli, root: Path, *extra: str) -> Any:
    return cli.json("catalog", "init", "--asset-root", str(root), "--subject-id", SUBJECT, *extra)


def seeded(cli: Cli, root: Path) -> Any:
    return init(cli, root, "--from-builtin", "pilot-v2", "--drop-room-tones")


def excerpt_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat the checked-in excerpts as the whole published files (20 sentences each)."""
    monkeypatch.setattr(standard_sets, "HARVARD_SENTENCE_COUNT", 20)
    monkeypatch.setattr(standard_sets, "ARCTIC_SENTENCE_COUNT", 20)
    for name, excerpt in (("harvard", HARVARD_EXCERPT), ("arctic", ARCTIC_EXCERPT)):
        digest = hashlib.sha256(excerpt.read_bytes()).hexdigest()
        monkeypatch.setitem(standard_sets.STANDARD_SET_SHA256, name, digest)


def saved_copy(name: str, digest: str) -> Path:
    if SAVED_SETS is None:
        pytest.skip(f"set {SAVED_SETS_VARIABLE} to a directory of saved published lists")
    path = SAVED_SETS / name
    if not path.is_file():
        pytest.skip(f"no saved copy of the published {name} at {path}")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, path
    return path


def fetched_at() -> datetime:
    return datetime.fromisoformat(FETCHED_AT.replace("Z", "+00:00"))


def corpus_exists(root: Path) -> bool:
    return (root / "provenance" / "corpus").exists()


# -- parsing the standard sets -----------------------------------------------------------


def test_harvard_excerpt_parses_into_twenty_prompts_with_list_and_item_ids() -> None:
    payload = HARVARD_EXCERPT.read_bytes()
    sentences = parse_harvard_sentences(payload, expected_count=20)
    assert [sentence.prompt_id for sentence in sentences] == [
        f"harvard-{number:02d}-{item:02d}" for number in (1, 2) for item in range(1, 11)
    ]
    texts = {sentence.prompt_id: sentence.text for sentence in sentences}
    assert texts["harvard-01-01"] == "The birch canoe slid on the smooth planks."
    assert texts["harvard-01-03"] == "It's easy to tell the depth of a well."
    assert texts["harvard-02-08"] == "The soft cushion broke the man's fall."
    assert texts["harvard-02-10"] == "The girl at the booth sold fifty bonds."

    prompt_set = standard_prompt_set(
        "harvard",
        payload,
        fetched_at=fetched_at(),
        url=HARVARD_URL,
        expected_count=20,
        accept_unpinned_source=True,
    )
    assert prompt_set.suite.schema_version == PROMPT_SUITE_V2
    assert len(prompt_set.suite.prompts) == 20
    first = prompt_set.suite.prompts[0]
    assert first == Prompt(
        prompt_id="harvard-01-01",
        text="The birch canoe slid on the smooth planks.",
        category="neutral",
        style="Plain reading, natural pace.",
        expected_seconds=4.2,  # 8 words: round(0.42 * 8 + 0.8, 1)
    )
    assert json.loads(prompt_set.source_reference) == {
        "url": HARVARD_URL,
        "fetched_at": "2026-09-16T04:17:51Z",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "license": HARVARD_LICENSE,
        "unpinned_source_accepted": True,
    }
    assert prompt_set.unpinned_source_accepted is True
    assert prompt_set.pinned_sha256 == HARVARD_FILE_SHA256 != prompt_set.sha256
    assert "IEEE" in HARVARD_LICENSE and "10.1109/IEEESTD.1969.7405210" in HARVARD_LICENSE


def test_arctic_excerpt_parses_into_twenty_prompts_with_utterance_ids() -> None:
    payload = ARCTIC_EXCERPT.read_bytes()
    sentences = parse_arctic_sentences(payload, expected_count=20)
    assert [sentence.prompt_id for sentence in sentences] == [
        f"arctic-a{number:04d}" for number in range(1, 21)
    ]
    assert sentences[0].text == "Author of the danger trail, Philip Steels, etc."
    assert sentences[-1].text == "Clubs and balls and cities grew to be only memories."
    copy_url = COPY_URLS["import-arctic"]
    prompt_set = standard_prompt_set(
        "arctic",
        payload,
        fetched_at=fetched_at(),
        url=copy_url,
        expected_count=20,
        accept_unpinned_source=True,
    )
    assert {prompt.category for prompt in prompt_set.suite.prompts} == {"neutral"}
    assert prompt_set.suite.prompts[4].expected_seconds == standard_expected_seconds(
        "Will we ever forget it."
    )
    assert standard_expected_seconds("Will we ever forget it.") == 2.9
    reference = PromptSourceReference.model_validate_json(prompt_set.source_reference)
    # The URL is recorded exactly as stated, never replaced by the one --fetch would ask.
    assert (reference.url, reference.license) == (copy_url, ARCTIC_LICENSE)
    assert prompt_set.url == copy_url != ARCTIC_URL


def test_the_arctic_license_is_the_verbatim_copying_notice() -> None:
    assert ARCTIC_LICENSE.encode() == ARCTIC_COPYING.read_bytes()


def test_an_excerpt_is_not_a_whole_published_set() -> None:
    """The count check is what refuses a partial file: 720 and 1,132 unless told otherwise."""
    with pytest.raises(CorpusStoreError, match="yields 20 sentences, not the 720") as harvard:
        parse_harvard_sentences(HARVARD_EXCERPT.read_bytes())
    with pytest.raises(CorpusStoreError, match="yields 20 sentences, not the 1132") as arctic:
        standard_prompt_set(
            "arctic", ARCTIC_EXCERPT.read_bytes(), fetched_at=fetched_at(), url=ARCTIC_URL
        )
    assert harvard.value.code == arctic.value.code == "CORPUS.SOURCE_INVALID"
    with pytest.raises(CorpusStoreError, match="yields 20 sentences, not the 21"):
        parse_arctic_sentences(ARCTIC_EXCERPT.read_bytes(), expected_count=21)


def _harvard_page(*lists: tuple[int, tuple[str, ...]], header: str = "<h1>Harvard</h1>") -> bytes:
    body = "".join(
        f"<h2>List {number}</h2>\n<ol>\n" + "".join(f"<li>{item}</li>\n" for item in items)
        for number, items in lists
    )
    return f"<html><body>{header}\n{body}</ol></body></html>".encode()


def _ten(prefix: str) -> tuple[str, ...]:
    return tuple(f"{prefix} sentence {word}." for word in "abcdefghij")


@pytest.mark.parametrize(
    ("payload", "problem"),
    [
        (b"\xff\xfe", "not UTF-8"),
        (_harvard_page((1, _ten("One")), header="<ul><li>Menu</li></ul>"), "before its first"),
        (_harvard_page((1, _ten("One")), (3, _ten("Three"))), "where list 2 belongs"),
        (_harvard_page((1, _ten("One")[:9])), "holds 9 sentences"),
        (_harvard_page((1, (*_ten("One")[:9], "An <i>odd</i> one."))), "contains markup"),
        (_harvard_page((1, (*_ten("One")[:9], "   "))), "harvard-01-10 is empty"),
        (_harvard_page((1, (*_ten("One")[:9], "one SENTENCE a"))), "read the same"),
    ],
)
def test_malformed_harvard_pages_are_refused_whole(payload: bytes, problem: str) -> None:
    with pytest.raises(CorpusStoreError, match=problem) as refused:
        parse_harvard_sentences(payload, expected_count=10)
    assert refused.value.code == "CORPUS.SOURCE_INVALID"


def test_harvard_entities_and_wrapped_lines_are_normalized() -> None:
    items = (*_ten("One")[:9], "Tom &amp; Jerry\n   ran  home.")
    sentences = parse_harvard_sentences(_harvard_page((1, items)), expected_count=10)
    assert sentences[-1].text == "Tom & Jerry ran home."


@pytest.mark.parametrize(
    ("payload", "problem"),
    [
        (b'( arctic_a0001 "One." )\n\n', "line 2 is not an utterance entry"),
        (b'( arctic_c0001 "One." )\n', "line 1 is not an utterance entry"),
        (b'arctic_a0001 "One."\n', "line 1 is not an utterance entry"),
        (b'( arctic_a0001 "Say \\"hi\\"." )\n', "carries an escaped character"),
        (b'( arctic_a0001 "One." )\n( arctic_a0001 "Two." )\n', "repeats sentence id"),
        (b'( arctic_a0001 "" )\n( arctic_a0002 "Two." )\n', "arctic-a0001 is empty"),
        (b'( arctic_a0001 "One two." )\n( arctic_a0002 "one, two!" )\n', "read the same"),
        (b'( arctic_a0001 "Caf\xe9." )\n', "not UTF-8"),
    ],
)
def test_malformed_arctic_lists_are_refused_whole(payload: bytes, problem: str) -> None:
    count = payload.count(b"arctic_")
    with pytest.raises(CorpusStoreError, match=problem) as refused:
        parse_arctic_sentences(payload, expected_count=count)
    assert refused.value.code == "CORPUS.SOURCE_INVALID"


def test_sentences_that_are_no_valid_prompts_and_bad_provenance_are_refused() -> None:
    too_long = b'( arctic_a0001 "' + b"word " * 500 + b'end." )\n'
    with pytest.raises(CorpusStoreError, match="do not form valid prompts") as invalid:
        standard_prompt_set(
            "arctic", too_long, fetched_at=fetched_at(), url=ARCTIC_URL, expected_count=1
        )
    assert invalid.value.code == "CORPUS.SOURCE_INVALID"
    assert "Value error" not in str(invalid.value)
    control = '( arctic_a0001 "Zero​width." )\n'.encode()
    with pytest.raises(CorpusStoreError, match="format or control characters"):
        standard_prompt_set(
            "arctic", control, fetched_at=fetched_at(), url=ARCTIC_URL, expected_count=1
        )
    with pytest.raises(CorpusStoreError, match="source reference is invalid") as naive:
        standard_prompt_set(
            "arctic",
            ARCTIC_EXCERPT.read_bytes(),
            fetched_at=datetime(2026, 9, 16, 4, 17),
            url=ARCTIC_URL,
            expected_count=20,
        )
    assert naive.value.code == "CORPUS.SOURCE_REFERENCE_INVALID"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "ftp://www.festvox.org/cmu_arctic/cmuarctic.data",
        "file:///tmp/cmuarctic.data",
        "https:///no-host",
        "cmuarctic.data",
        "https://[::1/unbalanced",
        "https://www.festvox.org/cmu arctic",
        "https://www.festvox.org/\x1b[2J",
        "https://example.org/" + "x" * 2030,
    ],
)
def test_a_source_url_must_be_a_plain_http_url(url: str) -> None:
    with pytest.raises(CorpusStoreError, match="is not an http\\(s\\) URL") as refused:
        standard_prompt_set(
            "arctic",
            ARCTIC_EXCERPT.read_bytes(),
            fetched_at=fetched_at(),
            url=url,
            expected_count=20,
        )
    assert refused.value.code == "CORPUS.SOURCE_REFERENCE_INVALID"


def test_valid_source_urls_are_recorded_unchanged() -> None:
    for url in (HARVARD_URL, "http://www.festvox.org/cmu_arctic/cmuarctic.data"):
        assert source_url(url) == url


def test_the_published_sets_parse_to_720_and_1132_sentences() -> None:
    harvard_path = saved_copy("harvard.html", HARVARD_FILE_SHA256)
    arctic_path = saved_copy("cmuarctic.data", ARCTIC_FILE_SHA256)
    harvard = standard_prompt_set(
        "harvard", read_standard_set_file(harvard_path), fetched_at=fetched_at(), url=HARVARD_URL
    )
    arctic = standard_prompt_set(
        "arctic", read_standard_set_file(arctic_path), fetched_at=fetched_at(), url=ARCTIC_URL
    )
    assert len(harvard.suite.prompts) == 720 and len(arctic.suite.prompts) == 1_132
    assert harvard.sha256 == HARVARD_FILE_SHA256 and arctic.sha256 == ARCTIC_FILE_SHA256
    # The saved copies are the pinned published lists, so no override is recorded.
    assert standard_sets.STANDARD_SET_SHA256 == {
        "harvard": HARVARD_FILE_SHA256,
        "arctic": ARCTIC_FILE_SHA256,
    }
    for prompt_set in (harvard, arctic):
        assert prompt_set.unpinned_source_accepted is False
        assert "unpinned_source_accepted" not in json.loads(prompt_set.source_reference)
    arctic_ids = [prompt.prompt_id for prompt in arctic.suite.prompts]
    assert sum(prompt_id.startswith("arctic-a") for prompt_id in arctic_ids) == 593
    assert sum(prompt_id.startswith("arctic-b") for prompt_id in arctic_ids) == 539
    prompts = (*harvard.suite.prompts, *arctic.suite.prompts)
    seconds = [prompt.expected_seconds for prompt in prompts]
    assert (min(seconds), max(seconds)) == (1.2, 6.3)
    assert round(sum(seconds) / 3600, 2) == 2.25
    texts = [prompt.text for prompt in prompts]
    spoken_pilot = {prompt.text for prompt in released_pilot_v2().prompts}
    assert len(set(texts)) == len(texts) and not spoken_pilot & set(texts)
    assert SAVED_SETS is not None  # saved_copy skipped otherwise
    parsed = SAVED_SETS / "parsed.json"
    if parsed.is_file():
        # The reference parse kept ARCTIC's quoted text raw; the importer collapses
        # whitespace as it does for Harvard, which changes exactly one sentence.
        expected = json.loads(parsed.read_text(encoding="utf-8"))
        raw = dict(map(tuple, expected["arctic"]))
        assert [k for k, v in raw.items() if " ".join(v.split()) != v] == ["arctic-a0028"]
        assert raw["arctic-a0028"] == "Robbery, bribery, fraud, "
        for name, prompt_set in (("harvard", harvard), ("arctic", arctic)):
            assert [[p.prompt_id, p.text] for p in prompt_set.suite.prompts] == [
                [prompt_id, " ".join(text.split())] for prompt_id, text in expected[name]
            ]


class _Response:
    def __init__(self, status: int, payload: bytes, url: str = "") -> None:
        self.status = status
        self.payload = payload
        self.url = url

    def read(self, size: int = -1, /) -> bytes:
        return self.payload if size < 0 else self.payload[:size]

    def geturl(self) -> str:
        return self.url

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *arguments: object) -> None:
        return None


def test_fetching_is_bounded_records_where_the_list_came_from_and_names_every_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[Any, float]] = []

    def opener(response: _Response | Exception) -> Callable[..., _Response]:
        def open_url(request: Any, *, timeout: float) -> _Response:
            requests.append((request, timeout))
            if isinstance(response, Exception):
                raise response
            return response

        return open_url

    moment = datetime(2026, 9, 16, 12, tzinfo=UTC)
    payload, when, url = fetch_standard_set(
        "harvard",
        opener=opener(_Response(200, b"page", HARVARD_URL)),
        clock=lambda: moment,
        timeout=5.0,
    )
    assert (payload, when, url) == (b"page", moment, HARVARD_URL)
    assert requests[-1][0].full_url == HARVARD_URL and requests[-1][1] == 5.0
    # A redirect is recorded as the URL the list was actually read from.
    moved = "https://festvox.org/cmu_arctic/cmuarctic.data"
    assert fetch_standard_set("arctic", opener=opener(_Response(200, b"", moved)))[2] == moved
    assert requests[-1][0].full_url == ARCTIC_URL
    for failure, problem in (
        (_Response(203, b"page", ARCTIC_URL), "answered HTTP 203"),
        (OSError("offline"), "cannot fetch .*offline"),
    ):
        with pytest.raises(CorpusStoreError, match=problem) as refused:
            fetch_standard_set("arctic", opener=opener(failure))
        assert refused.value.code == "CORPUS.SOURCE_UNAVAILABLE"
    with pytest.raises(CorpusStoreError, match="is not an http") as elsewhere:
        fetch_standard_set("arctic", opener=opener(_Response(200, b"", "file:///etc/passwd")))
    assert elsewhere.value.code == "CORPUS.SOURCE_REFERENCE_INVALID"
    monkeypatch.setattr(standard_sets, "MAX_STANDARD_SET_BYTES", 3)
    with pytest.raises(CorpusStoreError, match="larger than 3 bytes") as oversized:
        fetch_standard_set("harvard", opener=opener(_Response(200, b"page", HARVARD_URL)))
    assert oversized.value.code == "CORPUS.SOURCE_INVALID"


def _without_blocking(call: Callable[[], object], fifo: Path) -> BaseException | None:
    """Run ``call`` on a helper thread; a FIFO open that blocks fails the test, then unblocks."""
    outcome: list[BaseException | None] = []

    def run() -> None:
        try:
            call()
        except BaseException as error:  # the refusal is the expected outcome
            outcome.append(error)
        else:
            outcome.append(None)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=5)
    if worker.is_alive():
        # Release the blocked reader so the helper thread ends.
        descriptor = os.open(fifo, os.O_WRONLY)
        os.close(descriptor)
        worker.join(timeout=5)
        pytest.fail(f"opening the FIFO {fifo} blocked")
    return outcome[0]


def test_a_saved_copy_is_one_bounded_regular_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy = tmp_path / "copy.data"
    copy.write_bytes(ARCTIC_EXCERPT.read_bytes())
    assert read_standard_set_file(copy) == ARCTIC_EXCERPT.read_bytes()
    link = tmp_path / "link.data"
    link.symlink_to(copy)
    with pytest.raises(SecurityError):
        read_standard_set_file(link)
    with pytest.raises(SecurityError):
        read_standard_set_file(tmp_path)
    fifo = tmp_path / "copy.fifo"
    os.mkfifo(fifo)
    refused = _without_blocking(lambda: read_standard_set_file(fifo), fifo)
    assert isinstance(refused, SecurityError) and "expected a regular file" in str(refused)
    monkeypatch.setattr(standard_sets, "MAX_STANDARD_SET_BYTES", 10)
    with pytest.raises(CorpusStoreError, match="exceeds 10 bytes"):
        read_standard_set_file(copy)


def test_operator_files_named_as_fifos_are_refused_without_blocking(
    cli: Cli, root: Path, tmp_path: Path
) -> None:
    seeded(cli, root)
    fifo = tmp_path / "suite.fifo"
    os.mkfifo(fifo)
    base = ("--asset-root", str(root))
    results: list[tuple[int, str, str]] = []
    for arguments in (
        ("catalog", "import-json", *base, str(fifo)),
        (
            "catalog",
            "import-harvard",
            *base,
            "--from-file",
            str(fifo),
            "--fetched-at",
            FETCHED_AT,
            "--source-url",
            HARVARD_URL,
        ),
    ):
        call = lambda arguments=arguments: results.append(cli(*arguments))  # noqa: E731
        assert _without_blocking(call, fifo) is None
        status, _, err = results[-1]
        assert status == 2 and "expected a regular file" in err, arguments
    with store_of(root) as store:
        assert len(store.list_prompts(include_retired=True)) == 34


# -- catalog init ------------------------------------------------------------------------


def test_init_from_pilot_v2_without_room_tones_seeds_34_prompts_hashed_like_the_built_in(
    cli: Cli, root: Path
) -> None:
    printed = seeded(cli, root)
    assert printed["prompt_count"] == 34
    assert printed["dropped_prompt_ids"] == list(ROOM_TONES)
    assert printed["seeded_from"] == "pilot-v2" and printed["active_subject_id"] == SUBJECT
    assert printed["store"] == "provenance/corpus/corpus.sqlite3"
    reference = CatalogReference.model_validate(printed["reference"])
    assert (reference.catalog_id, reference.catalog_version) == ("cole-corpus", 1)
    assert (reference.suite_id, reference.purpose) == ("cole-corpus", "capture-pilot")
    assert printed["catalog"]["versions"] == [printed["reference"]]
    built_in = {prompt.prompt_id: prompt for prompt in released_pilot_v2().prompts}
    with store_of(root) as store:
        records = store.list_prompts(include_retired=True)
        suite, active = store.active_catalog(SUBJECT)
    assert len(records) == 34 and active == reference
    for record in records:
        assert record.source == "pilot-v2" and record.revision == 1
        assert record.prompt == built_in[record.prompt.prompt_id]
        assert record.prompt_sha256 == canonical_sha256(built_in[record.prompt.prompt_id])
    assert suite == PromptSuite(
        schema_version=PROMPT_SUITE_V2,
        suite_id="cole-corpus",
        purpose="capture-pilot",
        prompts=tuple(p for p in released_pilot_v2().prompts if p.prompt_id not in ROOM_TONES),
    )
    snapshot = root / reference.snapshot_path
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600


def test_init_with_room_tones_reproduces_the_released_digest_when_the_suite_id_matches(
    cli: Cli, tmp_path: Path
) -> None:
    """36 prompts; the digest differs from pilot-v2's only through the suite id."""
    same = AssetLayout.initialize(tmp_path.resolve() / "same" / "assets").root
    printed = init(cli, same, "--from-builtin", "pilot-v2", "--suite-id", "cole-pilot-v2")
    assert printed["prompt_count"] == 36 and printed["dropped_prompt_ids"] == []
    assert printed["reference"]["catalog_sha256"] == PILOT_V2_SHA256

    default = AssetLayout.initialize(tmp_path.resolve() / "default" / "assets").root
    printed = init(cli, default, "--from-builtin", "pilot-v2")
    renamed = released_pilot_v2().model_copy(update={"suite_id": "cole-corpus"})
    assert printed["reference"]["catalog_sha256"] == canonical_sha256(renamed)
    assert printed["reference"]["catalog_sha256"] != PILOT_V2_SHA256


def test_init_refuses_bad_input_before_the_store_exists(cli: Cli, root: Path) -> None:
    arguments = ("catalog", "init", "--asset-root", str(root), "--subject-id", SUBJECT)
    for extra, code, problem in (
        (
            ("--catalog-id", "pilot-v2", "--from-builtin", "pilot-v2"),
            "CATALOG_CLI.CATALOG_ID_RESERVED",
            "built-in prompt suite",
        ),
        (
            ("--catalog-id", "pilot", "--purpose", "capture-pilot"),
            "CATALOG_CLI.CATALOG_ID_RESERVED",
            "built-in prompt suite",
        ),
        ((), "CATALOG_CLI.USAGE_INVALID", "requires --purpose"),
        (
            ("--purpose", "capture-pilot", "--drop-room-tones"),
            "CATALOG_CLI.USAGE_INVALID",
            "only to a --from-builtin",
        ),
        (
            ("--from-builtin", "pilot-v1"),
            "CORPUS.CATALOG_INVALID",
            "capture-pilot prompt-suite/v2 catalog: prompt-suite/v2 room tone requires",
        ),
        (
            ("--from-builtin", "evaluation-v1", "--purpose", "capture-pilot"),
            "CORPUS.CATALOG_INVALID",
            "holdout flags",
        ),
        (
            ("--from-builtin", "pilot-v2", "--catalog-id", "Bad Id"),
            "CORPUS.CATALOG_INVALID",
            "is not a stable id",
        ),
        (
            ("--from-builtin", "pilot-v2", "--description", "x" * 1100),
            "CORPUS.CATALOG_INVALID",
            "description exceeds 1024 characters",
        ),
        (
            ("--from-builtin", "pilot-v2", "--description", "Living\ncorpus"),
            "CORPUS.CATALOG_INVALID",
            "control, format or line-separator",
        ),
        (
            ("--from-builtin", "pilot-v2", "--suite-id", "cole-physical-test-x"),
            "CORPUS.CATALOG_INVALID",
            "cannot name a capture-pilot catalog",
        ),
    ):
        refusal = cli.refused(*arguments, *extra, code=code)
        assert problem in refusal, extra
        assert "Value error" not in refusal
        assert not corpus_exists(root), extra
    refusal = cli.refused(
        "catalog",
        "init",
        "--asset-root",
        str(root),
        "--subject-id",
        "Cole Self",
        "--from-builtin",
        "pilot-v2",
        code="CORPUS.SUBJECT_INVALID",
    )
    assert "subject id" in refusal
    assert not corpus_exists(root)
    assert "built-in prompt suite" in cli.refused(
        "catalog",
        "create",
        "evaluation",
        "--asset-root",
        str(root),
        "--purpose",
        "capture-pilot",
        code="CATALOG_CLI.CATALOG_ID_RESERVED",
    )


def test_init_stores_seed_version_and_activation_together_or_not_at_all(
    cli: Cli, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_: object) -> None:
        raise CorpusStoreError("CORPUS.STORE_UNAVAILABLE", "activation failed")

    monkeypatch.setattr(CorpusStore, "_activate", refuse)
    assert "activation failed" in cli.refused(
        "catalog",
        "init",
        "--asset-root",
        str(root),
        "--subject-id",
        SUBJECT,
        "--from-builtin",
        "pilot-v2",
        "--drop-room-tones",
        code="CORPUS.STORE_UNAVAILABLE",
    )
    monkeypatch.undo()
    status, out, _ = cli("catalog", "list", "--asset-root", str(root), "--subject-id", SUBJECT)
    assert status == 0
    assert out.splitlines() == [
        "no catalogs",
        "prompts: 0 live · 0 retired",
        f"{SUBJECT} has no active catalog",
    ]
    # Nothing was kept, so the same init now simply succeeds.
    printed = seeded(cli, root)
    assert printed["prompt_count"] == 34 and printed["reference"]["catalog_version"] == 1


def test_a_second_init_names_the_command_that_continues_from_the_stores_state(
    cli: Cli, tmp_path: Path
) -> None:
    def fresh(name: str) -> tuple[Path, AssetLayout]:
        layout = AssetLayout.initialize(tmp_path.resolve() / name / "assets")
        return layout.root, layout

    def again(root: Path) -> str:
        refusal = cli.refused(
            "catalog",
            "init",
            "--asset-root",
            str(root),
            "--subject-id",
            SUBJECT,
            "--from-builtin",
            "pilot-v2",
            code="CORPUS.CATALOG_DUPLICATE",
        )
        assert "`voice-lab catalog init` never seeds it again; next step: " in refusal
        assert f"(inspect it with `voice-lab catalog list --subject-id {SUBJECT}`)" in refusal
        return refusal

    prompts_only, layout = fresh("prompts-only")
    with open_corpus_store(layout, create=True) as store:
        store.import_prompt_suite(released_pilot_v2(), source="pilot-v2")
    refusal = again(prompts_only)
    assert "already holds 36 prompt(s) and 0 catalog(s)" in refusal
    assert "`voice-lab catalog create <catalog_id> --purpose <purpose>`" in refusal

    unpublished, layout = fresh("unpublished")
    with open_corpus_store(layout, create=True) as store:
        store.import_prompt_suite(released_pilot_v2(), source="pilot-v2")
        store.create_catalog("cole-corpus", purpose="capture-pilot")
    assert (
        "publish cole-corpus (`voice-lab catalog publish --catalog-id cole-corpus`) and "
        f"activate it (`voice-lab catalog activate --subject-id {SUBJECT} --catalog-id "
        "cole-corpus --version 1`)"
    ) in again(unpublished)

    inactive, layout = fresh("inactive")
    with open_corpus_store(layout, create=True) as store:
        store.import_prompt_suite(released_pilot_v2(), source="pilot-v2")
        store.create_catalog("cole-corpus", purpose="capture-pilot")
        store.publish_version("cole-corpus")
        store.add_prompt(
            "extra-01",
            text="One more sentence.",
            category="neutral",
            style="Plain.",
            expected_seconds=2.0,
            source="manual",
        )
        store.publish_version("cole-corpus")
    assert (
        f"activate a published version (`voice-lab catalog activate --subject-id {SUBJECT} "
        "--catalog-id cole-corpus --version 2`)"
    ) in again(inactive)

    active, _ = fresh("active")
    init(cli, active, "--from-builtin", "pilot-v1", "--drop-room-tones")
    with store_of(active) as store:
        assert {record.source for record in store.list_prompts()} == {"pilot-v1"}
    refusal = again(active)
    assert "already holds 34 prompt(s) and 1 catalog(s)" in refusal
    assert f"next step: nothing; {SUBJECT} already records against cole-corpus version 1" in (
        refusal
    )


def test_init_keeps_physical_test_catalogs_on_marked_roots_and_names_their_suite_id(
    cli: Cli, root: Path, tmp_path: Path
) -> None:
    arguments = ("catalog", "init", "--asset-root", str(root), "--subject-id", SUBJECT)
    for extra in (
        ("--from-builtin", "physical-test-v1"),
        ("--from-builtin", "physical-test-v1", "--catalog-id", "cole-physical-test-a"),
        ("--purpose", "physical-test"),
    ):
        assert PHYSICAL_TEST_CATALOG_REFUSAL in cli.refused(*arguments, *extra), extra
    assert not corpus_exists(root)

    marked = tmp_path.resolve() / "marked" / "assets"
    assert (
        main(
            [
                "assets",
                "init",
                "--asset-root",
                str(marked),
                "--diagnostic",
                "physical-test",
                "--subject-id",
                DIAGNOSTIC_SUBJECT,
            ]
        )
        == 0
    )
    cli.capsys.readouterr()
    on_marked = ("catalog", "init", "--asset-root", str(marked))
    assert DIAGNOSTIC_SUBJECT_REFUSAL in cli.refused(
        *on_marked, "--subject-id", SUBJECT, "--from-builtin", "physical-test-v1"
    )
    diagnostic = (*on_marked, "--subject-id", DIAGNOSTIC_SUBJECT)
    assert DIAGNOSTIC_ROOT_REFUSAL in cli.refused(*diagnostic, "--from-builtin", "pilot-v2")
    refusal = cli.refused(
        *diagnostic, "--purpose", "physical-test", code="CATALOG_CLI.USAGE_INVALID"
    )
    assert "pass --suite-id cole-physical-test-<name>" in refusal
    assert not corpus_exists(marked)
    # The only seed a marked root takes works with the default catalog id: the suite id
    # is the built-in's, so version 1 is exactly the released catalog.
    printed = cli.json(*diagnostic, "--from-builtin", "physical-test-v1")
    reference = printed["reference"]
    assert (reference["catalog_id"], reference["suite_id"]) == (
        "cole-corpus",
        "cole-physical-test-v1",
    )
    assert reference["catalog_sha256"] == BUILTIN_PROMPT_SUITE_SHA256["physical-test-v1.json"]
    assert printed["active_subject_id"] == DIAGNOSTIC_SUBJECT


def test_init_without_a_seed_creates_an_empty_catalog_to_fill_publish_and_activate(
    cli: Cli, root: Path
) -> None:
    printed = init(cli, root, "--purpose", "capture-corpus", "--description", "Living corpus")
    assert printed["catalog"]["versions"] == [] and printed["reference"] is None
    assert printed["active_subject_id"] is None and printed["prompt_count"] == 0
    assert printed["catalog"]["description"] == "Living corpus"
    base = ("--asset-root", str(root))
    status, out, _ = cli("catalog", "list", *base, "--subject-id", SUBJECT)
    assert status == 0
    assert out.splitlines() == [
        "cole-corpus · suite cole-corpus · capture-corpus · nothing published",
        "  Living corpus",
        "prompts: 0 live · 0 retired",
        f"{SUBJECT} has no active catalog",
    ]
    assert "catalog cole-corpus has no prompts to publish" in cli.refused(
        "catalog", "publish", *base, code="CORPUS.EMPTY_CATALOG"
    )
    cli.json("catalog", "add-prompt", "first-prompt", *base, "--text", "A first sentence.")
    published = cli.json("catalog", "publish", *base)
    assert published["prompt_count"] == 1 and published["alias_count"] == 0
    assert published["expected_seconds"] == 2.1  # 3 words: round(0.42 * 3 + 0.8, 1)
    activated = cli.json("catalog", "activate", *base, "--subject-id", SUBJECT, "--version", "1")
    assert activated["reference"] == published["reference"]
    assert activated["takes_effect"] == "at the next launch of the recording app"
    listed = cli.json("catalog", "list", *base, "--subject-id", SUBJECT, "--json")
    assert listed["active"] == published["reference"] and listed["live_prompt_count"] == 1


def test_a_second_catalog_is_created_explicitly_and_published_from_the_same_prompts(
    cli: Cli, root: Path
) -> None:
    seeded(cli, root)
    base = ("--asset-root", str(root))
    created = cli.json("catalog", "create", "cole-corpus-b", *base, "--purpose", "capture-corpus")
    assert created["suite_id"] == "cole-corpus-b" and created["versions"] == []
    assert "already exists" in cli.refused(
        "catalog",
        "create",
        "cole-corpus-b",
        *base,
        "--purpose",
        "capture-corpus",
        code="CORPUS.CATALOG_DUPLICATE",
    )
    for description in ("safe\x1b]0;pwned\x07\x1b[2Jwiped", "two\nlines"):
        assert "control, format or line-separator" in cli.refused(
            "catalog",
            "create",
            "cole-corpus-c",
            *base,
            "--purpose",
            "capture-corpus",
            "--description",
            description,
            code="CORPUS.CATALOG_INVALID",
        )
    refusal = cli.refused(
        "catalog",
        "create",
        "cole-eval",
        *base,
        "--purpose",
        "held-out-evaluation",
        code="CORPUS.CATALOG_UNPUBLISHABLE",
    )
    assert "a held-out-evaluation catalog cannot hold the live prompts reference-neutral-01" in (
        refusal
    )
    assert "and 24 more" in refusal
    published = cli.json("catalog", "publish", *base, "--catalog-id", "cole-corpus-b")
    assert published["prompt_count"] == 34
    assert published["reference"]["purpose"] == "capture-corpus"
    listed = cli.json("catalog", "list", *base, "--json")
    assert [item["catalog_id"] for item in listed["catalogs"]] == ["cole-corpus", "cole-corpus-b"]
    assert listed["subject_id"] is None and listed["active"] is None


# -- importers -------------------------------------------------------------------------


def _import(cli: Cli, root: Path, command: str, path: Path, url: str | None = None) -> Any:
    return cli.json(
        "catalog",
        command,
        "--asset-root",
        str(root),
        "--from-file",
        str(path),
        "--fetched-at",
        FETCHED_AT,
        "--source-url",
        url or COPY_URLS[command],
    )


@pytest.mark.parametrize(
    ("command", "path", "first", "last", "license_text"),
    [
        ("import-harvard", HARVARD_EXCERPT, "harvard-01-01", "harvard-02-10", HARVARD_LICENSE),
        ("import-arctic", ARCTIC_EXCERPT, "arctic-a0001", "arctic-a0020", ARCTIC_LICENSE),
    ],
)
def test_importing_an_excerpt_adds_its_prompts_with_their_source_reference(
    cli: Cli,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    path: Path,
    first: str,
    last: str,
    license_text: str,
) -> None:
    seeded(cli, root)
    excerpt_counts(monkeypatch)
    printed = _import(cli, root, command, path)
    source = command.removeprefix("import-")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert printed["source"] == source and printed["prompt_count"] == 20
    assert (printed["first_prompt_id"], printed["last_prompt_id"]) == (first, last)
    assert printed["sha256"] == digest and printed["fetched_at"] == "2026-09-16T04:17:51+00:00"
    assert printed["url"] == COPY_URLS[command]
    with store_of(root) as store:
        records = [r for r in store.list_prompts() if r.prompt.prompt_id.startswith(source)]
    assert len(records) == 20 and records[0].prompt.prompt_id == first
    for record in records:
        assert record.source == source
        assert record.prompt.category == "neutral"
        assert record.prompt.style == "Plain reading, natural pace."
        assert record.prompt.text is not None
        assert record.prompt.expected_seconds == standard_expected_seconds(record.prompt.text)
        assert record.source_reference is not None
        assert json.loads(record.source_reference) == {
            "url": COPY_URLS[command],
            "fetched_at": "2026-09-16T04:17:51Z",
            "sha256": digest,
            "license": license_text,
        }
    published = cli.json("catalog", "publish", "--asset-root", str(root))
    assert published["prompt_count"] == 54 and published["reference"]["catalog_version"] == 2


def test_an_excerpt_imported_as_the_whole_set_is_refused_and_writes_nothing(
    cli: Cli, root: Path
) -> None:
    seeded(cli, root)
    for command, path, count in (
        ("import-harvard", HARVARD_EXCERPT, 720),
        ("import-arctic", ARCTIC_EXCERPT, 1132),
    ):
        refusal = cli.refused(
            "catalog",
            command,
            "--asset-root",
            str(root),
            "--from-file",
            str(path),
            "--fetched-at",
            FETCHED_AT,
            "--source-url",
            COPY_URLS[command],
            code="CORPUS.SOURCE_INVALID",
        )
        assert f"yields 20 sentences, not the {count}" in refusal
    with store_of(root) as store:
        assert len(store.list_prompts(include_retired=True)) == 34


def test_a_duplicate_text_or_a_repeated_import_is_refused_and_nothing_is_written(
    cli: Cli, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded(cli, root)
    excerpt_counts(monkeypatch)
    base = ("--asset-root", str(root))
    cli.json(
        "catalog",
        "add-prompt",
        "my-canoe",
        *base,
        "--text",
        "The birch canoe slid on the smooth planks.",
    )
    refusal = cli.refused(
        "catalog",
        "import-harvard",
        *base,
        "--from-file",
        str(HARVARD_EXCERPT),
        "--fetched-at",
        FETCHED_AT,
        "--source-url",
        HARVARD_URL,
        code="CORPUS.PROMPT_DUPLICATE",
    )
    assert "harvard-01-01 repeats the text of live prompt my-canoe" in refusal
    with store_of(root) as store:
        ids = [record.prompt.prompt_id for record in store.list_prompts(include_retired=True)]
    assert len(ids) == 35 and not any(prompt_id.startswith("harvard") for prompt_id in ids)
    assert "repeats the text" in cli.refused(
        "catalog",
        "add-prompt",
        "another-canoe",
        *base,
        "--text",
        "The birch canoe slid on the smooth planks.",
        code="CORPUS.PROMPT_DUPLICATE",
    )

    _import(cli, root, "import-arctic", ARCTIC_EXCERPT)
    again = cli.refused(
        "catalog",
        "import-arctic",
        *base,
        "--from-file",
        str(ARCTIC_EXCERPT),
        "--fetched-at",
        FETCHED_AT,
        "--source-url",
        COPY_URLS["import-arctic"],
        code="CORPUS.PROMPT_DUPLICATE",
    )
    assert "already holds arctic-a0001, the first sentence of the arctic set" in again
    with store_of(root) as store:
        assert len(store.list_prompts(include_retired=True)) == 55


def test_a_set_sentence_that_reads_like_a_live_prompt_is_refused_naming_both(
    cli: Cli, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case and punctuation apart, harvard-01-01 is a prompt the store already holds."""
    seeded(cli, root)
    excerpt_counts(monkeypatch)
    base = ("--asset-root", str(root))
    cli.json(
        "catalog",
        "add-prompt",
        "my-canoe",
        *base,
        "--text",
        "the Birch canoe slid on the smooth planks",
    )
    refusal = cli.refused(
        "catalog",
        "import-harvard",
        *base,
        "--from-file",
        str(HARVARD_EXCERPT),
        "--fetched-at",
        FETCHED_AT,
        "--source-url",
        HARVARD_URL,
        code="CORPUS.PROMPT_DUPLICATE",
    )
    assert (
        "prompt harvard-01-01 reads the same as live prompt my-canoe (ignoring case, "
        'punctuation and spacing): "The birch canoe slid on the smooth planks." and '
        '"the Birch canoe slid on the smooth planks"'
    ) in refusal
    with store_of(root) as store:
        ids = [record.prompt.prompt_id for record in store.list_prompts(include_retired=True)]
    assert len(ids) == 35 and not any(prompt_id.startswith("harvard") for prompt_id in ids)
    # Once the operator's prompt is retired, the set imports whole.
    cli.json("catalog", "retire-prompt", "my-canoe", *base)
    assert _import(cli, root, "import-harvard", HARVARD_EXCERPT)["prompt_count"] == 20


def test_a_copy_that_is_not_the_published_list_is_imported_only_on_the_operators_word(
    cli: Cli, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither plain-http fetches nor saved copies are authenticated; the pinned digest is."""
    seeded(cli, root)
    # The excerpts parse as whole sets but keep the published lists' pinned digests.
    monkeypatch.setattr(standard_sets, "HARVARD_SENTENCE_COUNT", 20)
    monkeypatch.setattr(standard_sets, "ARCTIC_SENTENCE_COUNT", 20)
    assert standard_sets.STANDARD_SET_SHA256 == {
        "harvard": HARVARD_FILE_SHA256,
        "arctic": ARCTIC_FILE_SHA256,
    }
    moment = datetime(2026, 9, 16, 12, tzinfo=UTC)
    monkeypatch.setattr(
        standard_sets,
        "fetch_standard_set",
        lambda name: (ARCTIC_EXCERPT.read_bytes(), moment, ARCTIC_URL),
    )
    base = ("--asset-root", str(root))
    harvard_digest = hashlib.sha256(HARVARD_EXCERPT.read_bytes()).hexdigest()
    copy = (
        "catalog",
        "import-harvard",
        *base,
        "--from-file",
        str(HARVARD_EXCERPT),
        "--fetched-at",
        FETCHED_AT,
        "--source-url",
        HARVARD_URL,
    )
    refusal = cli.refused(*copy, code="CORPUS.SOURCE_INVALID")
    assert (
        f"the harvard copy has sha256 {harvard_digest}, not {HARVARD_FILE_SHA256}, the "
        "published list's; nothing was imported"
    ) in refusal
    assert "pass --accept-unpinned-source to import this one" in refusal
    fetched = cli.refused(
        "catalog", "import-arctic", *base, "--fetch", code="CORPUS.SOURCE_INVALID"
    )
    assert f"not {ARCTIC_FILE_SHA256}, the published list's" in fetched
    with store_of(root) as store:
        assert len(store.list_prompts(include_retired=True)) == 34

    for arguments, source, digest, pinned in (
        (copy, "harvard", harvard_digest, HARVARD_FILE_SHA256),
        (
            ("catalog", "import-arctic", *base, "--fetch"),
            "arctic",
            hashlib.sha256(ARCTIC_EXCERPT.read_bytes()).hexdigest(),
            ARCTIC_FILE_SHA256,
        ),
    ):
        printed = cli.json(*arguments, "--accept-unpinned-source")
        assert (printed["sha256"], printed["pinned_sha256"]) == (digest, pinned)
        assert printed["unpinned_source_accepted"] is True and printed["prompt_count"] == 20
        with store_of(root) as store:
            records = [r for r in store.list_prompts() if r.source == source]
        assert len(records) == 20
        for record in records:
            assert record.source_reference is not None
            reference = json.loads(record.source_reference)
            assert reference["sha256"] == digest
            assert reference["unpinned_source_accepted"] is True


def test_the_published_sets_import_whole_and_publish_1886_prompts(cli: Cli, root: Path) -> None:
    harvard = saved_copy("harvard.html", HARVARD_FILE_SHA256)
    arctic = saved_copy("cmuarctic.data", ARCTIC_FILE_SHA256)
    seeded(cli, root)
    assert _import(cli, root, "import-harvard", harvard)["prompt_count"] == 720
    printed = _import(cli, root, "import-arctic", arctic)
    assert printed["prompt_count"] == 1_132
    assert printed["sha256"] == printed["pinned_sha256"] == ARCTIC_FILE_SHA256
    assert printed["unpinned_source_accepted"] is False
    assert (printed["first_prompt_id"], printed["last_prompt_id"]) == (
        "arctic-a0001",
        "arctic-b0539",
    )
    published = cli.json("catalog", "publish", "--asset-root", str(root))
    assert published["prompt_count"] == 1_886
    ids = [
        prompt["prompt_id"]
        for prompt in cli.json("catalog", "export", "--asset-root", str(root), "--json")["prompts"]
    ]
    assert ids[:34] == [
        p.prompt_id for p in released_pilot_v2().prompts if p.category != "room-tone"
    ]
    assert (ids[34], ids[753], ids[754], ids[-1]) == (
        "harvard-01-01",
        "harvard-72-10",
        "arctic-a0001",
        "arctic-b0539",
    )


def test_fetching_happens_only_behind_the_explicit_flag_and_only_when_it_can_import(
    cli: Cli, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    moment = datetime(2026, 9, 16, 12, tzinfo=UTC)

    def fake_fetch(name: str) -> tuple[bytes, datetime, str]:
        calls.append(name)
        path = HARVARD_EXCERPT if name == "harvard" else ARCTIC_EXCERPT
        return path.read_bytes(), moment, f"https://example.invalid/{name}"

    monkeypatch.setattr(standard_sets, "fetch_standard_set", fake_fetch)
    base = ("--asset-root", str(root))
    # No store: an import that cannot succeed downloads nothing (and reads no file).
    missing = root / "no-such-copy.html"
    assert "voice-lab catalog init" in cli.refused(
        "catalog", "import-arctic", *base, "--fetch", code="CORPUS.STORE_MISSING"
    )
    assert "is not an http(s) URL" in cli.refused(
        "catalog",
        "import-harvard",
        *base,
        "--from-file",
        str(HARVARD_EXCERPT),
        "--fetched-at",
        FETCHED_AT,
        "--source-url",
        "file:///tmp/harvard.html",
        code="CORPUS.SOURCE_REFERENCE_INVALID",
    )
    assert "voice-lab catalog init" in cli.refused(
        "catalog",
        "import-harvard",
        *base,
        "--from-file",
        str(missing),
        "--fetched-at",
        FETCHED_AT,
        "--source-url",
        HARVARD_URL,
        code="CORPUS.STORE_MISSING",
    )
    assert calls == [] and not missing.exists() and not corpus_exists(root)

    seeded(cli, root)
    excerpt_counts(monkeypatch)
    _import(cli, root, "import-harvard", HARVARD_EXCERPT)
    assert calls == []
    printed = cli.json("catalog", "import-arctic", *base, "--fetch")
    assert calls == ["arctic"] and printed["prompt_count"] == 20
    assert printed["url"] == "https://example.invalid/arctic"
    assert printed["fetched_at"] == moment.isoformat()
    with store_of(root) as store:
        reference = store.list_prompts()[-1].source_reference
    assert reference is not None
    assert json.loads(reference)["url"] == "https://example.invalid/arctic"
    # The set is in the store now, so neither list is downloaded again.
    for command in ("import-arctic", "import-harvard"):
        assert "is imported already; nothing was read, fetched or written" in cli.refused(
            "catalog", command, *base, "--fetch", code="CORPUS.PROMPT_DUPLICATE"
        )
    assert calls == ["arctic"]

    # A store that cannot answer whether the set is there downloads nothing either.
    history = CorpusStore.prompt_history

    def unavailable(self: CorpusStore, prompt_id: str) -> Any:
        raise CorpusStoreError("CORPUS.STORE_UNAVAILABLE", "corpus store query failed: busy")

    monkeypatch.setattr(CorpusStore, "prompt_history", unavailable)
    assert "busy" in cli.refused(
        "catalog", "import-harvard", *base, "--fetch", code="CORPUS.STORE_UNAVAILABLE"
    )
    monkeypatch.setattr(CorpusStore, "prompt_history", history)
    assert calls == ["arctic"]

    for extra in (("--fetched-at", FETCHED_AT), ("--source-url", ARCTIC_URL)):
        assert "describe a --from-file copy" in cli.refused(
            "catalog", "import-arctic", *base, "--fetch", *extra, code="CATALOG_CLI.USAGE_INVALID"
        )
    copy = ("catalog", "import-harvard", *base, "--from-file", str(HARVARD_EXCERPT))
    for extra in ((), ("--fetched-at", FETCHED_AT), ("--source-url", HARVARD_URL)):
        assert "requires --fetched-at and --source-url" in cli.refused(
            *copy, *extra, code="CATALOG_CLI.USAGE_INVALID"
        )
    parser = build_parser()
    for arguments in (
        ["catalog", "import-harvard"],
        ["catalog", "import-harvard", "--fetch", "--from-file", str(HARVARD_EXCERPT)],
        ["catalog", "import-arctic", "--from-file", "x", "--fetched-at", "2026-09-16T04:17:51"],
    ):
        with pytest.raises(SystemExit) as refused:
            parser.parse_args(arguments)
        assert refused.value.code == 2
    assert calls == ["arctic"]


def test_import_json_adds_every_prompt_of_a_suite_file_once(
    cli: Cli, root: Path, tmp_path: Path
) -> None:
    seeded(cli, root)
    suite = PromptSuite(
        schema_version=PROMPT_SUITE_V2,
        suite_id="operator-suite",
        purpose="capture-corpus",
        prompts=tuple(
            Prompt(
                prompt_id=f"operator-{number}",
                text=f"Operator sentence number {number}.",
                category="conversational",
                style="Relaxed.",
                expected_seconds=3.0,
            )
            for number in (1, 2)
        ),
    )
    path = tmp_path / "suite.json"
    path.write_bytes(canonical_json_bytes(suite))
    base = ("--asset-root", str(root))
    printed = cli.json("catalog", "import-json", *base, str(path))
    assert printed == {
        "prompt_count": 2,
        "prompt_ids": ["operator-1", "operator-2"],
        "source": "import",
        "suite_id": "operator-suite",
        "suite_sha256": canonical_sha256(suite),
    }
    assert "prompt id already exists: operator-1" in cli.refused(
        "catalog", "import-json", *base, str(path), code="CORPUS.PROMPT_DUPLICATE"
    )
    with store_of(root) as store:
        records = store.list_prompts()
    assert [r.prompt for r in records[-2:]] == list(suite.prompts)
    assert {r.source for r in records[-2:]} == {"import"}


def test_importing_needs_an_existing_store(cli: Cli, root: Path) -> None:
    refusal = cli.refused(
        "catalog",
        "import-harvard",
        "--asset-root",
        str(root),
        "--from-file",
        str(HARVARD_EXCERPT),
        "--fetched-at",
        FETCHED_AT,
        "--source-url",
        HARVARD_URL,
        code="CORPUS.STORE_MISSING",
    )
    assert "voice-lab catalog init" in refusal  # the store is checked before the source
    assert "voice-lab catalog init" in cli.refused("catalog", "list", "--asset-root", str(root))
    assert not corpus_exists(root)


# -- prompts that could never publish ---------------------------------------------------


def test_prompts_a_catalog_of_the_store_cannot_publish_are_refused_before_writing(
    cli: Cli, root: Path
) -> None:
    seeded(cli, root)
    base = ("--asset-root", str(root))
    unpublishable = "CORPUS.PROMPT_UNPUBLISHABLE"
    refusal = cli.refused(
        "catalog",
        "add-prompt",
        "held-01",
        *base,
        "--text",
        "A held out sentence.",
        "--holdout",
        code=unpublishable,
    )
    assert (
        "a catalog of this store could never publish held-01 (catalog cole-corpus is "
        "capture-pilot: prompt holdout flags must agree with suite purpose); nothing was written"
    ) in refusal
    assert "only physical-test catalogs may use the test label" in cli.refused(
        "catalog",
        "add-prompt",
        "lbl-1",
        *base,
        "--text",
        "A labelled sentence.",
        "--label",
        "test",
        code=unpublishable,
    )
    # A room tone that carries text fits no prompt-suite/v2 catalog at all.
    refusal = cli.refused(
        "catalog",
        "add-prompt",
        "tone-y",
        *base,
        "--category",
        "room-tone",
        "--text",
        "Silence please.",
        code="CORPUS.PROMPT_INVALID",
    )
    assert "prompt tone-y fits no prompt-suite/v2 catalog of any purpose" in refusal
    assert "room tone requires instructions and no text" in refusal
    for resource, first, more in (
        ("physical-test-v1.json", "p01-setup-identity", 6),
        ("evaluation-v1.json", "eval-short-neutral", 2),
    ):
        refusal = cli.refused(
            "catalog", "import-json", *base, str(resource_path(resource)), code=unpublishable
        )
        assert f"could never publish {first} (catalog cole-corpus is capture-pilot: " in refusal
        assert f"and {more} more; nothing was written" in refusal
    assert "holdout flags" in cli.refused(
        "catalog", "edit-prompt", "neutral-01", *base, "--holdout", code=unpublishable
    )
    with store_of(root) as store:
        assert len(store.list_prompts(include_retired=True)) == 34
        assert len(store.prompt_history("neutral-01")) == 1
    cli.json("catalog", "add-prompt", "extra-01", *base, "--text", "A publishable sentence.")
    assert cli.json("catalog", "publish", *base)["prompt_count"] == 35


def test_a_store_without_catalogs_takes_any_prompt_and_a_catalog_must_fit_them(
    cli: Cli, root: Path
) -> None:
    with open_corpus_store(AssetLayout.validate(root), create=True):
        pass
    base = ("--asset-root", str(root))
    held = cli.json(
        "catalog", "add-prompt", "held-01", *base, "--text", "A held out sentence.", "--holdout"
    )
    assert held["prompt"]["holdout"] is True
    assert "cannot hold the live prompts held-01" in cli.refused(
        "catalog",
        "create",
        "capture",
        *base,
        "--purpose",
        "capture-pilot",
        code="CORPUS.CATALOG_UNPUBLISHABLE",
    )
    cli.json("catalog", "create", "evaluation-set", *base, "--purpose", "held-out-evaluation")
    published = cli.json("catalog", "publish", *base, "--catalog-id", "evaluation-set")
    assert published["reference"]["purpose"] == "held-out-evaluation"


# -- publish, show, export, activate ------------------------------------------------------


def test_publish_show_export_and_activate_name_exact_versions(
    cli: Cli, root: Path, tmp_path: Path
) -> None:
    first = seeded(cli, root)["reference"]
    base = ("--asset-root", str(root))
    assert "already published as catalog cole-corpus version 1" in cli.refused(
        "catalog", "publish", *base, code="CORPUS.VERSION_UNCHANGED"
    )
    cli.json("catalog", "add-prompt", "extra-01", *base, "--text", "One more sentence here.")
    second = cli.json("catalog", "publish", *base)["reference"]
    assert second["catalog_version"] == 2

    shown = cli.json("catalog", "show", *base, "--json")
    assert shown["reference"] == second
    assert shown["snapshot"]["catalog_sha256"] == second["catalog_sha256"]
    assert len(shown["snapshot"]["suite"]["prompts"]) == 35
    assert cli.json("catalog", "show", *base, "--version", "1", "--json")["reference"] == first
    status, out, _ = cli("catalog", "show", *base, "--version", "1")
    assert status == 0
    lines = out.split("\n\n")
    assert lines[0] == (
        f"cole-corpus version 1 · 0 kept-recording aliases · catalog {first['catalog_sha256']}\n"
        "cole-corpus · 34 prompts · 4.7 expected minutes"
    )
    assert lines[1].startswith("reference-neutral-01 · reference, reference-candidate\n")

    exported = cli.json("catalog", "export", *base, "--version", "2", "--json")
    assert exported == shown["snapshot"]["suite"]
    exported_suite = PromptSuite.model_validate_json(json.dumps(exported))
    assert canonical_sha256(exported_suite) == second["catalog_sha256"]
    # An export is exactly what import-json reads: it seeds another library's store.
    export_path = tmp_path / "export.json"
    export_path.write_text(json.dumps(exported), encoding="utf-8")
    other = AssetLayout.initialize(tmp_path.resolve() / "other" / "assets").root
    init(cli, other, "--purpose", "capture-pilot")
    imported = cli.json("catalog", "import-json", "--asset-root", str(other), str(export_path))
    assert imported["prompt_count"] == 35 and imported["suite_sha256"] == second["catalog_sha256"]
    with pytest.raises(SystemExit):
        build_parser().parse_args(["catalog", "export", *base])
    assert "the following arguments are required: --json" in cli.capsys.readouterr().err
    assert "catalog cole-corpus has no version 3" in cli.refused(
        "catalog", "show", *base, "--version", "3", code="CORPUS.VERSION_UNKNOWN"
    )
    assert "unknown catalog: other" in cli.refused(
        "catalog",
        "export",
        *base,
        "--catalog-id",
        "other",
        "--json",
        code="CORPUS.CATALOG_UNKNOWN",
    )

    activated = cli.json("catalog", "activate", *base, "--subject-id", SUBJECT, "--version", "1")
    assert activated["reference"] == first and activated["prompt_count"] == 34
    with store_of(root) as store:
        assert store.active_catalog(SUBJECT)[1].model_dump(mode="json") == first
    status, out, _ = cli("catalog", "list", *base, "--subject-id", SUBJECT)
    assert out.splitlines() == [
        f"cole-corpus · suite cole-corpus · capture-pilot · latest version 2 · "
        f"{second['catalog_sha256']}",
        "prompts: 35 live · 0 retired",
        f"{SUBJECT} records against cole-corpus version 1",
    ]
    assert "subject id" in cli.refused(
        "catalog",
        "activate",
        *base,
        "--subject-id",
        "Not A Subject",
        "--version",
        "2",
        code="CORPUS.SUBJECT_INVALID",
    )


@pytest.mark.parametrize(
    "version",
    ["0", "01", "007", "-1", "+1", " 1", "one", "1.0", "١", str(MAX_CATALOG_VERSION + 1)]
    + ["9" * 30, "9" * 5000],
)
def test_a_version_flag_takes_only_a_positive_sqlite_integer(
    cli: Cli, root: Path, version: str
) -> None:
    for command in (
        ["catalog", "show", "--version", version],
        ["catalog", "export", "--version", version, "--json"],
        ["catalog", "activate", "--subject-id", SUBJECT, "--version", version],
        ["catalog", "edit-prompt", "p", "--style", "S.", "--expected-revision", version],
    ):
        with pytest.raises(SystemExit) as refused:
            build_parser().parse_args(command)
        assert refused.value.code == 2
        error = cli.capsys.readouterr().err
        assert "expected a positive whole number of at most" in error
        assert "without a leading zero" in error
    assert build_parser().parse_args(["catalog", "show", "--version", "7"]).version == 7
    largest = str(MAX_CATALOG_VERSION)
    assert build_parser().parse_args(["catalog", "show", "--version", largest]).version == (
        MAX_CATALOG_VERSION
    )
    seeded(cli, root)
    assert f"has no version {largest}" in cli.refused(
        "catalog", "show", "--asset-root", str(root), "--version", largest
    )


def test_activation_keeps_a_physical_test_catalog_off_a_standard_root(cli: Cli, root: Path) -> None:
    # A store holds a physical-test catalog only beside prompts it can publish, so this one
    # is created through the store API (catalog init refuses it on a standard root).
    with open_corpus_store(AssetLayout.validate(root), create=True) as store:
        store.import_prompt_suite(
            PromptSuite.model_validate_json(
                json.dumps(
                    {
                        "schema_version": PROMPT_SUITE_V2,
                        "suite_id": "cole-physical-test-probe",
                        "purpose": "physical-test",
                        "prompts": [
                            {
                                "prompt_id": "probe-step",
                                "category": "procedure",
                                "instructions": "Press Record.",
                                "style": "Follow the step.",
                                "expected_seconds": 5.0,
                                "labels": ["test"],
                            }
                        ],
                    }
                )
            ),
            source="import",
        )
        store.create_catalog("cole-physical-test-probe", purpose="physical-test")
        store.publish_version("cole-physical-test-probe", ("probe-step",))
    refusal = cli.refused(
        "catalog",
        "activate",
        "--asset-root",
        str(root),
        "--subject-id",
        SUBJECT,
        "--catalog-id",
        "cole-physical-test-probe",
        "--version",
        "1",
    )
    assert PHYSICAL_TEST_CATALOG_REFUSAL in refusal
    with store_of(root) as store, pytest.raises(CorpusStoreError) as inactive:
        store.active_catalog(SUBJECT)
    assert inactive.value.code == "CORPUS.NO_ACTIVE_CATALOG"


# -- add, edit, retire -------------------------------------------------------------------


def test_prompts_are_added_edited_and_retired_through_the_cli(cli: Cli, root: Path) -> None:
    seeded(cli, root)
    base = ("--asset-root", str(root))
    added = cli.json("catalog", "add-prompt", "extra-01", *base, "--text", "Four words right here.")
    assert added["prompt"] == {
        "prompt_id": "extra-01",
        "text": "Four words right here.",
        "category": "neutral",
        "style": "Plain reading, natural pace.",
        "reference_candidate": False,
        "holdout": False,
        "expected_seconds": 2.5,
    }
    assert added["revision"] == 1 and added["source"] == "manual"
    assert added["source_reference"] is None and added["retired_at"] is None
    assert "needs --expected-seconds" in cli.refused(
        "catalog",
        "add-prompt",
        "tone-02",
        *base,
        "--category",
        "room-tone",
        "--instructions",
        "Stay silent.",
        code="CATALOG_CLI.USAGE_INVALID",
    )
    silent = cli.json(
        "catalog",
        "add-prompt",
        "tone-02",
        *base,
        "--category",
        "room-tone",
        "--instructions",
        "Stay silent.",
        "--style",
        "Silence.",
        "--expected-seconds",
        "10",
        "--label",
        "quiet-room",
        "--reference-candidate",
    )
    assert silent["prompt"]["labels"] == ["quiet-room"] and "text" not in silent["prompt"]
    assert silent["prompt"]["reference_candidate"] is True

    assert "needs a field to change" in cli.refused(
        "catalog", "edit-prompt", "extra-01", *base, code="CATALOG_CLI.USAGE_INVALID"
    )
    edit = ("catalog", "edit-prompt", "extra-01", *base, "--json")
    dry = cli.json(*edit, "--text", "Four words right there.", "--dry-run")
    assert dry["written"] is False and dry["record"]["revision"] == 2
    assert dry["previous"] == added and dry["recordings"]["count"] == 0
    assert dry["text_change"] == {
        "before": "Four words right here.",
        "after": "Four words right there.",
        "word_edits": 1,
        "character_edits": 1,
        "typo_class": True,
    }
    with store_of(root) as store:
        assert len(store.prompt_history("extra-01")) == 1
    kept = cli.json(*edit, "--text", "Four words right there.", "--keep-recordings")
    assert kept["written"] is True and kept["record"]["revision"] == 2
    assert kept["record"]["keeps_recordings"] is True
    assert kept["record"]["accepted_prompt_sha256s"] == [added["prompt_sha256"]]
    assert "unchanged" in cli.refused(
        *edit, "--text", "Four words right there.", code="CORPUS.PROMPT_UNCHANGED"
    )

    changed = cli.json(
        "catalog",
        "edit-prompt",
        "tone-02",
        *base,
        "--json",
        "--no-labels",
        "--no-reference-candidate",
        "--style",
        "Steps.",
    )
    assert "labels" not in changed["record"]["prompt"]
    assert changed["record"]["prompt"]["reference_candidate"] is False
    assert changed["text_change"] is None
    # A procedure needs the test label, and a test label needs a physical-test catalog.
    assert "fits no prompt-suite/v2 catalog" in cli.refused(
        "catalog",
        "edit-prompt",
        "tone-02",
        *base,
        "--category",
        "procedure",
        code="CORPUS.PROMPT_INVALID",
    )
    assert "catalog cole-corpus is capture-pilot" in cli.refused(
        "catalog",
        "edit-prompt",
        "tone-02",
        *base,
        "--category",
        "procedure",
        "--label",
        "test",
        code="CORPUS.PROMPT_UNPUBLISHABLE",
    )
    spoken_again = cli.json(
        "catalog",
        "edit-prompt",
        "tone-02",
        *base,
        "--json",
        "--category",
        "neutral",
        "--text",
        "Now it is spoken.",
        "--no-instructions",
        "--expected-seconds",
        "3",
    )
    assert spoken_again["record"]["prompt"]["text"] == "Now it is spoken."
    assert "instructions" not in spoken_again["record"]["prompt"]
    assert "fits_no_catalog" not in spoken_again["recordings"]
    assert spoken_again["text_change"] == {
        "before": None,
        "after": "Now it is spoken.",
        "word_edits": None,
        "character_edits": None,
        "typo_class": False,
    }
    assert "only room tone and procedure" in cli.refused(
        "catalog", "edit-prompt", "extra-01", *base, "--no-text", code="CORPUS.PROMPT_INVALID"
    )
    status, out, _ = cli(
        "catalog",
        "edit-prompt",
        "tone-02",
        *base,
        "--category",
        "room-tone",
        "--no-text",
        "--instructions",
        "Stay silent again.",
        "--dry-run",
    )
    assert status == 0
    assert out.splitlines()[0] == 'tone-02 text: "Now it is spoken." -> (no spoken text)'
    assert "--allow-rewording only qualifies --keep-recordings" in cli.refused(
        "catalog",
        "edit-prompt",
        "extra-01",
        *base,
        "--text",
        "Other.",
        "--allow-rewording",
        code="CATALOG_CLI.USAGE_INVALID",
    )

    retired = cli.json("catalog", "retire-prompt", "extra-01", *base)
    assert retired["retired_at"] is not None
    assert "retired and cannot be edited" in cli.refused(
        "catalog",
        "edit-prompt",
        "extra-01",
        *base,
        "--style",
        "Brisk.",
        code="CORPUS.PROMPT_RETIRED",
    )
    assert "already retired" in cli.refused(
        "catalog", "retire-prompt", "extra-01", *base, code="CORPUS.PROMPT_RETIRED"
    )
    assert "unknown prompt: missing" in cli.refused(
        "catalog",
        "edit-prompt",
        "missing",
        *base,
        "--style",
        "Brisk.",
        code="CORPUS.PROMPT_UNKNOWN",
    )
    status, out, _ = cli("catalog", "list", *base)
    assert "prompts: 35 live · 1 retired" in out.splitlines()


def test_edit_prompt_states_the_text_and_the_native_recordings_before_it_writes(
    cli: Cli, library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One kept native take on spoken-0000: counted, invalidated or kept by an edit."""
    subject = "synthetic-subject"
    original = library.recorded.text
    assert original == "Synthetic sentence number 0."
    base = ("--asset-root", str(library.layout.root), "--subject-id", subject)
    edit = ("catalog", "edit-prompt", "spoken-0000", *base)
    rewording = (
        'spoken-0000 text: "Synthetic sentence number 0." -> "Synthetic sentence zero." '
        "(a rewording: more than 2 word edits or 3 character edits)"
    )
    status, out, _ = cli(*edit, "--text", "Synthetic sentence zero.", "--dry-run")
    assert status == 0
    corrected = library.recorded.model_copy(update={"text": "Synthetic sentence zero."})
    assert out.splitlines() == [
        rewording,
        f"spoken-0000: 1 recording of {subject} counts for this prompt now; this edit "
        "invalidates it (not counted until spoken-0000 is recorded again)",
        f"spoken-0000 would be revision 2 · {canonical_sha256(corrected)} · counts recordings "
        "of 0 earlier revision(s)",
        "not written (--dry-run; write it with --expected-revision 1)",
    ]
    # Keeping recordings pairs the old audio with the new text: a rewording is refused.
    refusal = cli.refused(
        *edit,
        "--text",
        "Synthetic sentence zero.",
        "--keep-recordings",
        code="CORPUS.REWORDING_REFUSED",
    )
    assert '"Synthetic sentence number 0." -> "Synthetic sentence zero." is a rewording' in refusal
    assert "add --allow-rewording if the recordings already say the new text" in refusal
    with store_of(library.layout.root) as store:
        assert len(store.prompt_history("spoken-0000")) == 1
    status, out, _ = cli(
        *edit, "--text", "synthetic Sentence number 0!", "--keep-recordings", "--dry-run"
    )
    assert status == 0
    assert out.splitlines()[0] == (
        'spoken-0000 text: "Synthetic sentence number 0." -> "synthetic Sentence number 0!" '
        "(case, punctuation or spacing only)"
    )
    status, out, _ = cli(
        *edit, "--text", "Synthetic sentense number 0.", "--keep-recordings", "--dry-run"
    )
    assert status == 0
    assert out.splitlines()[0].endswith("(a typo-class fix: 1 word edit, 1 character edit)")

    printed_before_write: list[tuple[str, str]] = []
    edit_prompt = CorpusStore.edit_prompt

    def spy(self: CorpusStore, prompt_id: str, **changes: Any) -> Any:
        if not changes.get("dry_run"):
            captured = cli.capsys.readouterr()
            printed_before_write.append((captured.out, captured.err))
        return edit_prompt(self, prompt_id, **changes)

    monkeypatch.setattr(CorpusStore, "edit_prompt", spy)
    status, out, _ = cli(
        *edit, "--text", "Synthetic sentence zero.", "--keep-recordings", "--allow-rewording"
    )
    assert status == 0
    assert printed_before_write == [
        (
            f"{rewording}\n"
            f"spoken-0000: 1 recording of {subject} counts for this prompt now; this edit "
            "keeps it (counted for the new text, which becomes the training transcript)\n",
            "",
        )
    ]
    assert out.splitlines()[0].startswith("spoken-0000 is now revision 2 · ")
    assert out.splitlines()[1].startswith("publish a version (`voice-lab catalog publish`)")

    # The kept take still counts for revision 2 through its alias, so a default edit names
    # it; with --json the preview goes to stderr before the write and stdout is one document.
    invalidated = cli.json(*edit, "--text", "A reworded synthetic sentence.", "--json")
    assert invalidated["recordings"] == {
        "count": 1,
        "outcome": "invalidated",
        "take_ids": [library.take_id],
        "unknown_record_ids": [],
    }
    assert invalidated["text_change"]["typo_class"] is False
    assert invalidated["previous"]["keeps_recordings"] is True
    assert invalidated["record"]["accepted_prompt_sha256s"] == []
    assert printed_before_write[1] == (
        "",
        'spoken-0000 text: "Synthetic sentence zero." -> "A reworded synthetic sentence." '
        "(a rewording: more than 2 word edits or 3 character edits)\n"
        f"spoken-0000: 1 recording of {subject} counts for this prompt now; this edit "
        "invalidates it (not counted until spoken-0000 is recorded again)\n",
    )
    status, out, _ = cli(*edit, "--style", "Brisk.", "--keep-recordings")
    assert status == 0 and out.startswith("spoken-0000 is now revision 4 · ")
    assert printed_before_write[2] == (
        f"spoken-0000: no recordings of {subject} count for this prompt now, so this edit "
        "keeps none\n",
        "",
    )
    other = cli.json(
        "catalog",
        "edit-prompt",
        "spoken-0000",
        "--asset-root",
        str(library.layout.root),
        "--style",
        "Calm.",
        "--json",
        "--dry-run",
    )
    assert other["subject_id"] == "cole-self" and other["recordings"]["count"] == 0


def test_an_edit_writes_only_over_the_revision_it_counted_and_showed(
    cli: Cli, library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = library.layout.root
    base = ("--asset-root", str(root), "--subject-id", "synthetic-subject")
    edit = ("catalog", "edit-prompt", "spoken-0000", *base)
    assert "at revision 1, not the expected revision 2" in cli.refused(
        *edit, "--style", "Brisk.", "--expected-revision", "2", code="CORPUS.PROMPT_CHANGED"
    )
    preview = cli.json(*edit, "--style", "Brisk.", "--dry-run", "--json")
    assert preview["previous"]["revision"] == 1
    counting = prompt_coverage.prompt_recordings

    def race(*arguments: Any, **keywords: Any) -> Any:
        # Another editor lands a default edit while this command counts recordings.
        with store_of(root) as other:
            other.edit_prompt("spoken-0000", style="Somebody else's style.")
        return counting(*arguments, **keywords)

    monkeypatch.setattr(prompt_coverage, "prompt_recordings", race)
    refusal = cli.refused(
        *edit,
        "--text",
        "Synthetic sentence number 0!",
        "--keep-recordings",
        code="CORPUS.PROMPT_CHANGED",
    )
    assert "spoken-0000 is at revision 2, not the expected revision 1" in refusal
    monkeypatch.undo()
    with store_of(root) as store:
        history = store.prompt_history("spoken-0000")
    assert [record.prompt.style for record in history] == [
        "Plain reading, natural pace.",
        "Somebody else's style.",
    ]
    written = cli.json(
        *edit, "--style", "Brisk.", "--expected-revision", "2", "--keep-recordings", "--json"
    )
    assert written["record"]["revision"] == 3
    # The other editor's default edit stopped the take counting, as the preview now says.
    assert written["recordings"]["count"] == 0


def test_edit_prompt_writes_nothing_when_a_recordings_catalog_snapshot_is_gone(
    cli: Cli, library: Library
) -> None:
    root = library.layout.root
    cli.json(
        "catalog",
        "activate",
        "--asset-root",
        str(root),
        "--subject-id",
        "synthetic-subject",
        "--catalog-id",
        "synthetic-corpus",
        "--version",
        "1",
    )
    snapshot = root / library.first[0].snapshot_path
    snapshot.unlink()
    refusal = cli.refused(
        "catalog",
        "edit-prompt",
        "spoken-0000",
        "--asset-root",
        str(root),
        "--subject-id",
        "synthetic-subject",
        "--text",
        "Synthetic sentence zero.",
        code="CORPUS.SNAPSHOT_MISSING",
    )
    assert f"catalog snapshot is missing: {library.first[0].snapshot_path}" in refusal
    with store_of(root) as store:
        assert len(store.prompt_history("spoken-0000")) == 1
    # Listing names the subject's active version, which reads (and here misses) its snapshot.
    assert "catalog snapshot is missing" in cli.refused(
        "catalog", "list", "--asset-root", str(root), "--subject-id", "synthetic-subject"
    )


def test_edit_prompt_names_only_the_records_whose_prompts_cannot_be_told(
    cli: Cli, tmp_path: Path, foreign_pilot_v2_package: tuple[Path, str]
) -> None:
    evidence = build_legacy_root(tmp_path.resolve() / "assets")
    foreign = add_foreign_package(evidence, foreign_pilot_v2_package)
    root = evidence.layout.root
    seeded(cli, root)
    status, out, _ = cli(
        "catalog",
        "edit-prompt",
        spoken(1),
        "--asset-root",
        str(root),
        "--subject-id",
        SUBJECT,
        "--text",
        "Reworded.",
        "--dry-run",
    )
    assert status == 0
    assert out.splitlines()[1:3] == [
        f"{spoken(1)}: 3 recordings of {SUBJECT} count for this prompt now; this edit "
        f"invalidates them (not counted until {spoken(1)} is recorded again)",
        f"  1 record whose prompts cannot be told is not counted: legacy:{foreign}",
    ]


def test_edit_prompt_reads_the_subjects_own_native_package_exactly(
    cli: Cli, tmp_path: Path, analyzer_build: Any
) -> None:
    """A mapped package that holds no take of the edited prompt is not unreadable."""
    layout, package = prepare_pilot_v2_package(
        tmp_path.resolve() / "assets",
        analyzer_build,
        prompt_ids=(*ROOM_TONES, "reference-neutral-01"),
    )
    root = layout.root
    seeded(cli, root)
    report = cli.json(
        "prompts",
        "coverage-report",
        "--asset-root",
        str(root),
        "--subject-id",
        SUBJECT,
        "--catalog",
        "cole-corpus",
        "--json",
    )
    assert report["unknown_record_ids"] == [] and report["covered_prompt_count"] == 1
    retained = {row["prompt_id"]: row["retained_take_count"] for row in report["prompts"]}
    assert retained["reference-neutral-01"] >= 1 and retained["reference-warm-01"] == 0
    for prompt_id in ("reference-warm-01", "reference-neutral-01"):
        printed = cli.json(
            "catalog",
            "edit-prompt",
            prompt_id,
            "--asset-root",
            str(root),
            "--subject-id",
            SUBJECT,
            "--style",
            "Calm.",
            "--dry-run",
            "--json",
        )
        assert printed["recordings"]["unknown_record_ids"] == [], prompt_id
        assert printed["recordings"]["count"] == retained[prompt_id]
        assert "cannot be told" not in cli.capsys.readouterr().err
    assert package.session_id.startswith("native-")


def test_list_reports_an_empty_store(cli: Cli, root: Path) -> None:
    with open_corpus_store(AssetLayout.validate(root), create=True):
        pass
    status, out, _ = cli("catalog", "list", "--asset-root", str(root), "--subject-id", SUBJECT)
    assert status == 0
    assert out.splitlines() == [
        "no catalogs",
        "prompts: 0 live · 0 retired",
        f"{SUBJECT} has no active catalog",
    ]


# -- published catalogs beside the built-ins, on the TC2 legacy root --------------------


@dataclass(frozen=True)
class LegacyCorpus:
    root: Path
    reference: dict[str, Any]


@pytest.fixture(scope="module")
def legacy_corpus(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LegacyCorpus]:
    """The TC2 legacy root with `catalog init --from-builtin pilot-v2 --drop-room-tones`."""
    evidence = build_legacy_root(tmp_path_factory.mktemp("legacy-corpus").resolve() / "assets")
    root = evidence.layout.root
    status = main(
        [
            "catalog",
            "init",
            "--asset-root",
            str(root),
            "--subject-id",
            SUBJECT,
            "--from-builtin",
            "pilot-v2",
            "--drop-room-tones",
        ]
    )
    assert status == 0
    with open_corpus_store(evidence.layout, create=False) as store:
        reference = store.resolve_reference("cole-corpus")
    yield LegacyCorpus(root, reference.model_dump(mode="json"))


def _selection(corpus: dict[str, Any]) -> list[tuple[Any, ...]]:
    return [
        (
            entry["prompt_id"],
            entry["take_id"],
            entry["source_kind"],
            entry["prompt_sha256"],
            entry["recorded_prompt_sha256"],
            entry["source_path"],
            entry["source_sha256"],
        )
        for entry in corpus["entries"]
    ]


def test_finetune_corpus_on_cole_corpus_selects_the_same_takes_as_pilot_v2(
    cli: Cli, legacy_corpus: LegacyCorpus
) -> None:
    base = ("finetune", "corpus", "--asset-root", str(legacy_corpus.root), "--json")
    built_in = cli.json(*base, "--catalog", "pilot-v2")
    published = cli.json(*base, "--catalog", "cole-corpus")
    pinned = cli.json(*base, "--catalog", "cole-corpus@1")
    assert built_in["selected_prompt_count"] == published["selected_prompt_count"] == 34
    assert built_in["complete"] and published["complete"]
    assert _selection(built_in["corpus"]) == _selection(published["corpus"])
    assert len({row[1] for row in _selection(published["corpus"])}) == 34
    assert built_in["catalog_reference"] is None
    assert published["catalog_reference"] == legacy_corpus.reference
    assert published["corpus"]["catalog_reference"] == legacy_corpus.reference
    assert published["catalog_suite_id"] == "cole-corpus"
    assert published["catalog_sha256"] == legacy_corpus.reference["catalog_sha256"]
    assert _selection(pinned["corpus"]) == _selection(published["corpus"])
    status, out, _ = cli(
        "finetune", "corpus", "--asset-root", str(legacy_corpus.root), "--catalog", "cole-corpus"
    )
    assert status == 0 and out.startswith("34/34 spoken prompts have a latest kept take")


def test_coverage_of_cole_corpus_against_pilot_v2_changes_only_the_dropped_room_tones(
    cli: Cli, legacy_corpus: LegacyCorpus
) -> None:
    base = ("prompts", "coverage-report", "--asset-root", str(legacy_corpus.root))
    base += ("--subject-id", SUBJECT)
    report = cli.json(*base, "--catalog", "cole-corpus", "--baseline", "pilot-v2", "--json")
    assert (report["covered_prompt_count"], report["total_prompt_count"]) == (34, 34)
    assert report["changed_prompt_ids"] == list(ROOM_TONES)
    assert report["lost_prompt_ids"] == [] and report["missing_prompt_ids"] == []
    assert report["exit_status"] == 0
    assert (report["catalog_id"], report["catalog_version"]) == ("cole-corpus", 1)
    assert report["catalog_sha256"] == legacy_corpus.reference["catalog_sha256"]
    assert (report["baseline_catalog_id"], report["baseline_catalog_version"]) == (None, None)
    assert report["baseline_catalog_sha256"] == PILOT_V2_SHA256
    status, out, _ = cli(*base, "--catalog", "cole-corpus@1", "--baseline", "pilot-v2")
    assert status == 0
    assert out.splitlines()[:3] == [
        "34/34 prompts covered · catalog cole-corpus@1 "
        + legacy_corpus.reference["catalog_sha256"],
        "missing: -",
        f"changed: {', '.join(ROOM_TONES)}",
    ]
    reverse = cli.json(*base, "--catalog", "pilot-v2", "--baseline", "cole-corpus", "--json")
    assert reverse["catalog_id"] is None and reverse["baseline_catalog_id"] == "cole-corpus"
    assert reverse["changed_prompt_ids"] == [] and reverse["missing_prompt_ids"] == list(ROOM_TONES)
    status, out, _ = cli(*base, "--catalog", "pilot-v2")
    assert out.splitlines()[0] == f"34/36 prompts covered · catalog {PILOT_V2_SHA256}"


def test_edit_prompt_counts_legacy_recordings_like_the_coverage_report(
    cli: Cli, legacy_corpus: LegacyCorpus
) -> None:
    layout = AssetLayout.validate(legacy_corpus.root)
    report = prompt_coverage_report(layout, SUBJECT, released_pilot_v2())
    expected = {row.prompt_id: row.retained_take_count for row in report.prompts}
    assert expected[spoken(1)] == 3  # finalized, in-progress and sidecar-less sessions
    for number in (1, 3, 4):
        printed = cli.json(
            "catalog",
            "edit-prompt",
            spoken(number),
            "--asset-root",
            str(legacy_corpus.root),
            "--subject-id",
            SUBJECT,
            "--text",
            "Reworded.",
            "--dry-run",
            "--json",
        )
        assert printed["recordings"]["count"] == expected[spoken(number)], number
        assert all(take.startswith("legacy") for take in printed["recordings"]["take_ids"])
        assert printed["written"] is False
    with store_of(legacy_corpus.root) as store:
        assert all(len(store.prompt_history(spoken(n))) == 1 for n in (1, 3, 4))


# -- the --catalog form --------------------------------------------------------------------


def _commands(root: Path, name: str) -> list[list[str]]:
    report = ["prompts", "coverage-report", "--asset-root", str(root), "--subject-id", SUBJECT]
    return [
        ["prompts", "show", name, "--asset-root", str(root)],
        [*report, "--catalog", name],
        [*report, "--catalog", "pilot-v2", "--baseline", name],
        ["finetune", "corpus", "--asset-root", str(root), "--catalog", name],
    ]


def test_an_unknown_catalog_is_refused_by_name_and_code_in_every_command(
    cli: Cli, root: Path, tmp_path: Path
) -> None:
    for name in ("cole-corpus", "cole-corpus@1"):
        for command in _commands(root, name):
            refusal = cli.refused(*command, code="CORPUS.STORE_MISSING")
            assert f"{UNKNOWN}: {name} (no corpus store" in refusal, command
    assert not corpus_exists(root)

    seeded(cli, root)
    too_large = f"cole-corpus@{MAX_CATALOG_VERSION + 1}"
    whole = (
        f"(a version is a positive whole number of at most {MAX_CATALOG_VERSION}, without "
        "a leading zero)"
    )
    for name, code, detail in (
        ("cole-corpus@2", "CORPUS.VERSION_UNKNOWN", "(catalog cole-corpus has no version 2)"),
        ("other", "CORPUS.CATALOG_UNKNOWN", "(unknown catalog: other)"),
        ("cole-corpus@0", "CORPUS.VERSION_UNKNOWN", whole),
        ("cole-corpus@01", "CORPUS.VERSION_UNKNOWN", whole),
        ("cole-corpus@one", "CORPUS.VERSION_UNKNOWN", whole),
        ("cole-corpus@", "CORPUS.VERSION_UNKNOWN", whole),
        ("cole-corpus@١", "CORPUS.VERSION_UNKNOWN", whole),
        (too_large, "CORPUS.VERSION_UNKNOWN", whole),
        ("cole-corpus@" + "9" * 5000, "CORPUS.VERSION_UNKNOWN", whole),
        ("../pilot-v2", "CORPUS.CATALOG_UNKNOWN", ""),
        ("Cole-Corpus", "CORPUS.CATALOG_UNKNOWN", ""),
        ("pilot-v2.json", "CORPUS.CATALOG_UNKNOWN", "(unknown catalog: pilot-v2.json)"),
    ):
        for command in _commands(root, name):
            refusal = cli.refused(*command, code=code)
            assert f"{UNKNOWN}: {name}{' ' if detail else ''}{detail}; " in refusal, command
            names = PROMPT_SHOW_NAMES if command[1] == "show" else BUILT_INS
            assert refusal.rstrip().endswith(names), refusal
    largest = f"cole-corpus@{MAX_CATALOG_VERSION}"
    assert f"(catalog cole-corpus has no version {MAX_CATALOG_VERSION})" in cli.refused(
        "prompts", "show", largest, "--asset-root", str(root), code="CORPUS.VERSION_UNKNOWN"
    )

    no_root = ["prompts", "show", "cole-corpus"]
    assert "read from the --asset-root or COLE_VOICE_LAB_ASSET_ROOT library" in cli.refused(
        *no_root, code="CORPUS.STORE_MISSING"
    )
    empty = AssetLayout.initialize(tmp_path.resolve() / "empty" / "assets").root
    with open_corpus_store(AssetLayout.validate(empty), create=True) as store:
        store.create_catalog("cole-corpus", purpose="capture-pilot")
    assert "(catalog cole-corpus has no published version)" in cli.refused(
        "prompts", "show", "cole-corpus", "--asset-root", str(empty)
    )


PROMPT_SHOW_NAMES = (
    "built-in names: pilot, evaluation, pilot-v1, evaluation-v1, pilot-v2, physical-test-v1"
)


def test_help_names_the_built_ins_and_how_a_published_catalog_is_read(
    capsys: pytest.CaptureFixture[str],
) -> None:
    for command in (
        ["prompts", "coverage-report", "--help"],
        ["finetune", "corpus", "--help"],
        ["prompts", "show", "--help"],
    ):
        with pytest.raises(SystemExit):
            build_parser().parse_args(command)
        # argparse wraps help at spaces and hyphens; compare without whitespace.
        text = "".join(capsys.readouterr().out.split())
        assert "pilot-v1,evaluation-v1,pilot-v2,physical-test-v1" in text, command
        assert "resolvedthroughthelibrary'scorpusstore" in text, command
    assert cli_module.MAX_CLI_VERSION == MAX_CATALOG_VERSION


def test_a_published_catalog_is_shown_like_a_built_in_and_through_the_environment(
    cli: Cli, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded(cli, root)
    shown = cli.json("prompts", "show", "cole-corpus", "--asset-root", str(root), "--json")
    assert shown["suite_id"] == "cole-corpus" and len(shown["prompts"]) == 34
    monkeypatch.setenv("COLE_VOICE_LAB_ASSET_ROOT", str(root))
    status, out, _ = cli("prompts", "show", "cole-corpus@1")
    assert status == 0
    assert out.split("\n\n")[0] == "cole-corpus · 34 prompts · 4.7 expected minutes"
    # A built-in name never consults the library, even when one is configured.
    status, out, _ = cli("prompts", "show", "pilot-v2")
    assert status == 0 and out.startswith("cole-pilot-v2 · 36 prompts")


def test_a_published_catalog_whose_snapshot_changed_fails_closed(cli: Cli, root: Path) -> None:
    reference = seeded(cli, root)["reference"]
    snapshot = root / reference["snapshot_path"]
    payload = bytearray(snapshot.read_bytes())
    payload[-2] ^= 1
    snapshot.write_bytes(bytes(payload))
    for command in _commands(root, "cole-corpus"):
        assert "does not have the published bytes" in cli.refused(
            *command, code="CORPUS.SNAPSHOT_MISMATCH"
        )


def test_errors_without_a_code_print_their_message_alone(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Uncoded(SecurityError):
        code = 7

    def fail(_: object) -> int:
        raise Uncoded("plain failure")

    monkeypatch.setattr(cli_module, "build_parser", lambda: _parser_with_handler(fail))
    assert cli.refused("doctor") == "voice-lab: plain failure\n"


def _parser_with_handler(handler: Callable[[object], int]) -> Any:
    import argparse

    parser = argparse.ArgumentParser(prog="voice-lab")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor").set_defaults(handler=handler)
    return parser
