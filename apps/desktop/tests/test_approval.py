"""Approve uploads to my PC (D-021) without Qt (``approval.py``): every field is derived (the
endpoint from the settings, the tailnet and the node from Tailscale, both classes, a 365-day
expiry, who approves, a fresh id); a lookup Tailscale refuses writes nothing; a standing
approval is never written twice; an expired, moved, narrower or expiring one is replaced by a
new one, bound; and what the library alone says about uploads. Tailscale is recorded answers
(``tailscale_fakes``): no ``tailscale`` binary runs. Synthetic names only."""

from __future__ import annotations

import dataclasses
import os
import re
import stat
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from home_host_fixtures import ENDPOINT, NODE, NOW, TAILNET, make_approval
from tailscale_fakes import (
    NOT_RUNNING,
    PEER_ADDRESS,
    FakeTailscale,
    again,
    completed,
    status,
    whois,
)
from voice_lab_desktop import approval
from voice_lab_desktop.approval import ApprovalRefused, Approved, Destination, Standing
from voice_lab_desktop.measurement import home_host

from cole_voice_lab.home_host import (
    HomeHostApproval,
    HomeHostClass,
    HomeHostLedger,
    approve,
    load_approval,
    record_sha256,
)
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.tailnet import TailscaleCliResolver

SERVER = f"https://{ENDPOINT}"
BOTH: tuple[HomeHostClass, ...] = ("capture", "measurement")
STABLE_ID = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")


@pytest.fixture
def library(tmp_path: Path) -> Path:
    """This app's library (``<data dir>/library``), not made yet: the first approval makes it."""
    return tmp_path / "data" / "library"


def approvals(library: Path) -> list[str]:
    """The approval files the library holds."""
    folder = library / "provenance" / "home-host" / "approvals"
    return sorted(os.listdir(folder)) if folder.exists() else []


def files(library: Path) -> dict[str, bytes]:
    """Every file under the library, with its bytes."""
    found = {}
    for path in sorted(library.rglob("*")):
        if path.is_file():
            found[str(path.relative_to(library))] = path.read_bytes()
    return found


def bind(library: Path, made: HomeHostApproval, *, now: datetime = NOW) -> None:
    """``made`` written and bound in the library, as the CLI or an earlier Approve left it."""
    layout = home_host.library_layout(library)
    approve(layout, made, reviewed_sha256=record_sha256(made), now=now)


def confirm(
    library: Path, proposal: approval.Proposal, *, now: datetime, server: str = SERVER
) -> Approved:
    """Approve pressed, with the settings naming ``server`` and ``library``."""
    return approval.confirm(proposal, server_url=server, library_dir=library, now=now)


def propose(
    library: Path,
    fake: FakeTailscale | None = None,
    *,
    server: str = SERVER,
    now: datetime = NOW,
    **options: Any,
) -> approval.Proposal:
    chosen = fake if fake is not None else FakeTailscale()
    return approval.propose(library, server, now=now, resolver_factory=chosen.factory, **options)


