"""Which library files a vault copy carries, as which class, and which stay on the Mac.

@canonical library_classification -- CLASSIFICATION_RULES is the one table that decides, for
every path under an asset root, whether a replication pass copies it (as which FileClass, how
and with which restore mode) or leaves it on the Mac; classify() is the only lookup, and a path
no rule covers is a STORAGE.PROTOCOL refusal, so a new writer's files must be classified here
before any pass can run.
Related: library_replication.py (the pass and the owner sources that read what this table
copies), storage_contracts.py (FileClass), tests/test_replication_sources.py (every fixture
path is classified exactly once; every rule is reachable).

The table is data: an ordered tuple of ``ClassificationRule`` records, each with a path
pattern, a disposition (a ``FileClass`` or ``"mac-only"``), the owner that writes those files,
the reason, and an example path the rule must classify. The first rule that matches a path
decides. Two rules match one path only where the earlier rule says so in ``wins_over`` (the
names of the later rules it shadows, or ``EVERY_LATER_RULE``); the import-time check and the
tests hold the table to that. Rules come in four bands: files that never leave the Mac wherever
they appear (they win over every later rule), directories that never leave the Mac (a pass
does not descend into them, which is safe because every such rule precedes every copied rule),
single Mac-only files among copied ones, and the copied files.

Pattern syntax (always matched against the whole path): literal text; ``*`` (any characters
within one path segment); ``**/`` (any number of leading directories, including none);
``/**`` (one or more segments below); ``{/**}`` (optionally, anything below: the entry itself
or its subtree); ``{a|b}`` (one of the literal alternatives); and the named parts
``{sha256}``, ``{hex16}``, ``{hex32}``, ``{group}`` (a 32- or 64-hex group token), ``{id}``
(a stable id) and ``{segment}`` (any one segment).

This module imports only the standard library and the package's ``errors``, ``paths``,
``storage_contracts`` and ``storage_ports`` modules.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, Literal, NamedTuple, get_args

from .paths import validate_relative_path
from .storage_contracts import FileClass
from .storage_ports import storage_error

__all__ = [
    "CLASSIFICATION_RULES",
    "CORPUS_DATABASE_PATH",
    "EVERY_LATER_RULE",
    "FILE_CLASSES",
    "MAC_ONLY",
    "Classification",
    "ClassificationRule",
    "Disposition",
    "RestoreMode",
    "Transfer",
    "classify",
    "matching_rules",
    "rule_named",
    "subtree_stays_on_mac",
]

MAC_ONLY: Final = "mac-only"
# A ``wins_over`` entry meaning: this rule shadows every later rule.
EVERY_LATER_RULE: Final = "*"
# The prompt database's own path; a pass carries it as one entry made by SQLite's online
# backup, never by reading the file (storage_binding.catalog_store_location names the same).
CORPUS_DATABASE_PATH: Final = "provenance/corpus/corpus.sqlite3"
FILE_CLASSES: Final[frozenset[str]] = frozenset(get_args(FileClass.__value__))

type Disposition = FileClass | Literal["mac-only"]
# How a copied file travels: its bytes (``file``), only its pin (``pin``: the manifest records
# the digest a record elsewhere pins, and nothing is uploaded) or an online SQLite backup of it
# (``sqlite-backup``).
type Transfer = Literal["file", "pin", "sqlite-backup"]
# The mode restore gives a copied file (its directories get 0500 or 0700 to match).
type RestoreMode = Literal[0o400, 0o600]

_NAMED_PARTS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "sha256": "[0-9a-f]{64}",
        "hex16": "[0-9a-f]{16}",
        "hex32": "[0-9a-f]{32}",
        "group": "[0-9a-f]{32}(?:[0-9a-f]{32})?",
        "id": "[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?",
        "segment": "[^/]+",
    }
)
_TOKENS: Final = re.compile(r"(\{/\*\*\}|\*\*/|/\*\*|\{[^{}]*\}|\*)")
_SUBTREE_SUFFIXES: Final = ("{/**}", "/**")
_RULE_NAME: Final = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def _pattern_regex(pattern: str) -> str:
    """The regular expression of a rule pattern (see the module docstring).

    Raises: ``ValueError`` for an unknown ``{name}`` or a stray brace.
    """
    fragments: list[str] = []
    for token in _TOKENS.split(pattern):
        if token == "{/**}":
            fragments.append("(?:/[^/]+)*")
        elif token == "**/":
            fragments.append("(?:[^/]+/)*")
        elif token == "/**":
            fragments.append("(?:/[^/]+)+")
        elif token == "*":
            fragments.append("[^/]*")
        elif token.startswith("{") and token.endswith("}"):
            body = token[1:-1]
            if body in _NAMED_PARTS:
                fragments.append(f"(?:{_NAMED_PARTS[body]})")
            elif "|" in body:
                fragments.append(f"(?:{'|'.join(re.escape(part) for part in body.split('|'))})")
            else:
                raise ValueError(f"unknown pattern part {token} in {pattern!r}")
        elif "{" in token or "}" in token:
            raise ValueError(f"unbalanced brace in pattern {pattern!r}")
        else:
            fragments.append(re.escape(token))
    return "".join(fragments)


@dataclass(frozen=True, slots=True)
class ClassificationRule:
    """One row of the table: which paths it covers and what a pass does with them.

    ``pattern`` covers whole paths relative to the asset root. ``disposition`` is the
    ``FileClass`` a copied file travels as, or ``"mac-only"``. ``owner`` names the module (or
    program) that writes the files, ``reason`` why they are copied or stay, and ``example`` a
    path this rule must classify. A copied rule also says how the file travels
    (``transfer``) and the mode restore gives it (``mode``); a Mac-only rule keeps the
    defaults. ``wins_over`` names the later rules this one intentionally shadows.
    Raises (on construction): ``ValueError`` for a malformed pattern.
    """

    name: str
    pattern: str
    disposition: Disposition
    owner: str
    reason: str
    example: str
    transfer: Transfer = "file"
    mode: RestoreMode = 0o600
    wins_over: tuple[str, ...] = ()
    _regex: re.Pattern[str] = field(init=False, repr=False, compare=False)
    _subtree: re.Pattern[str] | None = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_regex", re.compile(_pattern_regex(self.pattern)))
        subtree = None
        if self.disposition == MAC_ONLY:
            for suffix in _SUBTREE_SUFFIXES:
                if self.pattern.endswith(suffix):
                    prefix = self.pattern.removesuffix(suffix)
                    # The covered directory and every directory below it.
                    subtree = re.compile(f"{_pattern_regex(prefix)}(?:/[^/]+)*")
        object.__setattr__(self, "_subtree", subtree)

    @property
    def copied(self) -> bool:
        """Whether a pass copies the files this rule covers."""
        return self.disposition != MAC_ONLY

    @property
    def subtree(self) -> bool:
        """Whether this Mac-only rule covers whole directories a pass need not enter."""
        return self._subtree is not None

    def matches(self, path: str) -> bool:
        """Whether ``path`` (relative to the asset root) is covered by this rule's pattern."""
        return self._regex.fullmatch(path) is not None

    def covers_directory(self, directory: str) -> bool:
        """Whether this Mac-only subtree rule covers ``directory`` and everything below it."""
        return self._subtree is not None and self._subtree.fullmatch(directory) is not None


