"""Approve uploads to my PC: the standing home-host approval (D-021), made in the app.

D-021 asks for an explicit, standing approval naming the destination before anything is
uploaded. The app makes it in its own library (``Config.library_dir``), where its D-021
ledger lives, from one confirm dialog instead of the CLI's preview, digest and flags. No field
is typed:

- ``endpoint``: the configured server's host, lowercased;
- ``tailnet_suffix`` and ``node_stable_id``: from Tailscale, through the resolver every
  upload checks the node with (``TailscaleCliResolver``, ``identity_problem``). The peer is
  the one whose ``DNSName`` is the endpoint; the suffix is the endpoint less its first label,
  which the resolver refuses unless Tailscale reports it as this host's MagicDNS suffix; the
  stable ID is what ``whois`` says of that peer, which must carry ``tag:voice-lab``. Without
  Tailscale, without that peer, or with a peer that lacks the tag, nothing is approved and
  nothing is written;
- ``classes``: ``capture`` and ``measurement``; ``approved_at`` now and ``expires_at``
  ``APPROVAL_DAYS`` later; ``approved_by`` the sign-in's name, else ``APPROVER``;
  ``STATEMENT``; and a fresh ``approval_id`` that no approval in the library has.

``propose`` asks Tailscale and says what Approve would write: nothing when an approval stands
for this endpoint and node, covers both classes and does not expire within ``RENEW_WITHIN``.
Any other approval (none, expired, another endpoint or node, a missing class, one about to
expire) is replaced by a new one with a new id; the old file stays, as approvals are
write-once. ``confirm`` is the review: it writes exactly the approval the dialog showed, with
that approval's own digest, through ``cole_voice_lab.home_host.approve`` (0400, write-once,
bound). ``standing`` says, from the library alone, whether uploads wait for an approval.

The CLI (``voice-lab home-host approve``) stays the headless way. The Swift app's library
(``~/.local/share/cole-voice-lab``) is never read here: an approval and its ledger belong to
the same library, and that one serves only ``voice-lab corpus-import``.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

from cole_voice_lab.errors import ContractError, SecurityError
from cole_voice_lab.home_host import (
    HOME_HOST_TAG,
    HomeHostApproval,
    HomeHostClass,
    approve,
    load_approval,
    record_sha256,
)
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.storage_contracts import StorageNameError, validate_tailnet_endpoint
from cole_voice_lab.storage_ports import StorageError
from cole_voice_lab.tailnet import (
    ExpectedIdentity,
    TailnetResolver,
    TailscaleCliResolver,
    identity_problem,
)

from .measurement import home_host

#: How long an approval made here stands (D-021 allows at most 366 days).
APPROVAL_DAYS: Final = 365
#: An approval that expires within this is renewed by Approve (and offered for renewal).
RENEW_WITHIN: Final = timedelta(days=30)
#: What the app uploads: corpus and prompted takes (``capture``) and measurement sessions.
CLASSES: Final[tuple[HomeHostClass, ...]] = ("capture", "measurement")
STATEMENT: Final = "My recordings, to my own PC."
#: Who approves when the sign-in names no one.
APPROVER: Final = "Cole"
ID_PREFIX: Final = "pc-home-host"
#: The lead of every line that says uploads wait for an approval.
NEEDED: Final = "Uploads need your OK"

#: Makes the resolver for a tailnet suffix (tests: one over recorded Tailscale answers).
type ResolverFactory = Callable[[str], TailnetResolver]


def tailscale_resolver(tailnet_suffix: str) -> TailnetResolver:
    """The local Tailscale client, as every upload asks it."""
    return TailscaleCliResolver(tailnet_suffix=tailnet_suffix)


class ApprovalRefused(Exception):
    """Nothing was approved and nothing was written; the message says why and what to do."""


@dataclass(frozen=True)
class Destination:
    """The node Tailscale says answers for the configured server."""

    endpoint: str
    tailnet_suffix: str
    node_stable_id: str


@dataclass(frozen=True)
class Proposal:
    """What the confirm dialog shows: the destination, the approval Approve writes (``None``:
    ``standing`` covers it already, nothing is written) and why it replaces another (``""``
    when none stands)."""

    destination: Destination
    approval: HomeHostApproval | None
    standing: HomeHostApproval | None
    reason: str = ""


@dataclass(frozen=True)
class Approved:
    """What Approve did: ``approval`` stands now; ``written`` is ``False`` when it stood
    already and nothing was written."""

    approval: HomeHostApproval
    written: bool


@dataclass(frozen=True)
class Standing:
    """What this app's library holds for the configured server, read without Tailscale.

    ``needed``: uploads wait for an approval (none stands for this server covering both
    classes); ``expiring``: one stands but expires within ``RENEW_WITHIN``; ``text``: one
    plain line for the Status and Uploads screens."""

    needed: bool
    expiring: bool
    text: str
    approval: HomeHostApproval | None = None


def endpoint_of(server_url: str) -> str:
    """The configured server's host, lowercased: the endpoint an approval names.

    Raises: ``ApprovalRefused`` when no server is set or its host is not a name that can be
    on a tailnet (an IP address, ``localhost``)."""
    host = (urlsplit(server_url).hostname or "").lower()
    if not host:
        raise ApprovalRefused("Set the server on the Status screen first (Settings…).")
    try:
        validate_tailnet_endpoint(host)
        validate_tailnet_endpoint(host.partition(".")[2])
    except StorageNameError as exc:
        raise ApprovalRefused(
            f"The server {server_url} is not a name on the tailnet "
            "(https://voice-lab.<tailnet>.ts.net), so it cannot be approved."
        ) from exc
    return host


def find_destination(
    endpoint: str, resolver_factory: ResolverFactory = tailscale_resolver
) -> Destination:
    """Ask Tailscale which node answers for ``endpoint``, and require ``tag:voice-lab``.

    Raises: ``ApprovalRefused`` when Tailscale cannot be asked (not running, no answer), it
    knows no single node of that name on this host's tailnet, or the node is not the home
    host (no ``tag:voice-lab``, another name, no tailnet address)."""
    suffix = endpoint.partition(".")[2]
    try:
        peer = resolver_factory(suffix).resolve(endpoint)
    except StorageError as exc:
        if exc.code in ("STORAGE.UNAVAILABLE", "STORAGE.TIMEOUT"):
            raise ApprovalRefused(
                f"Tailscale is not running on this machine, or did not answer ({exc}). Start "
                "Tailscale, then approve again."
            ) from exc
        raise ApprovalRefused(
            f"Tailscale on this machine does not show {endpoint} as one node of its tailnet "
            f"({exc}). Check that Tailscale is connected and that the server in Settings is "
            "the PC."
        ) from exc
    try:
        expected = ExpectedIdentity(
            node_stable_id=peer.node_stable_id, tag=HOME_HOST_TAG, tailnet_suffix=suffix
        )
    except StorageNameError as exc:
        raise ApprovalRefused(f"Tailscale named no usable node for {endpoint} ({exc}).") from exc
    problem = identity_problem(endpoint, expected, peer)
    if problem is not None:
        raise ApprovalRefused(
            f"{endpoint} is not approved: {problem}. Only the PC's node, tagged "
            f"{HOME_HOST_TAG}, can receive uploads."
        )
    return Destination(endpoint, suffix, peer.node_stable_id)


def approver_name(claims: Mapping[str, Any] | None) -> str:
    """Who approves: the sign-in's ``name`` claim when it has one, else ``APPROVER``."""
    name = claims.get("name") if claims else None
    if isinstance(name, str) and name.strip():
        return name.strip()[:256]
    return APPROVER