def test_every_field_is_derived(library: Path) -> None:
    fake = FakeTailscale()
    proposal = propose(library, fake, server="https://Voice-Lab.Example.TS.net")
    made = proposal.approval
    assert made is not None and proposal.standing is None and proposal.reason == ""
    assert proposal.destination == Destination(ENDPOINT, TAILNET, NODE)
    assert made.endpoint == ENDPOINT  # the settings' host, lowercased
    assert (made.tailnet_suffix, made.node_stable_id, made.tag) == (TAILNET, NODE, "tag:voice-lab")
    assert made.classes == BOTH
    assert made.approved_at == NOW and made.expires_at == NOW + timedelta(days=365)
    assert made.approved_by == "Cole" and made.statement == "My recordings, to my own PC."
    assert STABLE_ID.fullmatch(made.approval_id)
    assert re.fullmatch(r"pc-home-host-20261006-[0-9a-f]{6}", made.approval_id)
    # Tailscale is asked as every upload asks it: the tailnet and the peer named the endpoint,
    # then who that peer is.
    assert fake.suffixes == [TAILNET]
    assert fake.calls == [["status", "--json"], ["whois", "--json", PEER_ADDRESS]]
    assert not library.exists()  # proposing writes nothing
    # What Approve reviews: this approval's digest, for these settings.
    assert proposal.digest == record_sha256(made)
    assert (proposal.server_url, proposal.library_dir) == (
        "https://Voice-Lab.Example.TS.net",
        library,
    )
    assert approval.classes_text(made.classes) == (
        "capture (takes, prompted sessions) · measurement (measurement sessions)"
    )
    assert approval.local_date(made.expires_at) == made.expires_at.astimezone().date().isoformat()
    # In the app, the resolver is the local Tailscale client's (not run here).
    real = approval.tailscale_resolver(TAILNET)
    assert isinstance(real, TailscaleCliResolver) and real.tailnet_suffix == TAILNET


def test_who_approves_comes_from_the_sign_in(library: Path) -> None:
    assert approval.approver_name({"name": "  Cole Example "}) == "Cole Example"
    assert approval.approver_name({"email": "owner@example.test"}) == "Cole"
    assert approval.approver_name({"name": "   "}) == "Cole"
    assert approval.approver_name({"name": 7}) == "Cole"
    assert approval.approver_name(None) == "Cole"
    assert approval.approver_name({"name": "x" * 300}) == "x" * 256
    made = propose(library, approved_by="Cole Example").approval
    assert made is not None and made.approved_by == "Cole Example"


def test_the_id_is_fresh_and_no_approval_has_it(library: Path) -> None:
    tokens = iter(["aaaaaa", "aaaaaa", "bbbbbb"])
    taken = {"pc-home-host-20261006-aaaaaa"}
    chosen = approval.fresh_approval_id(taken, NOW, token=lambda: next(tokens))
    assert chosen == "pc-home-host-20261006-bbbbbb"
    # The library holds an approval under the id the first token makes (another node's).
    other = make_approval(
        approval_id="pc-home-host-20261006-aaaaaa", node_stable_id="nOTHERNODE", classes=BOTH
    )
    bind(library, other)
    assert approval.taken_ids(library) == {other.approval_id}
    tokens = iter(["aaaaaa", "cccccc"])
    made = propose(library, token=lambda: next(tokens)).approval
    assert made is not None and made.approval_id == "pc-home-host-20261006-cccccc"
    assert approval.taken_ids(library / "nothing-here") == set()


@pytest.mark.parametrize(
    ("answers", "message"),
    [
        ([NOT_RUNNING], "Tailscale is not running on this machine, or did not answer"),
        ([FileNotFoundError(2, "No such file or directory")], "Tailscale is not running"),
        ([subprocess.TimeoutExpired(cmd="tailscale", timeout=10.0)], "did not answer within"),
        ([completed({"BackendState": "Stopped"})], "does not show voice-lab.example.ts.net"),
        ([completed(status(Peer={}))], "the tailnet has 0 nodes named voice-lab.example.ts.net"),
        (
            [completed(status(CurrentTailnet={"MagicDNSSuffix": "other.ts.net"}))],
            "this host is on the tailnet 'other.ts.net'",
        ),
        ([completed(status()), completed(whois(Tags=None))], "does not carry tag:voice-lab"),
        (
            [completed(status()), completed(whois(Tags=["tag:voice-lab-vault"]))],
            "does not carry tag:voice-lab, only",
        ),
        ([completed(status()), completed(whois(StableID="not stable!"))], "no usable node"),
    ],
    ids=[
        "not-running",
        "not-installed",
        "no-answer",
        "stopped",
        "no-peer",
        "another-tailnet",
        "untagged",
        "another-tag",
        "bad-node-id",
    ],
)
def test_a_lookup_tailscale_refuses_writes_nothing(
    library: Path, answers: list[Any], message: str
) -> None:
    with pytest.raises(ApprovalRefused, match=re.escape(message)):
        propose(library, FakeTailscale(answers=list(answers)))
    assert not library.exists()


