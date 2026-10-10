"""Make ``voice_lab_mirror`` importable from this checkout without installing it.

The mirror is a separate, standard-library-only package (it runs on a Mac with no
voice-lab install), so the repository's environment does not install it.
"""

from __future__ import annotations

import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))