def _home_host_dir(library_dir: Path) -> Path:
    """``provenance/home-host`` of the library (home_host.py: the approvals, the binding)."""
    return library_dir / "provenance" / "home-host"


def taken_ids(library_dir: Path) -> set[str]:
    """The ids of every approval the library holds (bound or not)."""
    approvals = _home_host_dir(library_dir) / "approvals"
    try:
        names = os.listdir(approvals)
    except FileNotFoundError:
        return set()
    return {name.removesuffix(".json") for name in names if name.endswith(".json")}


def _token() -> str:
    return secrets.token_hex(3)


def fresh_approval_id(
    taken: Collection[str], now: datetime, *, token: Callable[[], str] = _token
) -> str:
    """``pc-home-host-<yyyymmdd>-<6 hex>``, which no id in ``taken`` is."""
    while True:
        candidate = f"{ID_PREFIX}-{now:%Y%m%d}-{token()}"
        if candidate not in taken:
            return candidate


def _until(approval: HomeHostApproval) -> str:
    return approval.expires_at.date().isoformat()


def _current(library_dir: Path, *, now: datetime) -> tuple[HomeHostApproval | None, str]:
    """The approval bound in the library when it stands at ``now``, else ``None`` and why
    (``""``: nothing is bound). Reads only; nothing is created for a library that does not
    exist yet.

    Raises: ``SecurityError`` or ``OSError`` for a library that cannot be used."""
    if not library_dir.exists():
        return None, ""
    layout = AssetLayout.validate(library_dir)
    if not os.path.lexists(_home_host_dir(library_dir) / "binding.json"):
        return None, ""
    try:
        approval, _digest = load_approval(layout, now=now)
    except (ContractError, SecurityError) as exc:
        return None, str(exc)
    return approval, ""


def _covers(approval: HomeHostApproval, destination: Destination, now: datetime) -> bool:
    return (
        approval.endpoint == destination.endpoint
        and approval.tailnet_suffix == destination.tailnet_suffix
        and approval.node_stable_id == destination.node_stable_id
        and set(CLASSES) <= set(approval.classes)
        and approval.expires_at - now > RENEW_WITHIN
    )


