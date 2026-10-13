"""The corpus client (D-052, docs/design/RECORDING-V1.md step 5): takes recorded here first,
the PC's catalog pinned for a session, and the takes, decisions and trims sent to the PC in
order, offline-first.

- ``records.py``: the strict records it keeps on disk;
- ``catalog.py``: the PC's catalog, cached by snapshot digest and pinned per session;
- ``spool.py``: the takes, their state here and their outboxes;
- ``sync.py``: the sync engine (one pass, off the GUI thread);
- ``client.py``: ``CorpusClient``, what the prompt studio uses, and its scheduler;
- ``status.py``: ``SyncStatus``, what the UI shows.
"""
