"""The one capture chain supported at launch: the miniDSP UMIK-1 measurement chain.

The chain's identity is fixed here and must match the chain registry's ``umik1`` entry:
the intake accepts only ``measurement`` chains whose ``deviceName`` is a UMIK, and checks
``sampleRate`` against the registry. The server serves the registry's client projection
(``GET /api/v1/chains``, voice-journey PR #1 in voice-lab #31); the Measurement screen reads
it and refuses to start a session when the registry describes this chain differently
(:func:`registry_disagreement`). Without the server (offline, or a server without the
route) the chain fixed here is used, and the intake still checks every session. Other
chains (later eras) are a reviewed change here and in the registry.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from cole_voice_lab.api.chains import ClientChain

CHAIN_ID = "umik1"
CHAIN_KIND = "measurement"
CONTROLLED = True
SAMPLE_RATE = 48000
BIT_DEPTH = 24
CHANNELS = 1
#: The registry's default mouth-to-capsule distance for this chain.
DEFAULT_DISTANCE_CM = 35
#: miniDSP's USB vendor id and the UMIK-1's product id (as Linux shows them in
#: /proc/asound/cardN/usbid).
USB_ID = "2752:0007"
LABEL = "miniDSP UMIK-1 (chain umik1, 48 kHz / 24-bit)"

# "Umik-1  Gain: 18dB", "UMIK-1", "umik1"; not "UMIK-2" or "UMIK-X".
_UMIK1 = re.compile(r"umik[\s_-]?1(?!\d)", re.IGNORECASE)
_ANY_UMIK = re.compile(r"umik", re.IGNORECASE)


def is_umik1(name: str, usb_id: str | None = None) -> bool:
    """True for a UMIK-1 by its USB id (when known) or its name."""
    if usb_id is not None and usb_id.lower() == USB_ID:
        return True
    return bool(_UMIK1.search(name or ""))


def is_other_umik(name: str) -> bool:
    """A miniDSP UMIK that is not a UMIK-1 (e.g. a UMIK-2): refused at launch."""
    return bool(_ANY_UMIK.search(name or "")) and not is_umik1(name)


def registry_disagreement(chains: Sequence[ClientChain]) -> str | None:
    """How the server's chain registry describes this chain differently from this app, or
    ``None`` when it agrees: its kind, USB id, sample rate and bit depth, where the registry
    states them (a value it leaves out contradicts nothing)."""
    entry = next((candidate for candidate in chains if candidate.id == CHAIN_ID), None)
    if entry is None:
        return f"the server's chain registry has no chain {CHAIN_ID}"
    usb = entry.device.usb if entry.device is not None else None
    found = {
        "kind": (entry.kind, CHAIN_KIND),
        "USB id": (usb.lower() if usb else usb, USB_ID),
        "sample rate": (entry.sample_rate, SAMPLE_RATE),
        "bit depth": (entry.bit_depth, BIT_DEPTH),
    }
    differences = [
        f"{name} {theirs!r} (this app: {ours!r})"
        for name, (theirs, ours) in found.items()
        if theirs is not None and theirs != ours
    ]
    if not differences:
        return None
    return (
        f"the server's chain registry describes {CHAIN_ID} differently: "
        + "; ".join(differences)
        + ". Recording is refused until the app or the registry is changed (a reviewed change)."
    )