class Classification(NamedTuple):
    """What the table decided for one path: the path and the first rule that covers it."""

    path: str
    rule: ClassificationRule

    @property
    def disposition(self) -> Disposition:
        return self.rule.disposition

    @property
    def copied(self) -> bool:
        return self.rule.copied


def _rule(
    name: str,
    pattern: str,
    disposition: Disposition,
    *,
    owner: str,
    reason: str,
    example: str,
    transfer: Transfer = "file",
    mode: RestoreMode = 0o600,
    wins_over: tuple[str, ...] = (),
) -> ClassificationRule:
    return ClassificationRule(
        name=name,
        pattern=pattern,
        disposition=disposition,
        owner=owner,
        reason=reason,
        example=example,
        transfer=transfer,
        mode=mode,
        wins_over=wins_over,
    )


_TAKE = "provenance/recording-takes"
_NATIVE_EDITS = "provenance/recording-edits/native/take-{sha256}"
_EXAMPLE_SHA = "0123456789abcdef" * 4
_EXAMPLE_HEX16 = "0123456789abcdef"
_EXAMPLE_HEX32 = _EXAMPLE_HEX16 * 2
_EXAMPLE_TAKE = f"take-{_EXAMPLE_SHA}"
_EVERY = (EVERY_LATER_RULE,)