def test_a_refused_lookup_leaves_a_library_as_it_was(library: Path) -> None:
    bind(library, make_approval(classes=BOTH, node_stable_id="nOTHERNODE"))
    before = files(library)
    untagged = FakeTailscale(answers=[completed(status()), completed(whois(Tags=[]))])
    with pytest.raises(ApprovalRefused, match="not approved: the node does not carry"):
        propose(library, untagged)
    assert files(library) == before


@pytest.mark.parametrize(
    ("server", "message"),
    [
        ("", "Set the server on the Status screen first"),
        ("http://127.0.0.1:8080", "is not a name on the tailnet"),
        ("https://localhost", "is not a name on the tailnet"),
        ("https://voice-lab", "is not a name on the tailnet"),
    ],
)
def test_the_server_must_be_a_name_on_the_tailnet(library: Path, server: str, message: str) -> None:
    fake = FakeTailscale(answers=[])
    with pytest.raises(ApprovalRefused, match=message):
        propose(library, fake, server=server)
    assert fake.calls == [] and not library.exists()


def test_approve_writes_and_binds_exactly_the_approval_shown(library: Path) -> None:
    proposal = propose(library)
    made = proposal.approval
    assert made is not None
    result = confirm(library, proposal, now=NOW + timedelta(seconds=30))
    assert result == Approved(made, written=True)
    layout = AssetLayout.validate(library)  # the library was made, private
    later = NOW + timedelta(minutes=1)
    stored, digest = load_approval(layout, now=later)
    assert stored == made and digest == record_sha256(made)
    path = library / "provenance" / "home-host" / "approvals" / f"{made.approval_id}.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o400
    # Every class the app uploads may start now; nothing was uploaded yet.
    for package_class in BOTH:
        home_host.approval_for(layout, SERVER, now=later, package_class=package_class)
    assert HomeHostLedger(layout).entries() == []
    assert approval.standing(library, SERVER, now=later) == Standing(
        False,
        False,
        f"Uploads to {ENDPOINT} are approved until {approval.local_date(made.expires_at)} "
        f"(approval {made.approval_id}).",
        made,
    )


def test_a_standing_approval_is_never_written_twice(library: Path) -> None:
    fake = again(again(FakeTailscale()))
    first = propose(library, fake)
    raced = propose(library, fake)  # Approve pressed twice before the first was confirmed
    assert first.approval is not None and raced.approval is not None
    assert confirm(library, first, now=NOW).written
    assert confirm(library, raced, now=NOW) == Approved(first.approval, written=False)
    # Pressed again later: the approval stands for this endpoint and node; nothing is written.
    later = NOW + timedelta(days=200)
    pressed = propose(library, fake, now=later)
    assert pressed.approval is None and pressed.standing == first.approval
    assert confirm(library, pressed, now=later) == Approved(first.approval, written=False)
    assert approvals(library) == [f"{first.approval.approval_id}.json"]


def test_an_expired_approval_is_renewed_with_a_new_id(library: Path) -> None:
    old = make_approval(
        approval_id="pc-home-host-2025",
        classes=BOTH,
        approved_at=NOW - timedelta(days=366),
        expires_at=NOW - timedelta(days=1),
    )
    bind(library, old, now=NOW - timedelta(days=300))
    found = approval.standing(library, SERVER, now=NOW)
    assert found.needed and not found.expiring
    assert found.text.startswith("Uploads need your OK: the home-host approval pc-home-host-2025")
    assert "does not stand now" in found.text
    proposal = propose(library)
    made = proposal.approval
    assert made is not None and proposal.standing is None
    assert made.approval_id != old.approval_id and made.expires_at == NOW + timedelta(days=365)
    assert proposal.reason.startswith("It replaces the approval bound now: the home-host approval")
    assert confirm(library, proposal, now=NOW).written
    stored, _digest = load_approval(AssetLayout.validate(library), now=NOW)
    assert stored == made
    # Approvals are write-once: the expired one stays beside the new one.
    assert approvals(library) == sorted([f"{old.approval_id}.json", f"{made.approval_id}.json"])
    assert not approval.standing(library, SERVER, now=NOW).needed


