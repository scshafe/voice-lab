"""The one HTTP client the app uses toward Pocket ID and the voice-lab server.

No cookies, no redirects: the bearer token is the only credential, and a redirect means
the server's door wanted a browser sign-in instead of accepting the token.
"""

from __future__ import annotations

from typing import Final

import httpx

from . import __version__

TIMEOUT: Final = httpx.Timeout(60.0, connect=15.0)
USER_AGENT: Final = f"voice-lab-desktop/{__version__}"


def new_http() -> httpx.Client:
    return httpx.Client(
        timeout=TIMEOUT, follow_redirects=False, cookies=None, headers={"User-Agent": USER_AGENT}
    )
