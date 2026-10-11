"""The catalogs Record: prompts records from, and a session's batch (phase 4c, D-042).

A catalog is a prompt suite: one of the built-in pilot suites (``pilot-v2.json``, the
default, or ``pilot-v1.json``, read through ``prompt_catalogs`` with their release pins),
or a prompt-suite JSON file the settings name (``catalog_file``). The corpus catalog (D-017)
lives in the corpus store with its pilot gate; neither is served to the app in phase 4
(``GET /api/v1/catalog`` is not built), so a ``capture-corpus`` suite is refused here, as
are held-out evaluation suites (never training audio), physical-test suites (the Swift
app's diagnostics), test steps without spoken text (``procedure``) and a suite without
room tone.

A session records a batch: the prompts chosen, plus every room-tone prompt of the catalog
(D-010), in catalog order. The batch's own suite is what the capture package names: the
catalog itself when every prompt is in the batch, else a subset suite named the way the
lab already names them (``native-subset-<sha>`` for prompt-suite/v2, as
``recording_packages`` does; ``desktop-subset-<sha>`` for v1, as ``recording_library``
does).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from cole_voice_lab.canonical import canonical_sha256
from cole_voice_lab.errors import ContractError, SecurityError
from cole_voice_lab.models import PROMPT_SUITE_V1, PromptSuite
from cole_voice_lab.prompt_catalogs import load_builtin_prompt_suite

#: The built-in catalogs offered, default first: (resource, label).
BUILTIN: Final = (
    ("pilot-v2.json", "Pilot catalog (prompt suite v2)"),
    ("pilot-v1.json", "Pilot catalog (prompt suite v1)"),
)
DEFAULT_SOURCE: Final = "builtin:pilot-v2.json"
FILE_SOURCE: Final = "file"
#: The largest catalog file read (a suite of a few hundred prompts is well under it).
MAX_CATALOG_BYTES: Final = 4 * 1024 * 1024


class CatalogRefused(Exception):
    """This catalog cannot be recorded from here; the message says why."""


@dataclass(frozen=True)
class Catalog:
    """A catalog, checked: where it came from, the suite and its digest."""

    source: str
    label: str
    suite: PromptSuite
    sha256: str


def choices(catalog_file: str | None) -> list[dict[str, str]]:
    """The catalog choices: ``[{value, label}]`` (the settings' file last, when set)."""
    out = [{"value": f"builtin:{name}", "label": label} for name, label in BUILTIN]
    if catalog_file:
        out.append({"value": FILE_SOURCE, "label": f"Catalog file: {Path(catalog_file).name}"})
    return out


def check(suite: PromptSuite) -> PromptSuite:
    """``suite`` when the app may record from it. Raises: ``CatalogRefused``."""
    if suite.purpose == "held-out-evaluation":
        raise CatalogRefused(
            f"{suite.suite_id} is a held-out evaluation catalog: its prompts are never "
            "recorded as training audio"
        )
    if suite.purpose == "physical-test":
        raise CatalogRefused(
            f"{suite.suite_id} is a physical-test catalog: the Swift app's diagnostics, "
            "not something to record here"
        )
    if suite.purpose == "capture-corpus":
        raise CatalogRefused(
            f"{suite.suite_id} is the corpus catalog: it needs the corpus store's catalog and "
            "its pilot gate, which the server does not serve to this app yet (phase 4 "
            "records pilot catalogs; D-042)"
        )
    if any(prompt.category == "procedure" for prompt in suite.prompts):
        raise CatalogRefused(
            f"{suite.suite_id} has test steps without spoken text; they cannot become "
            "capture evidence"
        )
    if not any(prompt.category == "room-tone" for prompt in suite.prompts):
        raise CatalogRefused(f"{suite.suite_id} has no room tone; every session records one")
    return suite


def load(source: str, *, catalog_file: str | None) -> Catalog:
    """The catalog ``source`` names (``builtin:<resource>`` or ``file``), checked.

    Raises: ``CatalogRefused`` (unknown, unreadable, invalid or refused)."""
    if source.startswith("builtin:"):
        resource = source.removeprefix("builtin:")
        labels = dict(BUILTIN)
        if resource not in labels:
            raise CatalogRefused(f"not a built-in catalog offered here: {resource!r}")
        try:
            suite = load_builtin_prompt_suite(resource)
        except (ContractError, SecurityError, OSError, ValidationError) as exc:
            raise CatalogRefused(f"the built-in catalog {resource} cannot be read: {exc}") from exc
        label = labels[resource]
    elif source == FILE_SOURCE:
        if not catalog_file:
            raise CatalogRefused("no catalog file is set (catalog_file in the settings)")
        suite = read_file(Path(catalog_file).expanduser())
        label = f"Catalog file: {Path(catalog_file).name}"
    else:
        raise CatalogRefused(f"unknown catalog {source!r}")
    check(suite)
    return Catalog(source, label, suite, canonical_sha256(suite))


def read_file(path: Path) -> PromptSuite:
    """A prompt-suite JSON file. Raises: ``CatalogRefused``."""
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_CATALOG_BYTES + 1)
    except OSError as exc:
        raise CatalogRefused(f"the catalog file cannot be read: {exc}") from exc
    if len(data) > MAX_CATALOG_BYTES:
        raise CatalogRefused(f"the catalog file is larger than {MAX_CATALOG_BYTES} bytes")
    try:
        return PromptSuite.model_validate_json(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in error['loc']) or 'suite'}: {error['msg']}"
            for error in exc.errors()[:3]
        )
        raise CatalogRefused(f"the catalog file is not a valid prompt suite ({problems})") from exc


def batch_ids(suite: PromptSuite, selected: Iterable[str]) -> tuple[str, ...]:
    """The batch's prompt ids in catalog order: ``selected`` plus every room tone.

    Raises: ``CatalogRefused`` for an unknown id or nothing spoken selected."""
    chosen = set(selected)
    known = {prompt.prompt_id for prompt in suite.prompts}
    unknown = sorted(chosen - known)
    if unknown:
        raise CatalogRefused(f"not in the catalog: {', '.join(unknown)}")
    if not any(p.prompt_id in chosen and p.category != "room-tone" for p in suite.prompts):
        raise CatalogRefused("choose at least one prompt to record")
    return tuple(
        prompt.prompt_id
        for prompt in suite.prompts
        if prompt.prompt_id in chosen or prompt.category == "room-tone"
    )


def batch_suite(suite: PromptSuite, prompt_ids: Iterable[str]) -> PromptSuite:
    """The suite a session's package names: ``suite`` itself when the batch is all of it,
    else the subset, named by its prompts' digest."""
    wanted = set(prompt_ids)
    prompts = tuple(prompt for prompt in suite.prompts if prompt.prompt_id in wanted)
    if prompts == suite.prompts:
        return suite
    digest = canonical_sha256([prompt.model_dump(mode="json") for prompt in prompts])
    suite_id = (
        f"desktop-subset-{digest[:16]}"
        if suite.schema_version == PROMPT_SUITE_V1
        else f"native-subset-{digest[:24]}"
    )
    return PromptSuite(
        schema_version=suite.schema_version,
        suite_id=suite_id,
        purpose=suite.purpose,
        prompts=prompts,
    )
