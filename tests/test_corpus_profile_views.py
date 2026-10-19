"""The library seen per capture profile (D-056 step 3, docs/design/HOUSE-PANEL.md §5).

``GET /api/v1/corpus/library`` and a prompt's take history answer the ``umik-1`` view by
default, byte for byte the answer before profiles existed, so the desktop never sees a panel
take; ``?profile=xvf3800-linear-4`` answers the house panel's view. In each view the newest
kept take of the profile stands for a prompt (D-016's rule, per profile).
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from corpus_take_fixtures import make_take, seed_catalog
from panel_take_fixtures import make_profile_take

from cole_voice_lab.api.corpus_routes import LIBRARY_PATH, CorpusRoutes, library_profile
from cole_voice_lab.api.routing import RouteRefusal, RouteRequest, RouteResponse
from cole_voice_lab.corpus_read_model import (
    build_library,
    library_path,
    read_library,
    take_profile,
)
from cole_voice_lab.corpus_take_store import TakeDecision, read_take_states
from cole_voice_lab.errors import ContractError
from cole_voice_lab.intake_publisher import publish_pending
from cole_voice_lab.library_classification import classify
from cole_voice_lab.models import COLE_SUBJECT
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_contracts import CatalogReference, CatalogSnapshot

type Catalog = tuple[CatalogReference, CatalogSnapshot]
PANEL = "xvf3800-linear-4"


@pytest.fixture
def layout(tmp_path: Path) -> AssetLayout:
    return AssetLayout.initialize(tmp_path / "data" / "voice-lab" / "lab")


@pytest.fixture
def catalog(layout: AssetLayout) -> Catalog:
    return seed_catalog(layout)


def _publish_desktop(layout: AssetLayout, catalog: Catalog, work: Path) -> str:
    take = make_take(work, catalog=catalog)
    shutil.copytree(work, layout.directory("incoming") / take.session_id)
    publish_pending(layout, min_free_bytes=0)
    return take.session_id


def _publish_panel(layout: AssetLayout, catalog: Catalog, work: Path) -> str:
    take = make_profile_take(work, catalog=catalog)
    shutil.copytree(work, layout.directory("incoming") / take.session_id)
    publish_pending(layout, min_free_bytes=0)
    return take.session_id


def _get(routes: CorpusRoutes, path: str, query: str = "", **headers: str) -> RouteResponse | None:
    by_name = {name.casefold(): value for name, value in headers.items()}
    return routes(
        RouteRequest(
            method="GET",
            path=path,
            query=query,
            header=lambda name: by_name.get(name.casefold()),
            content_length=None,
            read_body=lambda _limit: b"",
        )
    )


def test_each_view_holds_its_profiles_takes_only(
    layout: AssetLayout, catalog: Catalog, tmp_path: Path
) -> None:
    desktop = _publish_desktop(layout, catalog, tmp_path / "desktop")
    default_bytes = library_path(layout, COLE_SUBJECT).read_bytes()
    panel = _publish_panel(layout, catalog, tmp_path / "panel")
    # The default view is the umik-1 takes' and never moved: the same bytes, file and tag.
    assert library_path(layout, COLE_SUBJECT).read_bytes() == default_bytes
    assert b'"profile"' not in default_bytes
    found = read_library(layout, COLE_SUBJECT)
    assert found is not None
    default, tag = found
    assert tag == hashlib.sha256(default_bytes).hexdigest()
    assert default.profile is None and default.coverage.takes == 1
    assert [take.take_id for history in default.histories for take in history.takes] == [desktop]
    found = read_library(layout, COLE_SUBJECT, PANEL)
    assert found is not None
    view, _ = found
    assert view.profile == PANEL and view.coverage.takes == 1
    assert [take.take_id for history in view.histories for take in history.takes] == [panel]
    assert view.rows[0].prompt_id == default.rows[0].prompt_id
    assert library_path(layout, COLE_SUBJECT, PANEL).name == f"library-{COLE_SUBJECT}-{PANEL}.json"


def test_the_panels_view_exists_before_its_first_take(
    layout: AssetLayout, catalog: Catalog, tmp_path: Path
) -> None:
    _publish_desktop(layout, catalog, tmp_path / "desktop")
    found = read_library(layout, COLE_SUBJECT, PANEL)
    assert found is not None
    view, _ = found
    assert view.coverage.takes == 0
    assert {row.status for row in view.rows} == {"missing"}


def test_the_newest_kept_take_of_each_profile_stands_for_its_prompt(
    layout: AssetLayout, catalog: Catalog, tmp_path: Path
) -> None:
    # D-016's rule, per profile: a kept panel take does not stand for the prompt in the
    # desktop's view, nor a desktop take in the panel's.
    desktop = _publish_desktop(layout, catalog, tmp_path / "desktop")
    panel = _publish_panel(layout, catalog, tmp_path / "panel")
    states = read_take_states(layout)
    assert {take_profile(state) for state in states} == {"umik-1", PANEL}
    kept = TakeDecision(decision="kept", revision=1, reason=None, decided_at=states[0].accepted_at)
    decided = tuple(
        state.model_copy(update={"decision": kept}) if state.take_id == panel else state
        for state in states
    )
    reference, snapshot = catalog
    default = build_library(COLE_SUBJECT, reference, snapshot, decided)
    view = build_library(COLE_SUBJECT, reference, snapshot, decided, PANEL)
    first = next(prompt for prompt in snapshot.suite.prompts if prompt.spoken)
    row = {row.prompt_id: row for row in default.rows}[first.prompt_id]
    assert (row.status, row.latest_take_id) == ("draft", desktop)
    row = {row.prompt_id: row for row in view.rows}[first.prompt_id]
    assert row.status in {"kept", "needs-attention"} and row.latest_take_id == panel
    assert default.coverage.kept + default.coverage.needs_attention == 0
    assert view.coverage.kept + view.coverage.needs_attention == 1


def test_the_api_answers_each_view_with_its_own_tag(
    layout: AssetLayout, catalog: Catalog, tmp_path: Path
) -> None:
    desktop = _publish_desktop(layout, catalog, tmp_path / "desktop")
    panel = _publish_panel(layout, catalog, tmp_path / "panel")
    routes = CorpusRoutes(layout)
    plain = _get(routes, LIBRARY_PATH)
    assert plain is not None and plain.status == 200
    assert b'"profile"' not in plain.body
    explicit = _get(routes, LIBRARY_PATH, "profile=umik-1")
    assert explicit is not None and explicit.body == plain.body
    assert dict(explicit.headers) == dict(plain.headers)
    found = read_library(layout, COLE_SUBJECT)
    assert found is not None
    assert dict(plain.headers)["ETag"] == f'"{found[1]}"'

    answer = _get(routes, LIBRARY_PATH, f"profile={PANEL}")
    assert answer is not None and answer.status == 200
    body = json.loads(answer.body)
    assert body["profile"] == PANEL and body["coverage"]["takes"] == 1
    tag = dict(answer.headers)["ETag"]
    assert tag != dict(plain.headers)["ETag"]
    unchanged = _get(routes, LIBRARY_PATH, f"profile={PANEL}", **{"If-None-Match": tag})
    assert unchanged is not None and unchanged.status == 304
    stale = _get(routes, LIBRARY_PATH, **{"If-None-Match": tag})
    assert stale is not None and stale.status == 200

    reference, snapshot = catalog
    prompt = next(prompt for prompt in snapshot.suite.prompts if prompt.spoken)
    history_path = f"/api/v1/corpus/prompts/{prompt.prompt_id}/takes"
    mine = _get(routes, history_path)
    theirs = _get(routes, history_path, f"profile={PANEL}")
    assert mine is not None and theirs is not None
    assert [take["take_id"] for take in json.loads(mine.body)["takes"]] == [desktop]
    assert b'"profile"' not in mine.body
    history = json.loads(theirs.body)
    assert [take["take_id"] for take in history["takes"]] == [panel]
    assert history["profile"] == PANEL and dict(theirs.headers)["ETag"] == tag
    # The default view's history is the library's tag, as the desktop relies on.
    assert dict(mine.headers)["ETag"] == dict(plain.headers)["ETag"]


@pytest.mark.parametrize(
    "query",
    [
        "profile=zoom-h6",
        "profile=",
        "profile",
        "view=panel",
        f"profile={PANEL}&profile=umik-1",
        f"profile={PANEL}&format=json",
    ],
)
def test_any_other_query_is_refused(query: str) -> None:
    with pytest.raises(RouteRefusal) as refused:
        library_profile(query)
    assert (refused.value.status, refused.value.code) == (400, "bad_request")


def test_a_view_in_another_profiles_file_is_refused(
    layout: AssetLayout, catalog: Catalog, tmp_path: Path
) -> None:
    _publish_desktop(layout, catalog, tmp_path / "desktop")
    panel_file = library_path(layout, COLE_SUBJECT, PANEL)
    default_file = library_path(layout, COLE_SUBJECT)
    panel_bytes, default_bytes = panel_file.read_bytes(), default_file.read_bytes()
    default_file.write_bytes(panel_bytes)
    with pytest.raises(ContractError, match="not this subject's canonical record"):
        read_library(layout, COLE_SUBJECT)
    panel_file.write_bytes(default_bytes)
    with pytest.raises(ContractError, match="not this subject's canonical record"):
        read_library(layout, COLE_SUBJECT, PANEL)


def test_the_panels_view_is_classified_with_the_read_model() -> None:
    rule = classify(f"derived/corpus/library-{COLE_SUBJECT}-{PANEL}.json").rule
    assert (rule.name, rule.disposition) == ("corpus-read-model", "mac-only")