_RULES: Final = (
    # -- Never copied, wherever they appear; each wins over every later rule. --------------
    _rule(
        "finder-metadata",
        "**/.DS_Store",
        MAC_ONLY,
        owner="macOS Finder",
        reason="Finder's folder metadata is not library evidence",
        example="raw/.DS_Store",
        wins_over=_EVERY,
    ),
    _rule(
        "interrupted-atomic-write",
        "**/.*.tmp-{hex16}",
        MAC_ONLY,
        owner="canonical (write_canonical_json, write_new_private_file) and corpus_store_sqlite",
        reason="the temporary of an interrupted atomic or write-once publish; the published "
        "file is what a pass copies",
        example=f"provenance/consents/.cole-self-v1.json.tmp-{_EXAMPLE_HEX16}",
        wins_over=_EVERY,
    ),
    _rule(
        "writer-lock",
        "**/.*.lock",
        MAC_ONLY,
        owner="every library writer",
        reason="a writer's flock file; a pass never takes, copies or restores one",
        example=f"{_TAKE}/.metadata.lock",
        wins_over=_EVERY,
    ),
    _rule(
        "writer-lease",
        "**/.*.lease",
        MAC_ONLY,
        owner="corpus_store_sqlite and recording_index",
        reason="a store's lifetime lease file; a pass never takes, copies or restores one",
        example="provenance/corpus/.corpus.lease",
        wins_over=_EVERY,
    ),
    # -- Directories that never leave the Mac; a pass does not enter them. -----------------
    _rule(
        "vault-records",
        "provenance/vault{/**}",
        MAC_ONLY,
        owner="storage_binding and library_replication",
        reason="the library identity and, per destination, the binding, approvals, replication "
        "state, ledger, manifest copies and request log describe this host's link to its "
        "vaults; they are never copied or restored",
        example="provenance/vault/destinations/laptop/ledger/00000001.json",
    ),
    _rule(
        "replication-scratch",
        "tmp/.vault-*{/**}",
        MAC_ONLY,
        owner="library_replication",
        reason="a pass's private scratch (the prompt database backup), removed once consumed",
        example=f"tmp/.vault-{_EXAMPLE_HEX16}/database-backup.sqlite3",
    ),
    _rule(
        "recording-index",
        "provenance/recording-library{/**}",
        MAC_ONLY,
        owner="recording_index",
        reason="the recording index projection and its SQLite files rebuild themselves from the "
        "copied records",
        example="provenance/recording-library/recording-library.sqlite3-wal",
    ),
    _rule(
        "recording-index-quarantine",
        "provenance/recording-library-quarantine-*{/**}",
        MAC_ONLY,
        owner="recording_index",
        reason="a broken index cache moved aside; the index rebuilds itself",
        example=f"provenance/recording-library-quarantine-{_EXAMPLE_HEX32}/"
        "recording-library.sqlite3",
    ),
    _rule(
        "discovery-checkpoints",
        "provenance/recording-library-discovery{/**}",
        MAC_ONLY,
        owner="recording_discovery",
        reason="discovery checkpoints bind this Mac's index instance and file identities, so "
        "they are invalid after a restore",
        example=f"provenance/recording-library-discovery/{_EXAMPLE_SHA}.result.json",
    ),
    _rule(
        "prepared-gates",
        "provenance/recording-groups/.prepared-gates{/**}",
        MAC_ONLY,
        owner="recording_takes",
        reason="a prepared capture gate binds file inodes; a restored group prepares its gate "
        "again",
        example=f"provenance/recording-groups/.prepared-gates/{_EXAMPLE_SHA}.json",
    ),
    _rule(
        "waveform-cache",
        "derived/recording-waveforms{/**}",
        MAC_ONLY,
        owner="the app's waveform archive (RecordingWaveformArchiveStore.swift)",
        reason="waveform caches rebuild on demand",
        example=f"derived/recording-waveforms/{_EXAMPLE_SHA}-v2.cvlwave",
    ),
    _rule(
        "speech-clips",
        "derived/speech{/**}",
        MAC_ONLY,
        owner="finetune (Speak)",
        reason="Speak clips stay on the Mac (Decision 4, D-019)",
        example="derived/speech/speak-1/speech.json",
    ),
    _rule(
        "fine-tune-synthesis-scratch",
        "derived/finetune/{id}/samples/{segment}/synthesis-tmp{/**}",
        MAC_ONLY,
        owner="finetune",
        reason=(
            "a running or interrupted synthesizer's work directory, with a copy of its "
            "reference clip"
        ),
        example="derived/finetune/run-1/samples/finetuned/synthesis-tmp/reference.wav",
        wins_over=("fine-tune-run",),
    ),
    _rule(
        "incoming-sessions",
        "incoming{/**}",
        MAC_ONLY,
        owner="sync and intake",
        reason="capture sessions not yet accepted; intake publishes them under raw/",
        example="incoming/pilot-001/READY",
    ),
    _rule(
        "intake-outcomes",
        "provenance/intake{/**}",
        MAC_ONLY,
        owner="intake_publisher",
        reason="the home host's record of each accepted or refused package; the receipt it "
        "names travels with the package under raw/, and a refusal is operational",
        example="provenance/intake/sessions/pilot-001.json",
    ),
    _rule(
        "home-host-records",
        "provenance/home-host{/**}",
        MAC_ONLY,
        owner="home_host",
        reason="a capturing host's standing home-host approval, binding, sign-in settings and "
        "upload ledger describe its link to the home host; like provenance/vault, they are "
        "never copied or restored",
        example="provenance/home-host/ledger/00000001.json",
    ),
    _rule(
        "runtimes",
        "runtimes{/**}",
        MAC_ONLY,
        owner="runtime",
        reason="installed runtimes, their locks and eviction tombstones are rebuilt from locks",
        example="runtimes/worker.lock",
    ),
    _rule(
        "models",
        "models{/**}",
        MAC_ONLY,
        owner="runtime",
        reason="model snapshots are downloaded again from their pins",
        example="models/f5-tts/model.safetensors",
    ),
    _rule(
        "experiments",
        "experiments{/**}",
        MAC_ONLY,
        owner="audition and promotion",
        reason="experiment outputs are not part of the library copy",
        example="experiments/run-1/result.json",
    ),
    _rule(
        "evaluations",
        "evaluations{/**}",
        MAC_ONLY,
        owner="blind",
        reason="evaluation outputs are not part of the library copy",
        example="evaluations/review-1/answers.json",
    ),
    _rule(
        "anonymous-temporaries",
        "tmp/tmp*{/**}",
        MAC_ONLY,
        owner="tempfile users (recording_discovery, recording_index, recording_index_backfill)",
        reason="an anonymous temporary file that exists only for a moment",
        example="tmp/tmpab12cd34",
    ),
    _rule(
        "intake-staging",
        "tmp/.intake-*{/**}",
        MAC_ONLY,
        owner="intake",
        reason="a raw session being accepted",
        example="tmp/.intake-pilot-001-123/manifest.json",
    ),
    _rule(
        "reference-staging",
        "tmp/.reference-*{/**}",
        MAC_ONLY,
        owner="references",
        reason="a reference being published",
        example=f"tmp/.reference-neutral-{_EXAMPLE_HEX16}/reference.wav",
    ),
    _rule(
        "dataset-staging",
        "tmp/.dataset-*{/**}",
        MAC_ONLY,
        owner="datasets",
        reason="a dataset manifest being published",
        example=f"tmp/.dataset-pilot-{_EXAMPLE_HEX16}/manifest.json",
    ),
    _rule(
        "transfer-plan-staging",
        "tmp/.transfer-plan-*{/**}",
        MAC_ONLY,
        owner="datasets",
        reason="a transfer plan being published",
        example=f"tmp/.transfer-plan-plan-1-{_EXAMPLE_HEX16}/manifest.json",
    ),
    _rule(
        "blind-review-staging",
        "tmp/blind-*{/**}",
        MAC_ONLY,
        owner="blind",
        reason="a blind review being staged",
        example=f"tmp/blind-review-1-{_EXAMPLE_HEX16}/clip-01.wav",
    ),
    _rule(
        "package-staging",
        "tmp/.native-package-stage-*{/**}",
        MAC_ONLY,
        owner="recording_packages",
        reason="a native package being prepared",
        example="tmp/.native-package-stage-native-1/audio/000-room-tone.wav",
    ),
    # -- Single Mac-only files among copied ones. ------------------------------------------
    _rule(
        "take-commit-journal",
        f"{_TAKE}/.pending.json",
        MAC_ONLY,
        owner="recording_takes",
        reason="an uncommitted operation's journal; a restored store has nothing to repair",
        example=f"{_TAKE}/.pending.json",
    ),
    _rule(
        "native-capture-partial",
        "tmp/recording-takes/take-{sha256}/capture.partial.wav",
        MAC_ONLY,
        owner="the app's recorder (NativeCaptureWriter.swift)",
        reason="audio still being recorded, never sealed",
        example=f"tmp/recording-takes/{_EXAMPLE_TAKE}/capture.partial.wav",
    ),
    _rule(
        "native-capture-recovery",
        "tmp/recording-takes/take-{sha256}/native-recovery.json",
        MAC_ONLY,
        owner="the app's recorder (NativeCaptureWriter.swift)",
        reason="the recorder's crash-recovery note",
        example=f"tmp/recording-takes/{_EXAMPLE_TAKE}/native-recovery.json",
    ),
    _rule(
        "native-capture-recovery-temporary",
        "tmp/recording-takes/take-{sha256}/.native-recovery-*.tmp",
        MAC_ONLY,
        owner="the app's recorder (NativeCaptureWriter.swift)",
        reason="the temporary of a crash-recovery note being written",
        example=f"tmp/recording-takes/{_EXAMPLE_TAKE}/.native-recovery-{_EXAMPLE_HEX32}.tmp",
    ),
    _rule(
        "corpus-database-journals",
        "provenance/corpus/corpus.sqlite3-{wal|shm|journal}",
        MAC_ONLY,
        owner="corpus_store_sqlite",
        reason="SQLite's journal files; the database travels only as its online backup",
        example="provenance/corpus/corpus.sqlite3-wal",
    ),
    _rule(
        "fine-tune-activity",
        "provenance/finetune-jobs/{head|speech-active}.json",
        MAC_ONLY,
        owner="recording_finetune_jobs",
        reason="the active-job pointer and the live Speak marker describe work running on this Mac",
        example="provenance/finetune-jobs/speech-active.json",
    ),
    _rule(
        "package-copy-partial",
        "provenance/recording-packages/work/{id}/copy-*.partial",
        MAC_ONLY,
        owner="recording_packages",
        reason="an original being copied into a package",
        example=f"provenance/recording-packages/work/prepare-1/copy-{_EXAMPLE_TAKE}-ab12.partial",
        wins_over=("package-records",),
    ),
    _rule(
        "edit-render-partial",
        "derived/recording-edits/{native/|}{sha256}/.trim-{hex16}.wav",
        MAC_ONLY,
        owner="recording_edits",
        reason="a trim being rendered",
        example=f"derived/recording-edits/native/{_EXAMPLE_SHA}/.trim-{_EXAMPLE_HEX16}.wav",
    ),
    # -- Copied: records. ------------------------------------------------------------------
    _rule(
        "take-records",
        f"{_TAKE}/take-{{sha256}}/{{head|reservation|seal}}.json",
        "records",
        owner="recording_takes",
        reason="a native take's head, reservation and seal",
        example=f"{_TAKE}/{_EXAMPLE_TAKE}/head.json",
    ),
    _rule(
        "take-operations",
        f"{_TAKE}/.operations/{{id}}.json",
        "records",
        owner="recording_takes",
        reason="the take store's immutable operation journal",
        example=f"{_TAKE}/.operations/save-1.json",
    ),
    _rule(
        "take-cursors",
        f"{_TAKE}/.cursors/{{id}}.json",
        "records",
        owner="recording_takes",
        reason="each subject's workspace cursor",
        example=f"{_TAKE}/.cursors/cole-self.json",
    ),
    _rule(
        "take-analysis-heads",
        f"{_TAKE}/.analysis/{{sha256}}.json",
        "records",
        owner="recording_takes",
        reason="the newest published analysis of each job",
        example=f"{_TAKE}/.analysis/{_EXAMPLE_SHA}.json",
    ),
    _rule(
        "take-store-head",
        f"{_TAKE}/.store-head.json",
        "records",
        owner="recording_takes",
        reason="the take store's committed revision",
        example=f"{_TAKE}/.store-head.json",
    ),
    _rule(
        "native-edit-head",
        f"{_NATIVE_EDITS}/head.json",
        "records",
        owner="recording_edits (under the take store's commit)",
        reason="a native take's current frame recipe",
        example=f"provenance/recording-edits/native/{_EXAMPLE_TAKE}/head.json",
    ),
    _rule(
        "native-edit-recipes",
        f"{_NATIVE_EDITS}/{{sha256}}.json",
        "records",
        owner="recording_edits (under the take store's commit)",
        reason="a native take's immutable frame recipes",
        example=f"provenance/recording-edits/native/{_EXAMPLE_TAKE}/{_EXAMPLE_SHA}.json",
    ),
    _rule(
        "native-edit-render-records",
        f"{_NATIVE_EDITS}/{{sha256}}.{{derivative|failure}}.json",
        "records",
        owner="recording_edits and recording_edit_jobs",
        reason="what rendering a native recipe produced or why it failed",
        example=f"provenance/recording-edits/native/{_EXAMPLE_TAKE}/{_EXAMPLE_SHA}.derivative.json",
    ),
    _rule(
        "recording-groups",
        "provenance/recording-groups/group-{group}.json",
        "records",
        owner="recording_takes",
        reason="recording groups, which bind the catalog every take was recorded against",
        example=f"provenance/recording-groups/group-{_EXAMPLE_HEX32}.json",
    ),
    _rule(
        "desktop-edits",
        "provenance/recording-edits/{sha256}.json",
        "records",
        owner="recording_edits",
        reason="legacy desktop trims and removals",
        example=f"provenance/recording-edits/{_EXAMPLE_SHA}.json",
    ),
    _rule(
        "legacy-edit-requests",
        "provenance/recording-edits/legacy/.operations/{id}.json",
        "records",
        owner="recording_edits",
        reason="immutable legacy edit intents",
        example="provenance/recording-edits/legacy/.operations/edit-1.json",
    ),
    _rule(
        "legacy-edit-heads",
        "provenance/recording-edits/legacy/{sha256}/head.json",
        "records",
        owner="recording_edits",
        reason="each legacy recording's current edit intent",
        example=f"provenance/recording-edits/legacy/{_EXAMPLE_SHA}/head.json",
    ),
    _rule(
        "legacy-edit-results",
        "provenance/recording-edits/legacy/{sha256}/{id}.result.json",
        "records",
        owner="recording_edits and recording_edit_jobs",
        reason="what materializing a legacy edit intent produced",
        example=f"provenance/recording-edits/legacy/{_EXAMPLE_SHA}/edit-1.result.json",
    ),
    _rule(
        "analysis-jobs",
        "provenance/recording-analysis/jobs/{sha256}.json",
        "records",
        owner="recording_analysis",
        reason="the analysis queue's job records",
        example=f"provenance/recording-analysis/jobs/{_EXAMPLE_SHA}.json",
    ),
    _rule(
        "package-origin",
        "provenance/recording-package-origin.json",
        "records",
        owner="recording_packages",
        reason="the root id every locally prepared package session names; without it no "
        "package can be read",
        example="provenance/recording-package-origin.json",
    ),
    _rule(
        "package-records",
        "provenance/recording-packages/**",
        "records",
        owner="recording_packages",
        reason="package plans, commits, sessions, finalization records and the removed job "
        "queue's residue",
        example="provenance/recording-packages/sessions/native-1.json",
    ),
    _rule(
        "desktop-sessions",
        "provenance/capture-desktop/{id}.json",
        "records",
        owner="recording_library",
        reason="legacy desktop session records, which name each take's audio digest",
        example=f"provenance/capture-desktop/desktop-{_EXAMPLE_HEX32}.json",
    ),
    _rule(
        "capture-continuity",
        "provenance/capture-continuity/**",
        "records",
        owner="capture_native",
        reason="native input continuity evidence of legacy takes",
        example=f"provenance/capture-continuity/desktop-1/{_EXAMPLE_HEX32}.json",
    ),
    _rule(
        "legacy-continuations",
        "provenance/recording-legacy-continuations/{id}.json",
        "records",
        owner="recording_recovery",
        reason="the intents that continued unfinished legacy sessions natively",
        example="provenance/recording-legacy-continuations/continue-1.json",
    ),
    _rule(
        "consent-history",
        "provenance/consents/history/{id}.{sha256}.json",
        "records",
        owner="consent",
        reason="earlier consent versions, each named by its own SHA-256 and written in place "
        "(created, then filled), so a pass binds each to the digest in its name",
        example=f"provenance/consents/history/cole-self-v1.{_EXAMPLE_SHA}.json",
        wins_over=("consents",),
    ),
    _rule(
        "consents",
        "provenance/consents/**",
        "records",
        owner="consent",
        reason="consent records (and any other file their directory holds)",
        example="provenance/consents/cole-self-v1.json",
    ),
    _rule(
        "diagnostic-root",
        "provenance/diagnostic-root.json",
        "records",
        owner="diagnostic_roots",
        reason="the marker that makes a root a physical-test root",
        example="provenance/diagnostic-root.json",
        mode=0o400,
    ),
    _rule(
        "pilot-gates",
        "provenance/pilot-gates/**",
        "records",
        owner="cli (dataset pilot-gate)",
        reason="pilot gate approvals",
        example="provenance/pilot-gates/gate-1.json",
        mode=0o400,
    ),
    _rule(
        "headphone-reviews",
        "provenance/headphone-reviews/**",
        "records",
        owner="cli (reference headphone-review)",
        reason="headphone reviews that references bind",
        example="provenance/headphone-reviews/review-1.json",
    ),
    _rule(
        "transfer-approvals",
        "provenance/transfer-approvals/**",
        "records",
        owner="the operator (datasets reads them)",
        reason="hosted-transfer approvals that transfer plans bind",
        example="provenance/transfer-approvals/approval-1.json",
    ),
    _rule(
        "fine-tune-job-records",
        "provenance/finetune-jobs/{requests|terminal}/{id}.json",
        "records",
        owner="recording_finetune_jobs",
        reason="immutable fine-tune job requests and outcomes",
        example="provenance/finetune-jobs/terminal/finetune-1.json",
    ),
    _rule(
        "transfer-plans",
        "derived/transfer-plans/**",
        "records",
        owner="datasets",
        reason="write-once transfer plans",
        example="derived/transfer-plans/plan-1/manifest.json",
        mode=0o400,
    ),
    # -- Copied: the prompt database. ------------------------------------------------------
    _rule(
        "corpus-database",
        CORPUS_DATABASE_PATH,
        "prompt-database",
        owner="corpus_store_sqlite",
        reason="the prompt database, carried as one online backup and never read as a file",
        example=CORPUS_DATABASE_PATH,
        transfer="sqlite-backup",
    ),
    _rule(
        "corpus-snapshots",
        "provenance/corpus/snapshots/{sha256}.json",
        "prompt-database",
        owner="corpus_store_sqlite",
        reason="published catalog snapshots, readable without the database",
        example=f"provenance/corpus/snapshots/{_EXAMPLE_SHA}.json",
    ),
    # -- Copied: recordings. ---------------------------------------------------------------
    _rule(
        "native-originals",
        "tmp/recording-takes/take-{sha256}/original.wav",
        "recordings",
        owner="the app's recorder (NativeCaptureWriter.swift) and recording_takes",
        reason="a native take's original, carried once its take's seal names these bytes",
        example=f"tmp/recording-takes/{_EXAMPLE_TAKE}/original.wav",
    ),
    _rule(
        "rejected-takes",
        "tmp/capture-{id}.rejected/**",
        "recordings",
        owner="capture",
        reason="rejected legacy takes and their log, carried as the log names them (restored "
        "at exactly 0600)",
        example="tmp/capture-desktop-1.rejected/rejected-takes.json",
        wins_over=("capture-sessions",),
    ),
    _rule(
        "capture-sessions",
        "tmp/capture-{id}/**",
        "recordings",
        owner="capture, recording_library and recording_packages",
        reason="legacy and package capture sessions: everything once READY; before READY, the "
        "audio a desktop session record names and interrupted partials",
        example="tmp/capture-desktop-1/audio/001-neutral-01.wav",
    ),
    _rule(
        "raw-packages",
        "raw/**",
        "recordings",
        owner="intake",
        reason="accepted raw capture sessions",
        example=f"raw/{_EXAMPLE_SHA}/audio/001-neutral-01.wav",
        mode=0o400,
    ),
    _rule(
        "references",
        "references/**",
        "recordings",
        owner="references",
        reason="published zero-shot references",
        example="references/cole-neutral-v1/reference.wav",
        mode=0o400,
    ),
    # -- Copied: derived edits. ------------------------------------------------------------
    _rule(
        "desktop-edit-renders",
        "derived/recording-edits/{sha256}/{sha256}.wav",
        "derived-edits",
        owner="recording_edits",
        reason="trimmed legacy recordings, named by their digest",
        example=f"derived/recording-edits/{_EXAMPLE_SHA}/{_EXAMPLE_SHA}.wav",
    ),
    _rule(
        "native-edit-renders",
        "derived/recording-edits/native/{sha256}/{sha256}.wav",
        "derived-edits",
        owner="recording_edits",
        reason="rendered native recipes, named by their digest",
        example=f"derived/recording-edits/native/{_EXAMPLE_SHA}/{_EXAMPLE_SHA}.wav",
    ),
    # -- Copied: training material (the opt-in class). -------------------------------------
    _rule(
        "datasets",
        "derived/datasets/**",
        "training",
        owner="datasets",
        reason="write-once dataset manifests",
        example="derived/datasets/pilot-1/manifest.json",
        mode=0o400,
    ),
    _rule(
        "fine-tune-pretrained-link",
        "derived/finetune/{id}/checkpoints/pretrained_*.safetensors",
        "training",
        owner="finetune",
        reason="the hard link to the runtime's pinned base weights, recorded by its pin and "
        "never uploaded",
        example="derived/finetune/run-1/checkpoints/pretrained_model_1250000.safetensors",
        transfer="pin",
        wins_over=("fine-tune-run",),
    ),
    _rule(
        "fine-tune-archive-records",
        "derived/finetune/{id}/{archived|local-removal}.json",
        "training",
        owner="finetune (run archive)",
        reason="a run's archive record and its local-removal record",
        example="derived/finetune/run-1/archived.json",
        wins_over=("fine-tune-run",),
    ),
    _rule(
        "fine-tune-removal-history",
        "derived/finetune/{id}/local-removal-restored-*.json",
        "training",
        owner="finetune (run archive)",
        reason="the removal records of runs restored since",
        example="derived/finetune/run-1/local-removal-restored-1.json",
        wins_over=("fine-tune-run",),
    ),
    _rule(
        "fine-tune-run",
        "derived/finetune/{id}/**",
        "training",
        owner="finetune",
        reason="a finished fine-tune run, carried only while no fine-tune job or Speak request "
        "is active",
        example="derived/finetune/run-1/checkpoints/model_last.pt",
    ),
)


