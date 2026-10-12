"""Speech in Cole's voice for registered callers (D-044 to D-047, docs/SPEECH_API.md).

@canonical speech -- the asynchronous speech API of the voice platform design (§3.6).
``config`` is the speech configuration and its caller registry, ``release`` the voice
release Cole designates (D-045), ``contracts`` the API's and the records' strict models,
``store`` the file-based job queue under ``state/speech/`` that the API and the job runner
share, ``gpu`` the one GPU slot, ``backend`` the narrow port a render goes through and the
F5-TTS backend behind it (``worker`` is its subprocess), ``runner`` the network-less job
runner, ``webhooks`` signing and delivery, and ``routes`` the ``/api/v1/speech`` routes.
"""
