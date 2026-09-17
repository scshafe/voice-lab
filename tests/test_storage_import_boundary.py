"""Only the composition root, the adapters themselves and the named seeders import an adapter.

Every module under ``src/cole_voice_lab`` and every Python file under ``scripts/`` (including
extension-less scripts) is parsed, and every import is found: top-level and function-local
``import`` / ``from ... import`` statements (relative ones resolved against the module's
package) and ``importlib.import_module`` / ``__import__`` / ``runpy.run_module`` calls with
string-literal names passed by position or keyword (including ``__import__``'s literal
``fromlist`` and ``level``). ``tests/`` is exempt: tests construct and patch adapters on purpose.
Later steps extend ``ADAPTERS`` (and an adapter's own allowed importers) as adapters land.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[1]
PACKAGE = "cole_voice_lab"
# The one module that picks implementations.
COMPOSITION_ROOT = "src/cole_voice_lab/storage_binding.py"
# The native fixture seeders build a private fixture library directly.
SEEDERS = ("scripts/verify_native_bridge.py", "scripts/verify_native_workspace.py")
# Adapter module -> files that may import it besides the adapter itself, the composition
# root and the seeders (e.g. the vault server may import the local store once it exists).
ADAPTERS: dict[str, tuple[str, ...]] = {
    "cole_voice_lab.corpus_store_sqlite": (),
    # The vault server (Step 7) serves the local stores.
    "cole_voice_lab.storage_local": ("src/cole_voice_lab/storage_server.py",),
    # The sealed store and the library key; library copies reach it only through the binding.
    "cole_voice_lab.storage_sealing": (),
}
# Third-party packages only one module may import (the vault's encryption).
EXCLUSIVE_PACKAGES: dict[str, str] = {"cryptography": "src/cole_voice_lab/storage_sealing.py"}
IMPORT_CALLS = frozenset({"import_module", "__import__", "run_module"})


@dataclass(frozen=True)
class Import:
    """One import of ``module`` found at ``path:line``."""

    path: str
    line: int
    module: str


def module_name(relative: Path) -> str | None:
    """The dotted module a ``src/`` file defines; ``None`` for anything else."""
    if relative.parts[:1] != ("src",):
        return None
    parts = list(relative.with_suffix("").parts[1:])
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def package_of(relative: Path) -> str | None:
    """The package relative imports in ``relative`` resolve against."""
    module = module_name(relative)
    if module is None:
        return None
    return module if relative.name == "__init__.py" else module.rpartition(".")[0]


def resolved(package: str | None, level: int, name: str | None) -> str | None:
    """``from <level dots><name>`` in ``package`` as an absolute name; unresolvable -> None."""
    if level == 0:
        return name
    if package is None:
        return None
    parts = package.split(".")
    if level - 1 >= len(parts):
        return None
    base = ".".join(parts[: len(parts) - (level - 1)])
    return f"{base}.{name}" if name else base


def imported_modules(tree: ast.AST, package: str | None) -> Iterator[tuple[int, str]]:
    """Every module name ``tree`` imports, wherever the import appears."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            base = resolved(package, node.level, node.module)
            if base is None:
                continue
            yield node.lineno, base
            for alias in node.names:
                # ``from package import module`` imports a submodule.
                yield node.lineno, f"{base}.{alias.name}"
        elif isinstance(node, ast.Call):
            function = node.func
            called = (
                function.attr
                if isinstance(function, ast.Attribute)
                else function.id
                if isinstance(function, ast.Name)
                else None
            )
            if called in IMPORT_CALLS:
                for name in call_imports(node, called, package):
                    yield node.lineno, name


def argument(call: ast.Call, index: int, keyword: str) -> ast.expr | None:
    """What ``call`` passes at position ``index`` or as ``keyword``; ``None`` for neither.

    Positions after a ``*args`` are unknown, so only the keyword is looked at for them.
    """
    for position, item in enumerate(call.args):
        if isinstance(item, ast.Starred):
            break
        if position == index:
            return item
    return next((item.value for item in call.keywords if item.arg == keyword), None)