def _checked(rules: tuple[ClassificationRule, ...]) -> tuple[ClassificationRule, ...]:
    """The table, once it holds its own rules (run at import).

    Raises: ``ValueError`` when two rules share a name, a name is malformed, a copied rule's
    class is unknown, a Mac-only rule sets a transfer or mode, a ``wins_over`` entry names no
    later rule, a Mac-only subtree rule follows a copied rule (a pass could then skip a
    copied file), an example is not a canonical relative path, or an example is not
    classified by its own rule.
    """
    names = [rule.name for rule in rules]
    if len(set(names)) != len(names):
        raise ValueError("classification rule names must be unique")
    first_copied = next(index for index, rule in enumerate(rules) if rule.copied)
    for index, rule in enumerate(rules):
        later = set(names[index + 1 :])
        if _RULE_NAME.fullmatch(rule.name) is None:
            raise ValueError(f"malformed classification rule name {rule.name!r}")
        if rule.copied and rule.disposition not in FILE_CLASSES:
            raise ValueError(f"rule {rule.name} copies an unknown class {rule.disposition!r}")
        if not rule.copied and (rule.transfer != "file" or rule.mode != 0o600):
            raise ValueError(f"Mac-only rule {rule.name} cannot set a transfer or restore mode")
        if rule.wins_over != _EVERY and not set(rule.wins_over) <= later:
            raise ValueError(f"rule {rule.name} can only win over later rules")
        if rule.subtree and index > first_copied:
            raise ValueError(f"Mac-only subtree rule {rule.name} must precede every copied rule")
        validate_relative_path(rule.example)
        decided = next((candidate for candidate in rules if candidate.matches(rule.example)), None)
        if decided is not rule:
            raise ValueError(f"rule {rule.name} does not classify its own example {rule.example}")
    return rules


