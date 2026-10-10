"""The Voice Memos mirror sender for macOS (design §5.2, §5.5).

Standard library only: it runs from launchd on a Mac, under the C launcher that holds
the Full Disk Access grant, with no voice-lab install. ``python -m voice_lab_mirror``
runs the mirror; ``python -m voice_lab_mirror.host_access`` is the read-only corpus seam.
"""

__version__ = "0.1.0"