def literal_string(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def call_imports(call: ast.Call, called: str, package: str | None) -> Iterator[str]:
    """The modules an ``import_module`` / ``__import__`` / ``run_module`` call imports.

    ``import_module(name, package)`` resolves leading dots against ``package`` (this file's
    package when that is not a literal). ``__import__(name, globals, locals, fromlist, level)``
    resolves a literal ``level`` against this file's package (``globals()`` at the call site)
    and imports ``<name>.<item>`` for each literal ``fromlist`` item. A non-literal ``level``
    is read as the default, absolute import.
    """
    name = literal_string(argument(call, 0, "mod_name" if called == "run_module" else "name"))
    if name is None:
        return
    stripped = name.lstrip(".")
    level = len(name) - len(stripped)
    anchor = package
    if called == "import_module":
        anchor = literal_string(argument(call, 1, "package")) or package
    elif called == "__import__":
        given = argument(call, 4, "level")
        if isinstance(given, ast.Constant) and type(given.value) is int and given.value >= 0:
            level += given.value
    base = resolved(anchor, level, stripped or None)
    if not base:
        return
    yield base
    fromlist = argument(call, 3, "fromlist") if called == "__import__" else None
    if isinstance(fromlist, (ast.List, ast.Tuple, ast.Set)):
        for item in fromlist.elts:
            member = literal_string(item)
            if member and member != "*":
                yield f"{base}.{member}"


def python_sources(root: Path) -> Iterator[tuple[Path, ast.AST]]:
    """Every scanned file under ``root`` with its syntax tree.

    ``src/cole_voice_lab/**/*.py`` and ``scripts/*.py`` must parse; an extension-less script
    is included when it parses, and must parse when its first line names Python.
    """
    candidates = sorted((root / "src" / PACKAGE).rglob("*.py"))
    candidates += sorted(path for path in (root / "scripts").rglob("*") if path.is_file())
    for path in candidates:
        if "__pycache__" in path.parts or path.suffix not in ("", ".py"):
            continue
        source = path.read_bytes()
        try:
            tree = ast.parse(source, filename=str(path))
        except (SyntaxError, ValueError):
            first_line = source.split(b"\n", 1)[0]
            if path.suffix == ".py" or (first_line.startswith(b"#!") and b"python" in first_line):
                raise
            continue
        yield path, tree


def adapter_imports(root: Path) -> list[Import]:
    """Every import of an adapter module under ``root``'s scanned sources."""
    found: list[Import] = []
    for path, tree in python_sources(root):
        relative = path.relative_to(root)
        for line, module in imported_modules(tree, package_of(relative)):
            for adapter in ADAPTERS:
                if module == adapter or module.startswith(f"{adapter}."):
                    found.append(Import(relative.as_posix(), line, adapter))
    return found


def allowed(found: Import) -> bool:
    own = module_name(Path(found.path))
    if own is not None and (own == found.module or own.startswith(f"{found.module}.")):
        return True  # the adapter itself
    return found.path in (COMPOSITION_ROOT, *SEEDERS, *ADAPTERS[found.module])


def violations(root: Path) -> list[Import]:
    return [found for found in adapter_imports(root) if not allowed(found)]


def test_only_the_composition_root_the_adapters_and_the_seeders_import_an_adapter() -> None:
    assert violations(REPOSITORY) == []
    importers = {found.path for found in adapter_imports(REPOSITORY)}
    # The scan sees the known importers, so an empty violation list means something.
    assert importers == {COMPOSITION_ROOT, *SEEDERS}
    scanned = {path.relative_to(REPOSITORY).as_posix() for path, _ in python_sources(REPOSITORY)}
    assert {COMPOSITION_ROOT, "src/cole_voice_lab/cli.py", "scripts/guard-private-assets"} <= (
        scanned
    )
    for adapter in ADAPTERS:
        assert (REPOSITORY / "src" / (adapter.replace(".", "/") + ".py")).is_file(), adapter


def exclusive_package_imports(root: Path) -> list[Import]:
    """Every import of an ``EXCLUSIVE_PACKAGES`` package under ``root``'s scanned sources."""
    found: list[Import] = []
    for path, tree in python_sources(root):
        relative = path.relative_to(root)
        for line, module in imported_modules(tree, package_of(relative)):
            for package in EXCLUSIVE_PACKAGES:
                if module == package or module.startswith(f"{package}."):
                    found.append(Import(relative.as_posix(), line, package))
    return found


def test_only_the_sealing_adapter_imports_cryptography() -> None:
    found = exclusive_package_imports(REPOSITORY)
    assert [item for item in found if item.path != EXCLUSIVE_PACKAGES[item.module]] == []
    # The scan sees the one importer, so an empty violation list means something.
    assert {item.path for item in found} == set(EXCLUSIVE_PACKAGES.values())


def test_the_exclusive_package_scan_finds_lazy_and_dynamic_imports(tmp_path: Path) -> None:
    write(tmp_path, "src/cole_voice_lab/__init__.py", "")
    write(
        tmp_path,
        "src/cole_voice_lab/storage_sealing.py",
        "from cryptography.hazmat.primitives.ciphers.aead import AESGCM\n",
    )
    write(
        tmp_path,
        "src/cole_voice_lab/library_replication.py",
        "\n".join(
            (
                "import importlib",
                "def seal():",
                "    from cryptography.exceptions import InvalidTag",  # 3
                "    return importlib.import_module('cryptography.fernet')",  # 4
                "import cryptography",  # 5
                "import cryptographyish",
                "",
            )
        ),
    )
    write(tmp_path, "scripts/keys.py", "__import__('cryptography', fromlist=['x509'])\n")
    found = exclusive_package_imports(tmp_path)
    assert {
        (item.path, item.line) for item in found if item.path != EXCLUSIVE_PACKAGES[item.module]
    } == {
        ("src/cole_voice_lab/library_replication.py", 3),
        ("src/cole_voice_lab/library_replication.py", 4),
        ("src/cole_voice_lab/library_replication.py", 5),
        ("scripts/keys.py", 1),
    }


def write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_the_scan_finds_every_import_form_of_an_adapter(tmp_path: Path) -> None:
    write(tmp_path, "src/cole_voice_lab/__init__.py", "")
    write(tmp_path, "src/cole_voice_lab/corpus_store_sqlite.py", "from . import corpus_store\n")
    write(
        tmp_path,
        "src/cole_voice_lab/storage_binding.py",
        "from .corpus_store_sqlite import CorpusStore\n",
    )
    write(
        tmp_path,
        "src/cole_voice_lab/rogue.py",
        "\n".join(
            (
                "import importlib",
                "from .corpus_store_sqlite import CorpusStore",  # 2
                "from . import corpus_store_sqlite",  # 3
                "import cole_voice_lab.corpus_store_sqlite as sqlite_store",  # 4
                "from cole_voice_lab import corpus_store_sqlite as again",  # 5
                "from cole_voice_lab.corpus_store import read_catalog_snapshot",
                "def lazily():",
                "    from .corpus_store_sqlite import CORPUS_DIRECTORY",  # 8
                "    return importlib.import_module('cole_voice_lab.corpus_store_sqlite')",  # 9
                "def relative():",
                "    return importlib.import_module('.corpus_store_sqlite', package)",  # 11
                "__import__('cole_voice_lab.corpus_store_sqlite')",  # 12
                "import_module = importlib.import_module",
                "import_module(name_from_elsewhere)",
                "importlib.import_module(name='cole_voice_lab.corpus_store_sqlite')",  # 15
                "importlib.import_module(package='cole_voice_lab', name='.corpus_store_sqlite')",
                "__import__('cole_voice_lab', fromlist=['corpus_store_sqlite'])",  # 17
                "__import__('cole_voice_lab', globals(), None, ('corpus_store_sqlite',))",  # 18
                "runpy.run_module(mod_name='cole_voice_lab.corpus_store_sqlite')",  # 19
                "__import__('corpus_store_sqlite', globals(), None, [], 1)",  # 20
                "__import__('', globals(), None, ['corpus_store_sqlite'], level=1)",  # 21
                "__import__('cole_voice_lab', fromlist=['corpus_store', '*'])",
                "importlib.import_module(*arguments)",
                "",
            )
        ),
    )
    write(
        tmp_path,
        "src/cole_voice_lab/workers/deep.py",
        "from ..corpus_store_sqlite import CorpusStore\nfrom ... import nothing\n"
        # Resolved against the named package (against this file's own, it would be nothing).
        "import_module('...corpus_store_sqlite', 'cole_voice_lab.workers.sub')\n",
    )
    write(
        tmp_path, "src/cole_voice_lab/workers/__init__.py", "from .. import corpus_store_sqlite\n"
    )
    write(
        tmp_path,
        "scripts/seed-anything",
        "#!/usr/bin/env python3\nfrom cole_voice_lab.corpus_store_sqlite import CorpusStore\n",
    )
    write(
        tmp_path, "scripts/verify_native_bridge.py", "import cole_voice_lab.corpus_store_sqlite\n"
    )
    write(tmp_path, "scripts/run.sh", "import cole_voice_lab.corpus_store_sqlite\n")
    # A shell script is not Python, whatever it names.
    write(
        tmp_path,
        "scripts/verify",
        "#!/bin/sh\nexport PYTHONPATH=src\nexec python3 -m cole_voice_lab.corpus_store_sqlite\n",
    )
    write(tmp_path, "scripts/relative.py", "from .corpus_store_sqlite import CorpusStore\n")
    write(tmp_path, "tests/test_anything.py", "import cole_voice_lab.corpus_store_sqlite\n")
    found = {(item.path, item.line) for item in violations(tmp_path)}
    assert found == {
        *(
            ("src/cole_voice_lab/rogue.py", line)
            for line in (2, 3, 4, 5, 8, 9, 11, 12, 15, 16, 17, 18, 19, 20, 21)
        ),
        ("src/cole_voice_lab/workers/deep.py", 1),
        ("src/cole_voice_lab/workers/deep.py", 3),
        ("src/cole_voice_lab/workers/__init__.py", 1),
        ("scripts/seed-anything", 2),
    }
    importers = {item.path for item in adapter_imports(tmp_path)}
    assert {COMPOSITION_ROOT, "scripts/verify_native_bridge.py"} <= importers
    # A Python script that does not parse is an error, not a file the scan skips.
    write(tmp_path, "scripts/broken", "#!/usr/bin/env python3\ndef broken(:\n")
    with pytest.raises(SyntaxError):
        violations(tmp_path)


def test_the_replication_modules_load_no_adapter_even_indirectly() -> None:
    """Library copies take ports: importing them loads no adapter, key or owner module."""
    code = (
        "import sys\n"
        "import cole_voice_lab.library_classification\n"
        "import cole_voice_lab.library_replication\n"
        "import cole_voice_lab.library_generations\n"
        "print('\\n'.join(sorted(sys.modules)))\n"
    )
    loaded = set(
        subprocess.run(
            [sys.executable, "-I", "-B", "-c", code],
            check=True,
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
            timeout=60,
        ).stdout.split()
    )
    assert {
        "cole_voice_lab.library_classification",
        "cole_voice_lab.library_replication",
        "cole_voice_lab.library_generations",
    } <= loaded
    forbidden = {
        *ADAPTERS,
        "cole_voice_lab.storage_binding",
        "cole_voice_lab.recording_takes",
        "cole_voice_lab.recording_contracts",
        "cole_voice_lab.recording_finetune_jobs",
        "cole_voice_lab.finetune",
        *EXCLUSIVE_PACKAGES,
    }
    assert forbidden & loaded == set()


@pytest.mark.parametrize(
    ("package", "level", "name", "expected"),
    [
        ("cole_voice_lab", 0, "x", "x"),
        ("cole_voice_lab", 1, "x", "cole_voice_lab.x"),
        ("cole_voice_lab", 1, None, "cole_voice_lab"),
        ("cole_voice_lab.workers", 2, "x", "cole_voice_lab.x"),
        ("cole_voice_lab", 2, "x", None),
        (None, 1, "x", None),
    ],
)
def test_relative_imports_resolve_against_the_importing_package(
    package: str | None, level: int, name: str | None, expected: str | None
) -> None:
    assert resolved(package, level, name) == expected
