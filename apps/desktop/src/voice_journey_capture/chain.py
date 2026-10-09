"""The one capture chain supported at launch: the miniDSP UMIK-1 measurement chain.

voice-journey's chain registry (``manifests/chains.json``) is not served over
its API, so the chain's identity is fixed here and must match the registry's
``umik1`` entry exactly: the intake accepts only ``measurement`` chains whose
``deviceName`` is a UMIK, and checks ``sampleRate`` against the registry.
Other chains (later eras) are a reviewed change here and in the registry.
"""

from __future__ import annotations

import re

CHAIN_ID = "umik1"
CHAIN_KIND = "measurement"
CONTROLLED = True
SAMPLE_RATE = 48000
BIT_DEPTH = 24
CHANNELS = 1
#: The registry's default mouth-to-capsule distance for this chain.
DEFAULT_DISTANCE_CM = 35
#: miniDSP's USB vendor id and the UMIK-1's product id (as Linux shows them in /proc/asound/cardN/usbid).
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
