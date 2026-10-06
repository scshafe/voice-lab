"""The lab's versioned HTTP API on the home host (D-021, D-024, D-031).

@canonical api -- the ``/api/v1`` server of the voice platform design (phase 2: ``meta``
and ``intake`` only). ``contracts`` holds the strict request and response models,
``staging`` the resumable upload sessions under ``incoming/.uploads/``, ``auth`` the
bearer-token check against the identity provider's published keys, and ``server`` the
standard-library ``ThreadingHTTPServer`` that routes requests to them. The API never writes
``raw/``: a finalized upload becomes ``incoming/<session>``, which the network-less intake
publisher (``intake_publisher.py``) validates and publishes.
"""
