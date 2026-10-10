"""The chain registry as a capture client sees it: ``GET /api/v1/chains``.

@canonical api.chains -- the port of voice-journey's ``src/chains.mjs`` (its draft PR #1,
"retire the AVFoundation capture path, add GET /api/chains"; design §5.3). The registry
(``voice-journey.chains.v1``: the packaged ``resources/journey-chains.json``, or
``VOICE_JOURNEY_CHAINS``) is the source of truth; a client gets only what it needs to pick
and verify its device: per chain ``id``, ``kind``, ``label``, ``device`` (``name`` match
and ``usb`` id, or ``null``), ``sampleRate``, ``bitDepth``, ``eraStart`` and
``pinnedInputVolume``. Distance and notes are never served (and the packaged registry has
no calibration path at all, §5.4).

:func:`chains_for_client` is ``chainsForClient``, expression for expression (``??``,
``||`` and ``undefined`` keys through ``journey/jscompat.py``), and is what the phase-3
alias ``GET /api/chains`` answers verbatim. ``/api/v1/chains`` answers the same projection
in the strict :class:`ChainRegistryView` envelope (snake_case like the rest of
``/api/v1``). The routes live with the journey routes (``journey_routes.py``: the bearer
check, ``503 journey_unavailable`` without ``serve --journey``); the registry is read on
every request, as the JS did, so a changed file is served without a restart.

Refusals: a missing registry file is ``503 chain_registry_unavailable`` on both paths
(the JS's), and every other failure is a :class:`ChainRegistryError`: ``500
chain_registry_invalid`` on ``/api/v1``, ``500 {"error": <message>}`` on the alias. For a
registry with another ``schemaVersion`` the message is the JS's. Differences from the JS,
all on registries the JS crashed on with its runtime's message: an unreadable or
unparseable file is "chain registry is not readable JSON" (the JS sent the OS or parser
message, which names the path), and a ``chains`` member that is not an array, or a chain
that is ``null``, has its own message (the JS threw a ``TypeError``). ``/api/v1`` is
stricter than the alias: a projected chain whose values do not fit :class:`ClientChain`
(a missing ``chainId``, a string sample rate) is ``500 chain_registry_invalid`` there,
where the alias passes the values through as the JS did.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from pydantic import Field, StringConstraints, ValidationError

from ..journey import jscompat as js
from ..journey.jsonio import MISSING, read_json
from ..models import ContractModel

CHAINS_SCHEMA_VERSION: Final = "voice-journey.chains.v1"
CLIENT_SCHEMA_VERSION: Final = "voice-journey.chains-client.v1"

type ChainText = Annotated[str, StringConstraints(min_length=1, max_length=256)]


class ChainRegistryError(ValueError):
    """The registry exists but cannot be served: not JSON, another schema, or malformed."""


class ChainDevice(ContractModel):
    """How a client recognizes the chain's device: a name match and a USB ``vid:pid``."""

    name: ChainText | None
    usb: ChainText | None


class ClientChain(ContractModel):
    """One chain as a client sees it (no calibration path, distance or notes)."""

    id: ChainText
    kind: ChainText | None
    label: ChainText
    device: ChainDevice | None
    sample_rate: int | None = Field(gt=0)
    bit_depth: int | None = Field(gt=0)
    era_start: ChainText | None
    pinned_input_volume: int | float | None = Field(allow_inf_nan=False)


class ChainRegistryView(ContractModel):
    """``GET /api/v1/chains``: the chain registry's client projection, in registry order."""

    schema_version: Literal["cole-voice-lab.api.chains/v1"] = "cole-voice-lab.api.chains/v1"
    chains: tuple[ClientChain, ...]


def _client_chain(chain: Any) -> dict[str, Any]:
    if chain is None:
        raise ChainRegistryError("chain registry has a null chain")
    device_name, usb_id = js.prop(chain, "deviceName"), js.prop(chain, "usbId")
    device = (
        {"name": js.coalesce(device_name, None), "usb": js.coalesce(usb_id, None)}
        if js.truthy(device_name) or js.truthy(usb_id)
        else None
    )
    return js.defined(
        id=js.prop(chain, "chainId"),
        kind=js.coalesce(js.prop(chain, "kind"), None),
        label=js.coalesce(js.prop(chain, "label"), js.prop(chain, "chainId")),
        device=device,
        sampleRate=js.coalesce(js.prop(chain, "sampleRate"), None),
        bitDepth=js.coalesce(js.prop(chain, "bitDepth"), None),
        eraStart=js.coalesce(js.get(js.prop(chain, "era"), "start"), None),
        pinnedInputVolume=js.coalesce(js.prop(chain, "pinnedInputVolume"), None),
    )


def chains_for_client(registry: Any) -> dict[str, Any]:
    """``chainsForClient(registry)``: the client projection, with the JS's keys.

    Raises: ``ChainRegistryError`` for a registry whose ``schemaVersion`` is not
    ``voice-journey.chains.v1`` (the JS's message), whose ``chains`` is not an array, or
    with a ``null`` chain."""

    version = js.get(registry, "schemaVersion")
    if version != CHAINS_SCHEMA_VERSION:
        found = js.to_string(js.coalesce(version, "missing"))
        raise ChainRegistryError(f"chain registry has unexpected schema: {found}")
    chains = js.coalesce(js.get(registry, "chains"), [])
    if not isinstance(chains, list):
        raise ChainRegistryError("chain registry's chains is not an array")
    return {
        "schemaVersion": CLIENT_SCHEMA_VERSION,
        "chains": [_client_chain(chain) for chain in chains],
    }


def read_chain_registry(path: Path) -> dict[str, Any] | None:
    """The client projection of the registry at ``path``; ``None`` when the file is missing.

    Raises: ``ChainRegistryError`` for a registry that cannot be read, is not JSON, or
    that :func:`chains_for_client` refuses."""

    try:
        registry = read_json(path)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise ChainRegistryError("chain registry is not readable JSON") from error
    return chains_for_client(registry)


def registry_view(projection: Mapping[str, Any]) -> ChainRegistryView:
    """The ``/api/v1`` envelope of a projection.

    Raises: ``ChainRegistryError`` when a chain's values do not fit ``ClientChain``."""

    def chain(item: Mapping[str, Any]) -> dict[str, Any]:
        device = item.get("device")
        return {
            "id": item.get("id", MISSING),
            "kind": item["kind"],
            "label": item.get("label", MISSING),
            "device": device,
            "sample_rate": item["sampleRate"],
            "bit_depth": item["bitDepth"],
            "era_start": item["eraStart"],
            "pinned_input_volume": item["pinnedInputVolume"],
        }

    chains = tuple(js.defined(chain(item)) for item in projection["chains"])
    try:
        return ChainRegistryView.model_validate({"chains": chains})
    except ValidationError as error:
        raise ChainRegistryError("chain registry has a chain a client cannot use") from error
