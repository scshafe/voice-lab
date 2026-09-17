"""Composition root: the one place that picks the implementation behind each storage port.

@canonical storage_binding -- every prompt and catalog store is opened through
open_catalog_reader or open_catalog_editor; no other module (besides the adapters and the two
native fixture seeders) imports an adapter. Both openers are local-only and read no binding
file: the catalog is the SQLite corpus store under provenance/corpus.
Related: catalog_ports.py (CatalogReader, CatalogEditor), corpus_store_sqlite.py (the
adapter), tests/test_storage_import_boundary.py (who may import an adapter),
tests/catalog_conformance.py (the port checks, run through these openers).
"""

from __future__ import annotations

from .catalog_ports import CatalogEditor, CatalogReader
from .corpus_store_sqlite import (
    CORPUS_DATABASE_NAME,
    CORPUS_DIRECTORY,
    CorpusStore,
    corpus_store_migration_resources,
)
from .paths import AssetLayout


def open_catalog_reader(layout: AssetLayout) -> CatalogReader:
    """Open the library's catalog for reading, as a context manager.

    Opening never creates a store, but it is not side-effect free: the SQLite store takes
    its lease and lock, creates its snapshot directory, discards crashed-publish scratch and
    migrates an empty schema-version-0 database.
    Params: ``layout`` is the validated asset root.
    Returns: an open ``CatalogReader``; ``with open_catalog_reader(layout) as catalog:``
    closes it.
    Raises: ``CorpusStoreError`` with ``CORPUS.STORE_MISSING`` when the library has no
    store, ``CORPUS.STORE_UNAVAILABLE`` when it cannot be opened, migrated or fails an
    integrity check; ``CorpusStoreSecurityError`` (``CORPUS.STORE_UNSAFE``) when a store
    directory or member is not owned and private, is a symlink, was replaced or is not part
    of the store; ``SecurityError`` for an unsafe root.
    """
    return CorpusStore(layout, create=False)


def open_catalog_editor(layout: AssetLayout, *, create: bool = False) -> CatalogEditor:
    """Open the library's catalog for reading and changing, as a context manager.

    Params: ``layout`` is the validated asset root; ``create`` allows an empty store to be
    created when the library has none (an existing store is opened either way).
    Returns: an open ``CatalogEditor``; ``with open_catalog_editor(layout) as catalog:``
    closes it.
    Raises: as ``open_catalog_reader`` (``CORPUS.STORE_MISSING`` only without ``create``).
    """
    return CorpusStore(layout, create=create)


def catalog_store_location(layout: AssetLayout) -> str:
    """Where the catalog of ``layout`` is kept, for operator output.

    Params: ``layout`` is the asset root the location is relative to.
    Returns: the store's database path relative to the asset root.
    Raises: nothing.
    """
    return f"provenance/{CORPUS_DIRECTORY}/{CORPUS_DATABASE_NAME}"


def catalog_store_build_resources() -> tuple[tuple[str, str, int], ...]:
    """Fingerprint the resources the catalog store applies, for a build identity.

    Returns: one ``("migrations/<name>", sha256 hex, byte length)`` entry per migration.
    Raises: as ``sqlite_migrations.migration_resources``.
    """
    return corpus_store_migration_resources()
