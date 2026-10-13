"""The prompt studio (D-051, D-052; docs/design/RECORDING-V1.md §5, step 6): Record → Prompts
rebuilt on the Swift app's fixed workspace, corpus-first, over the corpus client
(``corpus/``). Nothing here is a QObject; ``screens/studio.py`` is the screen's model.

- ``presentation.py``: how a catalog prompt is shown (title, kind, banner, labels, search);
- ``library.py``: each prompt's status from the PC's library and this machine's takes,
  coverage, the Show/Labels/search filters and the next missing prompt;
- ``status.py``: the status line's words for the transport and the sync (``SyncStatus``).
"""