def test_one_about_to_expire_is_offered_and_renewed(library: Path) -> None:
    soon = make_approval(
        classes=BOTH, approved_at=NOW - timedelta(days=340), expires_at=NOW + timedelta(days=20)
    )
    bind(library, soon, now=NOW - timedelta(days=340))
    found = approval.standing(library, SERVER, now=NOW)
    assert (found.needed, found.expiring, found.approval) == (False, True, soon)
    assert found.text.endswith(f"(approval {soon.approval_id}): renew it before then.")
    proposal = propose(library)
    assert proposal.approval is not None and proposal.standing == soon
    until = approval.local_date(soon.expires_at)
    assert (
        proposal.reason == f"It renews the approval {soon.approval_id}, which expires on {until}."
    )
    assert confirm(library, proposal, now=NOW).written


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"node_stable_id": "nOTHERNODE"}, f"which names {ENDPOINT} (node nOTHERNODE)"),
        ({"endpoint": f"old-pc.{TAILNET}"}, f"which names old-pc.{TAILNET} (node {NODE})"),
        ({"classes": ("measurement",)}, "which does not cover prompted sessions"),
        ({"tailnet_suffix": "ts.net"}, f"which names the tailnet ts.net, not {TAILNET}"),
    ],
    ids=["another-node", "another-endpoint", "a-missing-class", "another-tailnet"],
)
def test_another_node_endpoint_or_class_is_replaced(
    library: Path, changes: dict[str, Any], reason: str
) -> None:
    old = make_approval(**{"classes": BOTH, **changes})
    bind(library, old)
    proposal = propose(library)
    assert proposal.standing == old and proposal.approval is not None
    assert proposal.reason.startswith(f"It replaces the approval {old.approval_id}, {reason}")
    assert confirm(library, proposal, now=NOW).written
    stored, _digest = load_approval(AssetLayout.validate(library), now=NOW)
    assert stored == proposal.approval


def test_what_the_library_says_about_uploads(library: Path, tmp_path: Path) -> None:
    assert approval.standing(library, "", now=NOW) == Standing(
        False, False, "Set the server on the Status screen first (Settings…)."
    )
    nothing = Standing(True, False, "Uploads need your OK: there is no approval here yet.")
    assert approval.standing(library, SERVER, now=NOW) == nothing
    assert not library.exists()  # reading made nothing
    home_host.library_layout(library)  # Uploads made it, empty
    assert approval.standing(library, SERVER, now=NOW) == nothing
    made = make_approval(classes=BOTH)
    bind(library, made)
    assert approval.standing(library, SERVER, now=NOW) == Standing(
        False,
        False,
        f"Uploads to {ENDPOINT} are approved until {approval.local_date(made.expires_at)} "
        "(approval pc-home-host).",
        made,
    )
    assert approval.standing(library, f"https://other.{TAILNET}", now=NOW) == Standing(
        True,
        False,
        f"Uploads need your OK: the approval pc-home-host names {ENDPOINT}, not other.{TAILNET}.",
        made,
    )
    narrow = tmp_path / "narrow"
    bind(narrow, make_approval(classes=("measurement",)))
    found = approval.standing(narrow, SERVER, now=NOW)
    assert found.needed and found.text == (
        "Uploads need your OK: the approval pc-home-host does not cover prompted sessions "
        "(capture packages)."
    )
    # A library that is not private: Approve cannot mend it, so it is not offered, and
    # neither proposing nor approving writes into it.
    shown = propose(library)
    library.chmod(0o755)
    unsafe = approval.standing(library, SERVER, now=NOW)
    assert not unsafe.needed and unsafe.text.startswith("This app's library cannot be used: ")
    with pytest.raises(ApprovalRefused, match="This app's library cannot be used"):
        propose(library)
    with pytest.raises(ApprovalRefused, match="This app's library cannot be used"):
        confirm(library, shown, now=NOW)