CLASSIFICATION_RULES: Final[tuple[ClassificationRule, ...]] = _checked(_RULES)
_RULES_BY_NAME: Final[Mapping[str, ClassificationRule]] = MappingProxyType(
    {rule.name: rule for rule in CLASSIFICATION_RULES}
)
_SUBTREE_RULES: Final = tuple(rule for rule in CLASSIFICATION_RULES if rule.subtree)


def rule_named(name: str) -> ClassificationRule:
    """The rule called ``name``.

    Raises: ``KeyError`` when no rule has that name.
    """
    return _RULES_BY_NAME[name]


def matching_rules(path: str) -> tuple[ClassificationRule, ...]:
    """Every rule whose pattern covers ``path``, in table order (the first one decides).

    Params: ``path``, relative to the asset root (not validated here).
    Returns: the matching rules; empty when the path is unclassified.
    Raises: nothing.
    """
    return tuple(rule for rule in CLASSIFICATION_RULES if rule.matches(path))


def classify(path: str) -> Classification:
    """Decide what a pass does with one path under the asset root.

    Params: ``path``, a canonical relative POSIX path (for example ``raw/<sha>/READY``).
    Returns: the path with the first rule that covers it.
    Raises: ``StorageError`` (``STORAGE.PROTOCOL``) naming the path when it is not a
    canonical relative path or no rule covers it; the pass is refused before anything is
    read, because an unclassified file could be evidence a restore needs.
    """
    try:
        validate_relative_path(path)
    except ValueError as error:
        raise storage_error(
            "STORAGE.PROTOCOL", f"the library holds a path that cannot be classified: {path!r}"
        ) from error
    for rule in CLASSIFICATION_RULES:
        if rule.matches(path):
            return Classification(path, rule)
    raise storage_error(
        "STORAGE.PROTOCOL",
        f"no classification rule covers {path}; classify the file (library_classification) "
        "before a replication pass can run",
    )


def subtree_stays_on_mac(directory: str) -> bool:
    """Whether every path below ``directory`` stays on the Mac, so a pass need not enter it.

    This holds because every Mac-only subtree rule precedes every copied rule in the table.
    Params: ``directory``, relative to the asset root.
    Returns: ``True`` when a Mac-only subtree rule covers the directory.
    Raises: nothing.
    """
    return any(rule.covers_directory(directory) for rule in _SUBTREE_RULES)