def _replaces(current: HomeHostApproval, destination: Destination) -> str:
    """Why a new approval replaces ``current`` (which does not cover ``destination``)."""
    old = current.approval_id
    if (current.endpoint, current.node_stable_id) != (
        destination.endpoint,
        destination.node_stable_id,
    ):
        return (
            f"It replaces the approval {old}, which names {current.endpoint} "
            f"(node {current.node_stable_id})."
        )
    missing = [c for c in CLASSES if c not in current.classes]
    if missing:
        nouns = " and ".join(home_host.CLASS_NOUNS[c] for c in missing)
        return f"It replaces the approval {old}, which does not cover {nouns}."
    return f"It renews the approval {old}, which expires on {_until(current)}."


def _library_refused(exc: BaseException) -> ApprovalRefused:
    return ApprovalRefused(f"This app's library cannot be used: {exc}")


def propose(
    library_dir: Path,
    server_url: str,
    *,
    now: datetime,
    approved_by: str = APPROVER,
    resolver_factory: ResolverFactory = tailscale_resolver,
    token: Callable[[], str] = _token,
) -> Proposal:
    """What Approve would write for the configured server (Tailscale is asked first; nothing
    is written).

    Raises: ``ApprovalRefused`` (see ``endpoint_of``, ``find_destination``), or for a
    library that cannot be used."""
    destination = find_destination(endpoint_of(server_url), resolver_factory)
    try:
        current, why = _current(library_dir, now=now)
    except (SecurityError, OSError) as exc:
        raise _library_refused(exc) from exc
    if current is not None and _covers(current, destination, now):
        return Proposal(destination, None, current)
    if current is not None:
        reason = _replaces(current, destination)
    else:
        reason = f"It replaces the approval bound now: {why}" if why else ""
    approval = HomeHostApproval(
        approval_id=fresh_approval_id(taken_ids(library_dir), now, token=token),
        approved=True,
        endpoint=destination.endpoint,
        tailnet_suffix=destination.tailnet_suffix,
        node_stable_id=destination.node_stable_id,
        classes=CLASSES,
        approved_at=now,
        expires_at=now + timedelta(days=APPROVAL_DAYS),
        approved_by=approved_by,
        statement=STATEMENT,
    )
    return Proposal(destination, approval, current, reason)


def confirm(library_dir: Path, proposal: Proposal, *, now: datetime) -> Approved:
    """Approve was pressed: write the proposal's approval as shown, with its own digest, and
    bind it (the library is made, private, when it does not exist). When an approval that
    covers the destination stands by now (approved meanwhile), nothing is written.

    Raises: ``ApprovalRefused`` when the library cannot be used, an approval with this id
    exists, the approval does not stand at ``now`` (the clock went back), or the one that
    stood when the dialog opened no longer does: nothing is written, approve again."""
    try:
        current, _why = _current(library_dir, now=now)
        layout = home_host.library_layout(library_dir)
    except (SecurityError, OSError) as exc:
        raise _library_refused(exc) from exc
    if current is not None and _covers(current, proposal.destination, now):
        return Approved(current, written=False)
    approval = proposal.approval
    if approval is None:
        raise ApprovalRefused("The approval that stood no longer does: approve again.")
    try:
        approve(layout, approval, reviewed_sha256=record_sha256(approval), now=now)
    except FileExistsError as exc:
        raise ApprovalRefused(
            f"An approval named {approval.approval_id} exists already: approve again."
        ) from exc
    except (SecurityError, OSError) as exc:
        raise ApprovalRefused(f"The approval was not written ({exc}): approve again.") from exc
    return Approved(approval, written=True)


def standing(library_dir: Path, server_url: str, *, now: datetime) -> Standing:
    """Whether uploads wait for an approval, from the library and the settings alone."""
    host = (urlsplit(server_url).hostname or "").lower()
    if not host:
        return Standing(False, False, "Set the server on the Status screen first (Settings…).")
    try:
        current, why = _current(library_dir, now=now)
    except (SecurityError, OSError) as exc:
        return Standing(False, False, f"This app's library cannot be used: {exc}")
    if current is None:
        return Standing(True, False, f"{NEEDED}: {why or 'there is no approval here yet.'}")
    if current.endpoint != host:
        return Standing(
            True,
            False,
            f"{NEEDED}: the approval {current.approval_id} names {current.endpoint}, not {host}.",
            current,
        )
    missing = [c for c in CLASSES if c not in current.classes]
    if missing:
        nouns = " and ".join(home_host.CLASS_NOUNS[c] for c in missing)
        return Standing(
            True,
            False,
            f"{NEEDED}: the approval {current.approval_id} does not cover {nouns}.",
            current,
        )
    approved = (
        f"Uploads to {current.endpoint} are approved until {_until(current)} "
        f"(approval {current.approval_id})"
    )
    if current.expires_at - now <= RENEW_WITHIN:
        return Standing(False, True, f"{approved}: renew it before then.", current)
    return Standing(False, False, f"{approved}.", current)