def test_a_library_whose_approvals_cannot_be_listed_is_refused(library: Path) -> None:
    home_host.library_layout(library)
    folder = library / "provenance" / "home-host" / "approvals"
    folder.mkdir(parents=True, mode=0o700)
    folder.chmod(0)
    try:
        with pytest.raises(ApprovalRefused, match="This app's library cannot be used"):
            propose(library)
    finally:
        folder.chmod(0o700)
    assert approvals(library) == []


def test_approve_refuses_what_it_cannot_write(library: Path, tmp_path: Path) -> None:
    fake = again(FakeTailscale())
    # An approval under the id shown appeared meanwhile (bound, for another node): nothing
    # is overwritten.
    proposal = propose(library, fake)
    assert proposal.approval is not None
    clash = make_approval(
        approval_id=proposal.approval.approval_id, node_stable_id="nOTHERNODE", classes=BOTH
    )
    bind(library, clash)
    with pytest.raises(ApprovalRefused, match="exists already: approve again"):
        confirm(library, proposal, now=NOW)
    # The clock went back before the approval's own time: it would not stand; nothing is
    # written, not even the library.
    other = tmp_path / "other"
    early = propose(other, fake)
    with pytest.raises(ApprovalRefused, match="would not stand now"):
        confirm(other, early, now=NOW - timedelta(minutes=5))
    assert not other.exists()
    # Not the approval shown (its digest is another's): refused before anything is written.
    altered = dataclasses.replace(early, digest="0" * 64)
    with pytest.raises(ApprovalRefused, match="not the one shown"):
        confirm(other, altered, now=NOW)
    assert not other.exists()
    # The settings name another server or library since: refused, nothing written.
    for server, folder in ((f"https://other.{TAILNET}", other), (SERVER, tmp_path / "moved")):
        with pytest.raises(ApprovalRefused, match="The settings changed"):
            confirm(folder, early, now=NOW, server=server)
    assert not other.exists() and not (tmp_path / "moved").exists()
    assert approval.still_current(early, SERVER.upper(), other)
    # Nothing to write because one stood when the dialog opened, but it has expired since.
    standing_one = tmp_path / "standing"
    bind(standing_one, make_approval(classes=BOTH))
    pressed = propose(standing_one)
    assert pressed.approval is None
    with pytest.raises(ApprovalRefused, match="no longer does"):
        confirm(standing_one, pressed, now=NOW + timedelta(days=400))
    # The library cannot be made (its folder is read-only), or the approval cannot be
    # written into it: refused, nothing bound.
    sealed = tmp_path / "sealed"
    sealed.mkdir(mode=0o500)
    unmade = sealed / "library"
    try:
        with pytest.raises(ApprovalRefused, match="This app's library cannot be used"):
            confirm(unmade, propose(unmade), now=NOW)
    finally:
        sealed.chmod(0o700)
    assert not unmade.exists()
    blocked = tmp_path / "blocked"
    folder = blocked / "provenance" / "home-host" / "approvals"
    home_host.library_layout(blocked)
    folder.mkdir(parents=True, mode=0o500)
    try:
        with pytest.raises(ApprovalRefused, match="The approval was not written"):
            confirm(blocked, propose(blocked), now=NOW)
    finally:
        folder.chmod(0o700)
    assert approvals(blocked) == [] and not (folder.parent / "binding.json").exists()
