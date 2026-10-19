"""The native recording service's frozen Python runtime, and the bake-off's runtime receipts.

It identifies the Python runtime the Mac's recording service runs on (its distributions, its
Mach-O load commands and its receipt), and it reads the runtime install receipts and source
manifests the retired zero-shot bake-off copied into its audition runs (D-057), so that blind
review can still validate those runs.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import os
import re
import stat
import struct
import subprocess
import sys
import sysconfig
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from pydantic import Field, JsonValue, field_validator, model_validator

from .canonical import (
    canonical_json_bytes,
    canonical_sha256,
    open_regular_no_follow,
    sha256_file,
)
from .errors import ContractError, RuntimeUnavailableError, SecurityError
from .models import ContractModel
from .paths import (
    _reject_existing_symlink_components,
    reject_symlink_components,
    validate_relative_path,
)

_MACHO_MAGICS = {
    b"\xfe\xed\xfa\xce",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf",
    b"\xbf\xba\xfe\xca",
}


_PACKAGE_NAME = re.compile(r"^[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*$")


def canonical_package_name(value: str) -> str:
    """Return the canonical package-index spelling after strict validation."""

    if not _PACKAGE_NAME.fullmatch(value):
        raise ContractError(f"invalid package name in runtime lock: {value!r}")
    return re.sub(r"[-_.]+", "-", value).lower()


class RuntimeSanitizedFileLock(ContractModel):
    """One reviewed executable startup file removed before runtime imports."""

    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    reason: Literal["executable-pth"] = "executable-pth"

    @field_validator("path")
    @classmethod
    def _exact_pth_path(cls, value: str) -> str:
        relative = validate_relative_path(value)
        if not relative.startswith("lib/python3.12/site-packages/") or not relative.endswith(
            ".pth"
        ):
            raise ValueError("runtime sanitizer may remove only an exact site-packages .pth file")
        return relative


class LocalSourceManifest(ContractModel):
    schema_version: str = "cole-voice-lab.local-source/v1"
    source_id: str = Field(min_length=1, max_length=128)
    kind: str = Field(pattern=r"^(git|huggingface)$")
    repository: str = Field(min_length=1, max_length=1_024)
    revision: str = Field(pattern=r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
    files: dict[str, str] = Field(default_factory=dict)

    @field_validator("files")
    @classmethod
    def _portable_files(cls, value: dict[str, str]) -> dict[str, str]:
        for path, digest in value.items():
            validate_relative_path(path)
            if len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise ValueError("source manifest file digests must be lowercase SHA-256")
        return value


class RuntimeTreeEntry(ContractModel):
    path: str
    kind: Literal["directory", "file", "symlink"]
    mode: int = Field(ge=0, le=0o7777)
    size_bytes: int = Field(ge=0)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    link_target: str | None = Field(default=None, max_length=4_096)
    resolved_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def _portable_path(cls, value: str) -> str:
        return validate_relative_path(value)

    @model_validator(mode="after")
    def _entry_shape(self) -> RuntimeTreeEntry:
        if self.kind == "file":
            if (
                self.sha256 is None
                or self.link_target is not None
                or self.resolved_sha256 is not None
            ):
                raise ValueError("runtime file entries require only a SHA-256 digest")
        elif self.kind == "symlink":
            if self.link_target is None or self.sha256 is not None or self.resolved_sha256 is None:
                raise ValueError(
                    "runtime symlink entries require target text and resolved-byte evidence"
                )
        elif (
            self.sha256 is not None
            or self.link_target is not None
            or self.resolved_sha256 is not None
        ):
            raise ValueError("runtime directory entries cannot carry file/link evidence")
        return self


class RuntimeInstallReceipt(ContractModel):
    schema_version: Literal["cole-voice-lab.runtime-install-receipt/v1"] = (
        "cole-voice-lab.runtime-install-receipt/v1"
    )
    runtime_id: str
    runtime_lock_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    requirements_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_lock_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewed_wheel_sha256: dict[str, str]
    reviewed_build_toolchain_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    lab_wheel_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    lab_distribution_version: str = Field(min_length=1, max_length=128)
    runtime_python_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    python_runtime_lock_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sanitized_files: tuple[RuntimeSanitizedFileLock, ...]
    installed_distributions: dict[str, str] = Field(min_length=1)
    tree: tuple[RuntimeTreeEntry, ...] = Field(min_length=1)
    tree_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _tree_digest(self) -> RuntimeInstallReceipt:
        if self.tree_sha256 != canonical_sha256(
            [entry.model_dump(mode="json") for entry in self.tree]
        ):
            raise ValueError("runtime receipt tree digest does not match its entries")
        paths = [entry.path for entry in self.tree]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise ValueError("runtime receipt tree paths must be sorted and unique")
        sanitized_paths = [entry.path for entry in self.sanitized_files]
        if len(sanitized_paths) != len(set(sanitized_paths)):
            raise ValueError("runtime receipt sanitized paths must be unique")
        return self


def _normalized_real_directory(path: Path, *, label: str) -> Path:
    absolute = Path(os.path.abspath(path.expanduser()))
    cursor = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        cursor /= component
        try:
            metadata = cursor.lstat()
        except OSError as error:
            raise RuntimeUnavailableError(f"cannot inspect {label}: {cursor}") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise SecurityError(f"{label} contains a symlink component: {cursor}")
    metadata = absolute.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise SecurityError(f"{label} is not a real directory: {absolute}")
    return absolute


def _confined_symlink_resolution(
    candidate: Path,
    root: Path,
    *,
    label: str,
    relative: str,
) -> Path:
    """Resolve every symlink hop without permitting escape and re-entry."""

    try:
        pending = list(candidate.relative_to(root).parts)
    except ValueError as error:
        raise SecurityError(
            f"{label} symlink starts outside its reviewed root: {relative}"
        ) from error
    resolved_parts: list[str] = []
    symlink_hops = 0
    while pending:
        component = pending.pop(0)
        if component in {"", ".", ".."}:
            raise SecurityError(f"{label} has an unsafe symlink component: {relative}")
        current = root.joinpath(*resolved_parts, component)
        try:
            metadata = current.lstat()
        except OSError as error:
            raise SecurityError(f"{label} has a dangling symlink path: {relative}") from error
        if stat.S_ISLNK(metadata.st_mode):
            symlink_hops += 1
            if symlink_hops > 64:
                raise SecurityError(f"{label} symlink exceeds its hop bound: {relative}")
            target = Path(os.readlink(current))
            if target.is_absolute():
                if target.parts[: len(root.parts)] != root.parts:
                    raise SecurityError(f"{label} symlink escapes its reviewed root: {relative}")
                target_parts = target.parts[len(root.parts) :]
                target_base: list[str] = []
            else:
                target_parts = target.parts
                target_base = list(resolved_parts)
            for target_component in target_parts:
                if target_component in {"", "."}:
                    continue
                if target_component == "..":
                    if not target_base:
                        raise SecurityError(
                            f"{label} symlink escapes its reviewed root: {relative}"
                        )
                    target_base.pop()
                else:
                    target_base.append(target_component)
            pending = target_base + pending
            resolved_parts = []
            continue
        if pending and not stat.S_ISDIR(metadata.st_mode):
            raise SecurityError(f"{label} symlink traverses a non-directory: {relative}")
        resolved_parts.append(component)
    return root.joinpath(*resolved_parts)


def _immutable_tree_sha256(
    path: Path, *, label: str, excluded_relative_entries: tuple[str, ...] = ()
) -> str:
    """Hash one complete directory tree without following symlinks."""

    root = _normalized_real_directory(path, label=label)
    for relative in excluded_relative_entries:
        validate_relative_path(relative)
    entries: list[dict[str, object]] = []
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        parent = Path(directory)
        directory_names[:] = [
            name
            for name in directory_names
            if (parent / name).relative_to(root).as_posix() not in excluded_relative_entries
        ]
        file_names[:] = [
            name
            for name in file_names
            if (parent / name).relative_to(root).as_posix() not in excluded_relative_entries
        ]
        directory_names.sort()
        file_names.sort()
        parent = Path(directory)
        for name in (*directory_names, *file_names):
            candidate = parent / name
            metadata = candidate.lstat()
            relative = candidate.relative_to(root).as_posix()
            base: dict[str, object] = {
                "path": relative,
                "mode": stat.S_IMODE(metadata.st_mode),
                "size_bytes": metadata.st_size,
            }
            if stat.S_ISDIR(metadata.st_mode):
                base["kind"] = "directory"
            elif stat.S_ISREG(metadata.st_mode):
                if metadata.st_nlink != 1:
                    raise SecurityError(f"{label} contains a hardlinked file: {relative}")
                digest, size = sha256_file(candidate)
                base.update({"kind": "file", "sha256": digest, "size_bytes": size})
            elif stat.S_ISLNK(metadata.st_mode):
                _confined_symlink_resolution(
                    candidate,
                    root,
                    label=label,
                    relative=relative,
                )
                base.update({"kind": "symlink", "link_target": os.readlink(candidate)})
            else:
                raise SecurityError(f"{label} contains an unsafe file type: {relative}")
            entries.append(base)
            if len(entries) > 100_000:
                raise SecurityError(f"{label} exceeds its closure entry bound")
    return canonical_sha256(entries)


# The complete third-party closure of the recording service. cffi, cryptography and pycparser
# are the library vault's sealing closure (requirements/vault-macos.txt): background copying
# encrypts in a worker of this same frozen Python.
RECORDING_PYTHON_DISTRIBUTIONS = (
    "annotated-types",
    "cffi",
    "cryptography",
    "pycparser",
    "pydantic",
    "pydantic-core",
    "typing-extensions",
    "typing-inspection",
)
VAULT_REQUIREMENTS_LOCK = "requirements/vault-macos.txt"
_VAULT_DISTRIBUTIONS = frozenset({"cffi", "cryptography", "pycparser"})
# RECORD rows outside a distribution's tree that pip writes for its declared entry-point
# scripts (site-packages is <prefix>/lib/pythonX.Y/site-packages). They are launchers, not
# importable code, so they are neither fingerprinted nor frozen; any other row that leaves the
# tree is refused.
_LAUNCHER_RECORD_PREFIX = "../../../bin/"
_LAUNCHER_GROUPS = frozenset({"console_scripts", "gui_scripts"})
_LAUNCHER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def missing_recording_python_distribution_message(name: str) -> str:
    """How to install a missing recording-service distribution, for a refusal.

    Params: ``name``, one of ``RECORDING_PYTHON_DISTRIBUTIONS``.
    Returns: a message naming the distribution and the reviewed install route.
    Raises: nothing.
    """
    if name in _VAULT_DISTRIBUTIONS:
        route = (
            f"install the reviewed vault lock {VAULT_REQUIREMENTS_LOCK} into it "
            "(python -I -B -m pip install --require-hashes --no-deps --only-binary=:all: "
            f"-r {VAULT_REQUIREMENTS_LOCK})"
        )
    else:
        route = "install the package's dependencies into it (python -m pip install -e '.[dev]')"
    return f"the recording service Python has no installed {name}; {route}"


def recording_python_distribution(name: str) -> importlib.metadata.Distribution:
    """The installed distribution of one recording-service dependency.

    Params: ``name``, one of ``RECORDING_PYTHON_DISTRIBUTIONS``.
    Returns: its ``importlib.metadata`` distribution in the running interpreter.
    Raises: ``RuntimeUnavailableError`` naming the install route (``VAULT_REQUIREMENTS_LOCK``
    for the sealing closure) when it is not installed.
    """
    try:
        return importlib.metadata.distribution(name)
    except importlib.metadata.PackageNotFoundError as error:
        message = missing_recording_python_distribution_message(name)
        raise RuntimeUnavailableError(message) from error


def recording_distribution_files(
    name: str, files: Sequence[str], launchers: Sequence[str]
) -> tuple[str, ...]:
    """The RECORD rows of a service distribution's own tree, launcher rows dropped.

    Params: ``name``; ``files``, the distribution's RECORD rows as strings; ``launchers``, the
    names of its ``console_scripts`` / ``gui_scripts`` entry points.
    Returns: every row except ``../../../bin/<launcher>`` rows, in the given order (the
    remaining rows are validated as relative paths by the caller).
    Raises: ``SecurityError`` when a launcher name is not a plain file name.
    """
    for launcher in launchers:
        if _LAUNCHER_NAME.fullmatch(launcher) is None:
            raise SecurityError(f"Python dependency declares an unsafe launcher: {name}")
    dropped = {f"{_LAUNCHER_RECORD_PREFIX}{launcher}" for launcher in launchers}
    return tuple(relative for relative in files if relative not in dropped)


def _declared_distribution_files(
    name: str, distribution: importlib.metadata.Distribution
) -> tuple[str, ...]:
    declared = distribution.files
    if declared is None or len(declared) > 100_000:
        raise SecurityError(f"Python dependency has no bounded installed file evidence: {name}")
    files = [str(item) for item in declared]
    launchers: list[str] = []
    if any(relative.startswith(_LAUNCHER_RECORD_PREFIX) for relative in files):
        launchers = [
            entry.name for entry in distribution.entry_points if entry.group in _LAUNCHER_GROUPS
        ]
    return recording_distribution_files(name, files, launchers)


type _MarkerKind = Literal["string", "name", "operator", "open", "close"]
type _MarkerToken = tuple[_MarkerKind, str]

# The PEP 508 marker subset the service closure uses: string variables compared with == / !=,
# version variables compared with the six ordering operators, and/or, and parentheses.
_MARKER_STRING_VARIABLES = frozenset(
    {
        "extra",
        "implementation_name",
        "platform_python_implementation",
        "platform_system",
        "sys_platform",
    }
)
_MARKER_VERSION_VARIABLES = frozenset({"python_full_version", "python_version"})
_MARKER_TOKEN = re.compile(
    r"""\s*(?:
        (?P<string>'[^'\\]*'|"[^"\\]*")
        |(?P<operator><=|>=|==|!=|<|>)
        |(?P<open>\()
        |(?P<close>\))
        |(?P<name>[a-z_]+)
    )""",
    re.VERBOSE,
)
_MARKER_RELEASE = re.compile(r"[0-9]{1,6}(?:\.[0-9]{1,6}){0,5}")
_MARKER_NAME_SEPARATORS = re.compile(r"[-_.]+")


class _MarkerParser:
    """Recursive descent over the marker subset; anything else is refused."""

    def __init__(self, marker: str, environment: Mapping[str, str]) -> None:
        self.marker = marker
        self.environment = environment
        self.tokens: list[_MarkerToken] = []
        position = 0
        text = marker.rstrip()
        while position < len(text):
            match = _MARKER_TOKEN.match(text, position)
            if match is None or match.lastgroup is None:
                raise self.refusal("an unsupported character")
            # Every alternative of the token pattern is one named group of a token kind.
            kind = cast(_MarkerKind, match.lastgroup)
            self.tokens.append((kind, match[kind]))
            position = match.end()
        self.position = 0

    def refusal(self, problem: str) -> SecurityError:
        return SecurityError(
            f"Python service dependency marker has {problem}, so its closure cannot be "
            f"decided: {self.marker!r}"
        )

    def take(self) -> _MarkerToken:
        if self.position >= len(self.tokens):
            raise self.refusal("a missing term")
        token = self.tokens[self.position]
        self.position += 1
        return token

    def peek_name(self, name: str) -> bool:
        return self.position < len(self.tokens) and self.tokens[self.position] == ("name", name)

    def evaluate(self) -> bool:
        result = self.disjunction()
        if self.position != len(self.tokens):
            raise self.refusal("trailing terms")
        return result

    def disjunction(self) -> bool:
        result = self.conjunction()
        while self.peek_name("or"):
            self.position += 1
            right = self.conjunction()
            result = result or right
        return result

    def conjunction(self) -> bool:
        result = self.term()
        while self.peek_name("and"):
            self.position += 1
            right = self.term()
            result = result and right
        return result

    def term(self) -> bool:
        kind, value = self.take()
        if kind == "open":
            result = self.disjunction()
            if self.take() != ("close", ")"):
                raise self.refusal("an unbalanced parenthesis")
            return result
        if kind != "name":
            raise self.refusal(f"a comparison that does not start with a variable ({value})")
        operator_kind, operator = self.take()
        if operator_kind != "operator":
            raise self.refusal(f"an unsupported operator ({operator})")
        literal_kind, literal = self.take()
        if literal_kind != "string":
            raise self.refusal(f"a comparison with a non-string value ({literal})")
        return self.compare(value, operator, literal[1:-1])

    def compare(self, variable: str, operator: str, literal: str) -> bool:
        if variable in _MARKER_STRING_VARIABLES:
            if operator not in ("==", "!="):
                raise self.refusal(f"a version operator on {variable} ({operator})")
            current = self.environment[variable]
            if variable == "extra":
                # Extra names compare normalized (PEP 685); the service installs no extras.
                current = _MARKER_NAME_SEPARATORS.sub("-", current).lower()
                literal = _MARKER_NAME_SEPARATORS.sub("-", literal).lower()
            return (current == literal) is (operator == "==")
        if variable not in _MARKER_VERSION_VARIABLES:
            raise self.refusal(f"an unsupported variable ({variable})")
        current_release = self.release(self.environment[variable])
        wanted = self.release(literal)
        width = max(len(current_release), len(wanted))
        left = current_release + (0,) * (width - len(current_release))
        right = wanted + (0,) * (width - len(wanted))
        return {
            "==": left == right,
            "!=": left != right,
            "<": left < right,
            "<=": left <= right,
            ">": left > right,
            ">=": left >= right,
        }[operator]

    def release(self, value: str) -> tuple[int, ...]:
        if _MARKER_RELEASE.fullmatch(value) is None:
            raise self.refusal(f"a version that is not a plain release ({value!r})")
        return tuple(int(part) for part in value.split("."))


def recording_marker_environment() -> dict[str, str]:
    """The marker variables of the running interpreter, with no extra installed.

    Returns: the values ``requirement_marker_applies`` reads.
    Raises: nothing.
    """
    import platform

    return {
        "extra": "",
        "implementation_name": sys.implementation.name,
        "platform_python_implementation": platform.python_implementation(),
        "platform_system": platform.system(),
        "python_full_version": platform.python_version(),
        "python_version": ".".join(platform.python_version_tuple()[:2]),
        "sys_platform": sys.platform,
    }


def requirement_marker_applies(marker: str, environment: Mapping[str, str]) -> bool:
    """Whether a ``Requires-Dist`` environment marker selects the requirement here.

    Only the subset the service closure uses is understood: ``extra``,
    ``implementation_name``, ``platform_python_implementation``, ``platform_system`` and
    ``sys_platform`` compared with ``==`` / ``!=``; ``python_version`` and
    ``python_full_version`` compared with ``==``, ``!=``, ``<``, ``<=``, ``>``, ``>=``
    against a plain release; ``and``, ``or`` and parentheses. The whole marker is parsed
    before its value is used.
    Params: ``marker``, the text after the requirement's ``;``; ``environment``, as
    ``recording_marker_environment`` returns it.
    Returns: the marker's value.
    Raises: ``SecurityError`` for anything outside the subset (another variable or operator,
    ``in``/``not``, a value on the left, a version that is not a plain release, unbalanced
    parentheses), so an unknown marker can never silently widen or narrow the closure.
    """
    return _MarkerParser(marker, environment).evaluate()


def _required_distribution(
    name: str, requirement: str, environment: Mapping[str, str]
) -> str | None:
    """The canonical name a ``Requires-Dist`` row requires here, or ``None`` if its marker
    excludes it."""
    requirement_text, separator, marker = requirement.partition(";")
    if separator and not requirement_marker_applies(marker, environment):
        return None
    package = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement_text.strip())
    if package is None:
        raise SecurityError(f"Python service dependency closure changed: {name}: {requirement}")
    return canonical_package_name(package[0])


@dataclass(frozen=True)
class MachOLoadCommands:
    """Bounded native loader evidence, read without invoking the executable."""

    minimum_macos: tuple[int, int, int] | None
    architectures: tuple[int, ...]
    dependencies: tuple[str, ...]
    install_names: tuple[str, ...]
    rpaths: tuple[str, ...]


def inspect_macho_load_commands(path: Path) -> MachOLoadCommands:
    """Read thin/fat 32/64-bit Mach-O deployment and dependency commands.

    @canonical native executable probe -- version/dependency evidence for release
    packaging and runtime closure. Reject malformed ranges and unbounded headers.
    """
    dependencies: set[str] = set()
    install_names: set[str] = set()
    rpaths: set[str] = set()
    versions: list[tuple[int, int, int]] = []
    architectures: set[int] = set()
    with open_regular_no_follow(path) as stream:
        initial = os.fstat(stream.fileno())
        magic = stream.read(4)
        fat = {
            b"\xca\xfe\xba\xbe": (">", False),
            b"\xbe\xba\xfe\xca": ("<", False),
            b"\xca\xfe\xba\xbf": (">", True),
            b"\xbf\xba\xfe\xca": ("<", True),
        }
        slices: list[tuple[int, int]] = []
        if magic in fat:
            endian, wide = fat[magic]
            raw = stream.read(4)
            if len(raw) != 4:
                raise SecurityError("Mach-O universal header is truncated")
            count = struct.unpack(endian + "I", raw)[0]
            if not 1 <= count <= 32:
                raise SecurityError("Mach-O architecture count exceeds its bound")
            size = 32 if wide else 20
            for _ in range(count):
                raw = stream.read(size)
                if len(raw) != size:
                    raise SecurityError("Mach-O architecture header is truncated")
                offset, length = struct.unpack_from(endian + ("QQ" if wide else "II"), raw, 8)
                if offset < 8 + count * size or length < 28 or offset + length > initial.st_size:
                    raise SecurityError("Mach-O architecture range is invalid")
                slices.append((offset, length))
        else:
            slices.append((0, initial.st_size))
        thin = {
            b"\xce\xfa\xed\xfe": ("<", False),
            b"\xfe\xed\xfa\xce": (">", False),
            b"\xcf\xfa\xed\xfe": ("<", True),
            b"\xfe\xed\xfa\xcf": (">", True),
        }
        for offset, length in slices:
            stream.seek(offset)
            raw = stream.read(32)
            if len(raw) < 28 or raw[:4] not in thin:
                raise SecurityError("native executable is not a supported Mach-O image")
            endian, wide = thin[raw[:4]]
            header_size = 32 if wide else 28
            cpu, _, _, count, command_bytes, _ = struct.unpack_from(endian + "IIIIII", raw, 4)
            if count > 4096 or command_bytes > 8 * 1024**2 or header_size + command_bytes > length:
                raise SecurityError("Mach-O load command range exceeds its bound")
            architectures.add(cpu)
            stream.seek(offset + header_size)
            commands = stream.read(command_bytes)
            cursor = 0
            for _ in range(count):
                if cursor + 8 > len(commands):
                    raise SecurityError("Mach-O load command is truncated")
                kind, size = struct.unpack_from(endian + "II", commands, cursor)
                if size < 8 or cursor + size > len(commands) or size % 4:
                    raise SecurityError("Mach-O load command size is invalid")
                command = commands[cursor : cursor + size]
                if kind in {
                    0xC,
                    0xD,
                    0x18,
                    0x80000018,
                    0x1F,
                    0x8000001F,
                    0x20,
                    0x23,
                    0x80000023,
                    0x8000001C,
                }:
                    minimum = 12 if kind == 0x8000001C else 24
                    if size < minimum:
                        raise SecurityError("Mach-O library command is truncated")
                    start = struct.unpack_from(endian + "I", command, 8)[0]
                    if start < minimum or start >= size or b"\0" not in command[start:]:
                        raise SecurityError("Mach-O library path is invalid")
                    try:
                        value = command[start:].split(b"\0", 1)[0].decode("utf-8")
                    except UnicodeDecodeError as error:
                        raise SecurityError("Mach-O library path is not UTF-8") from error
                    if not value or len(value) > 16_384:
                        raise SecurityError("Mach-O library path exceeds its bound")
                    target = (
                        rpaths
                        if kind == 0x8000001C
                        else install_names
                        if kind == 0xD
                        else dependencies
                    )
                    target.add(value)
                if kind in {0x24, 0x32}:
                    if size < (24 if kind == 0x32 else 16):
                        raise SecurityError("Mach-O deployment command is truncated")
                    if kind == 0x24 or struct.unpack_from(endian + "I", command, 8)[0] == 1:
                        version = struct.unpack_from(
                            endian + "I", command, 12 if kind == 0x32 else 8
                        )[0]
                        versions.append((version >> 16, (version >> 8) & 255, version & 255))
                cursor += size
            if cursor != len(commands):
                raise SecurityError("Mach-O load command byte count differs from its header")
        final = os.fstat(stream.fileno())
        if (initial.st_ino, initial.st_size, initial.st_mtime_ns, initial.st_ctime_ns) != (
            final.st_ino,
            final.st_size,
            final.st_mtime_ns,
            final.st_ctime_ns,
        ):
            raise SecurityError("Mach-O executable changed during its identity probe")
    return MachOLoadCommands(
        max(versions) if versions else None,
        tuple(sorted(architectures)),
        tuple(sorted(dependencies)),
        tuple(sorted(install_names)),
        tuple(sorted(rpaths)),
    )


def _python_distribution_identity(name: str) -> tuple[dict[str, object], tuple[Path, ...]]:
    distribution = recording_python_distribution(name)
    environment = recording_marker_environment()
    for requirement in distribution.requires or ():
        # This service intentionally installs no extras, so an extra's requirement is not
        # selected. A dependency the markers select here must already be in the closure: an
        # unknown one needs an explicit closure update rather than an incomplete fingerprint.
        required = _required_distribution(name, requirement, environment)
        if required is not None and required not in RECORDING_PYTHON_DISTRIBUTIONS:
            raise SecurityError(f"Python service dependency closure changed: {name}: {requirement}")
    root = Path(str(distribution.locate_file(""))).resolve(strict=True)
    entries: list[tuple[str, str, int]] = []
    macho: list[Path] = []
    for relative in sorted(_declared_distribution_files(name, distribution)):
        validate_relative_path(relative)
        candidate = root / relative
        # pip may record caches which were deliberately omitted by a bytecode-free
        # frozen release. Any present bytecode is executable and must be hashed.
        if candidate.suffix == ".pyc" and not candidate.exists():
            continue
        reject_symlink_components(root, candidate)
        with open_regular_no_follow(candidate) as stream:
            if os.fstat(stream.fileno()).st_nlink != 1:
                raise SecurityError(f"Python dependency contains a hard link: {candidate}")
            magic = stream.read(4)
        digest, size = sha256_file(candidate)
        entries.append((relative, digest, size))
        if magic in _MACHO_MAGICS:
            macho.append(candidate)
    if len({entry[0] for entry in entries}) != len(entries):
        raise SecurityError(f"Python dependency contains duplicate file declarations: {name}")
    imported = importlib.import_module(name.replace("-", "_"))
    source = Path(str(imported.__file__)).resolve(strict=True)
    if not source.is_relative_to(root) or source.relative_to(root).as_posix() not in {
        entry[0] for entry in entries
    }:
        raise SecurityError(f"Python dependency import differs from its installed files: {name}")
    return {
        "name": canonical_package_name(name),
        "version": distribution.version,
        "files_sha256": canonical_sha256(entries),
        "file_count": len(entries),
    }, tuple(macho)


def _python_linked_library_identity(candidates: Sequence[Path]) -> dict[str, object]:
    pending = list(candidates)
    seen: set[Path] = set()
    libraries: list[tuple[str, str, int]] = []
    system: set[str] = set()
    while pending:
        candidate = pending.pop().resolve(strict=True)
        if candidate in seen:
            continue
        seen.add(candidate)
        if len(seen) > 2048:
            raise SecurityError("Python linked library closure exceeds its bound")
        digest, size = sha256_file(candidate)
        libraries.append((str(candidate), digest, size))
        commands = inspect_macho_load_commands(candidate)
        for dependency in commands.dependencies:
            if dependency.startswith(("/usr/lib/", "/System/Library/")):
                system.add(dependency)
            elif dependency:
                pending.extend(
                    _resolve_macho_dependency(dependency, candidate, rpaths=commands.rpaths)
                )
    return {"files": sorted(libraries), "system_library_references": sorted(system)}


def _current_recording_python_environment_identity() -> dict[str, object]:
    """Bind actual service dependency/base/library bytes and the exact macOS system TCB.

    @canonical recording Python environment -- used at process startup and release
    preparation. No model/Qt imports, installs, version-only cache, or audio work.
    The base site-packages path is excluded because the isolated venv cannot
    import it; installed service distributions are independently byte-bound.
    """
    if sys.platform != "darwin":
        raise RuntimeUnavailableError("native recording service runtime identity requires macOS")
    base = Path(sys.base_prefix).resolve(strict=True)
    stdlib = Path(sysconfig.get_path("stdlib")).resolve(strict=True)
    excluded = stdlib / "site-packages"
    active_roots = tuple(Path(item).resolve() for item in sys.path if item)
    if any(
        item == excluded.resolve() or item.is_relative_to(excluded.resolve())
        for item in active_roots
    ):
        raise SecurityError("native recording requires an isolated Python environment")
    dependencies: list[dict[str, object]] = []
    macho: list[Path] = [Path(sys.executable).resolve(strict=True)]
    for name in RECORDING_PYTHON_DISTRIBUTIONS:
        identity, extensions = _python_distribution_identity(name)
        dependencies.append(identity)
        macho.extend(extensions)
    # The complete stdlib tree digest includes source and existing bytecode; the
    # extension roots also seed recursive non-system dynamic-library evidence.
    dynamic = stdlib / "lib-dynload"
    for path in sorted(dynamic.glob("*.so")):
        reject_symlink_components(stdlib, path)
        macho.append(path)
    system_version = Path("/System/Library/CoreServices/SystemVersion.plist")
    system_sha, system_size = sha256_file(system_version)
    return {
        "schema_version": "cole-voice-lab.recording-python-environment/v1",
        "python_version": sys.version,
        "implementation": sys.implementation.name,
        "base_prefix": str(Path(sys.base_prefix).resolve()),
        "stdlib_root": str(stdlib),
        "base_runtime_sha256": _immutable_tree_sha256(
            base,
            label="Python base runtime",
            excluded_relative_entries=(excluded.relative_to(base).as_posix(),),
        ),
        "excluded_base_site_packages": {
            "path": str(excluded),
            "link_target": os.readlink(excluded) if excluded.is_symlink() else None,
        },
        "dependencies": dependencies,
        "linked_libraries": _python_linked_library_identity(macho),
        "system_version_plist_sha256": system_sha,
        "system_version_plist_size_bytes": system_size,
        "kernel_build": os.uname().version,
    }


RECORDING_PYTHON_RUNTIME_RECEIPT = ".cole-voice-lab-recording-python-runtime.json"


class RecordingPythonFileStat(ContractModel):
    path: str = Field(min_length=1, max_length=4096)
    device: int = Field(ge=0)
    inode: int = Field(ge=0)
    mode: int = Field(ge=0)
    size_bytes: int = Field(ge=0)
    mtime_ns: int = Field(ge=0)
    ctime_ns: int = Field(ge=0)
    link_target: str | None = Field(default=None, max_length=4096)


class RecordingPythonRuntimeReceipt(ContractModel):
    schema_version: Literal["cole-voice-lab.recording-python-runtime-receipt/v1"] = (
        "cole-voice-lab.recording-python-runtime-receipt/v1"
    )
    environment_prefix: str
    environment: dict[str, JsonValue]
    environment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    entries: tuple[RecordingPythonFileStat, ...] = Field(min_length=1, max_length=50_000)

    @model_validator(mode="after")
    def _bound_identity(self) -> RecordingPythonRuntimeReceipt:
        if self.environment_sha256 != canonical_sha256(self.environment):
            raise ValueError("runtime receipt environment digest changed")
        paths = tuple(entry.path for entry in self.entries)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("runtime receipt entries must be unique and ordered")
        return self


def _recording_runtime_stat(path: Path) -> RecordingPythonFileStat:
    metadata = path.lstat()
    if not (
        stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
    ):
        raise SecurityError(f"runtime evidence includes an unsupported entry: {path}")
    if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
        raise SecurityError(f"runtime evidence includes a hard link: {path}")
    return RecordingPythonFileStat(
        path=str(path),
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=metadata.st_mode,
        size_bytes=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
        ctime_ns=metadata.st_ctime_ns,
        link_target=os.readlink(path) if stat.S_ISLNK(metadata.st_mode) else None,
    )


def _recording_runtime_entries(
    environment: dict[str, object],
) -> tuple[RecordingPythonFileStat, ...]:
    base = Path(sys.base_prefix).resolve(strict=True)
    excluded = Path(sysconfig.get_path("stdlib")).resolve(strict=True) / "site-packages"
    paths: set[Path] = {
        base,
        Path(sys.prefix),
        Path(sys.prefix) / "pyvenv.cfg",
        Path(os.path.abspath(sys.executable)),
        Path("/System/Library/CoreServices/SystemVersion.plist"),
    }
    for directory, directories, files in os.walk(base, followlinks=False):
        parent = Path(directory)
        paths.update(parent / name for name in (*directories, *files))
        directories[:] = [name for name in directories if parent / name != excluded]
    for name in RECORDING_PYTHON_DISTRIBUTIONS:
        distribution = recording_python_distribution(name)
        root = Path(str(distribution.locate_file(""))).resolve(strict=True)
        paths.add(root)
        for relative in _declared_distribution_files(name, distribution):
            candidate = root / validate_relative_path(relative)
            if candidate.suffix == ".pyc" and not candidate.exists():
                continue
            paths.add(candidate)
            paths.update(parent for parent in candidate.parents if parent.is_relative_to(root))
    # The service's own installed package belongs to the trusted base. Only its source
    # files and packaged SQL are digested elsewhere, and Python resolves an extension
    # module before a source module, so an added .so or .pyc beside a service module
    # would shadow it while every recorded identity still matched. Binding the tree's
    # directory and file identities closes that. A development checkout is never
    # inside the environment prefix and is deliberately untouched.
    package = Path(__file__).resolve().parent
    if package.is_relative_to(Path(sys.prefix).resolve()):
        paths.add(package)
        for directory, directories, files in os.walk(package, followlinks=False):
            parent = Path(directory)
            paths.update(parent / name for name in (*directories, *files))
    linked = environment["linked_libraries"]
    if not isinstance(linked, dict) or not isinstance(linked.get("files"), list):
        raise SecurityError("runtime closure has no declared linked library evidence")
    for item in linked["files"]:
        if not isinstance(item, (tuple, list)) or not isinstance(item[0], str):
            raise SecurityError("runtime linked library evidence is malformed")
        paths.add(Path(item[0]))
    if len(paths) > 50_000:
        raise SecurityError("runtime stat evidence exceeds its bound")
    return tuple(_recording_runtime_stat(path) for path in sorted(paths, key=str))


def freeze_recording_python_runtime_receipt() -> Path:
    """Write one immutable release receipt after two stable full byte verifications.

    The receipt is adjacent to the venv, so creating it cannot invalidate tracked
    import directory identities. This is a build operation, never an interactive
    service mutation. Existing receipts are not replaced.
    """
    prefix = _reject_existing_symlink_components(Path(sys.prefix))
    path = prefix.parent / RECORDING_PYTHON_RUNTIME_RECEIPT
    if os.path.lexists(path):
        raise SecurityError("runtime receipt already exists; rebuild in a new release directory")
    first = _current_recording_python_environment_identity()
    before = _recording_runtime_entries(first)
    second = _current_recording_python_environment_identity()
    after = _recording_runtime_entries(second)
    if first != second or before != after:
        raise SecurityError("Python runtime changed while freezing its byte identity")
    receipt = RecordingPythonRuntimeReceipt.model_validate_json(
        canonical_json_bytes(
            {
                "schema_version": "cole-voice-lab.recording-python-runtime-receipt/v1",
                "environment_prefix": str(prefix),
                "environment": second,
                "environment_sha256": canonical_sha256(second),
                "entries": [entry.model_dump(mode="json") for entry in after],
            }
        )
    )
    data = canonical_json_bytes(receipt)
    if len(data) > 32 * 1024**2:
        raise SecurityError("Python runtime receipt exceeds its byte bound")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    parent = os.open(path.parent, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)
    return path


def recording_python_environment_identity() -> dict[str, object]:
    """Validate a release's previously verified bytes by exact filesystem identity.

    Any changed inode, ctime, mtime, mode, size, link target or import-directory
    identity fails closed. Development without a release receipt hashes all
    current bytes. Source and SQL identity remain independently recomputed.
    """
    prefix = Path(sys.prefix)
    path = prefix.parent / RECORDING_PYTHON_RUNTIME_RECEIPT
    if not os.path.lexists(path):
        return _current_recording_python_environment_identity()
    path = _reject_existing_symlink_components(path)
    with open_regular_no_follow(path) as stream:
        metadata = os.fstat(stream.fileno())
        if (
            metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or metadata.st_mode & 0o077
            or metadata.st_size > 32 * 1024**2
        ):
            raise SecurityError("Python runtime receipt must remain owned, private and bounded")
        raw = stream.read(32 * 1024**2 + 1)
    receipt = RecordingPythonRuntimeReceipt.model_validate_json(raw)
    if raw != canonical_json_bytes(receipt) or receipt.environment_prefix != str(prefix):
        raise SecurityError("Python runtime receipt does not bind this canonical environment")
    for entry in receipt.entries:
        candidate = Path(entry.path)
        if not candidate.is_absolute() or os.path.normpath(entry.path) != entry.path:
            raise SecurityError("runtime evidence path is not normalized and absolute")
        if _recording_runtime_stat(candidate) != entry:
            raise SecurityError(
                f"verified Python runtime changed; rebuild the preview: {entry.path}"
            )
    if receipt.environment.get("kernel_build") != os.uname().version:
        raise SecurityError("verified macOS kernel build changed; rebuild the preview")
    return {**receipt.environment, "verified_runtime_receipt_sha256": canonical_sha256(receipt)}


def _macho_rpaths(candidate: Path) -> tuple[str, ...]:
    completed = subprocess.run(
        ("/usr/bin/otool", "-l", str(candidate)),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": "/usr/bin:/bin"},
    )
    if completed.returncode != 0:
        raise RuntimeUnavailableError(f"cannot inspect Mach-O rpaths: {candidate}")
    lines = completed.stdout.splitlines()
    rpaths: list[str] = []
    for index, line in enumerate(lines):
        if line.strip() != "cmd LC_RPATH":
            continue
        for detail in lines[index + 1 : index + 5]:
            stripped = detail.strip()
            if stripped.startswith("path "):
                rpaths.append(stripped.removeprefix("path ").split(" (offset ", 1)[0])
                break
    return tuple(rpaths)


def _expand_loader_reference(value: str, candidate: Path) -> Path:
    if value == "@loader_path":
        return candidate.parent
    if value.startswith("@loader_path/"):
        return candidate.parent / value.removeprefix("@loader_path/")
    if value.startswith("@executable_path"):
        raise SecurityError(
            f"locked runtime uses context-dependent @executable_path: {candidate}: {value}"
        )
    return Path(value)


def _resolve_macho_dependency(
    dependency: str, candidate: Path, *, rpaths: tuple[str, ...] | None = None
) -> tuple[Path, ...]:
    if dependency.startswith("@rpath/"):
        suffix = dependency.removeprefix("@rpath/")
        resolved: list[Path] = []
        for raw_rpath in _macho_rpaths(candidate) if rpaths is None else rpaths:
            expanded = _expand_loader_reference(raw_rpath, candidate)
            target = expanded / suffix
            if target.exists():
                resolved.append(target.resolve(strict=True))
        if not resolved:
            raise SecurityError(
                f"locked runtime has an unresolved @rpath dependency: {candidate}: {dependency}"
            )
        return tuple(sorted(set(resolved)))
    expanded = _expand_loader_reference(dependency, candidate)
    if not expanded.is_absolute():
        raise SecurityError(
            f"locked runtime contains a non-absolute dependency: {candidate}: {dependency}"
        )
    return (expanded.resolve(strict=True),)


def _read_runtime_receipt(
    path: Path,
    *,
    expected_sha256: str | None = None,
) -> tuple[RuntimeInstallReceipt, str]:
    with open_regular_no_follow(path) as stream:
        before = os.fstat(stream.fileno())
        if before.st_nlink != 1 or before.st_size > 128 * 1024 * 1024:
            raise SecurityError("runtime install receipt has unsafe link count or size")
        payload = stream.read(128 * 1024 * 1024 + 1)
        after = os.fstat(stream.fileno())
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
        before.st_nlink,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
        after.st_nlink,
    )
    if len(payload) > 128 * 1024 * 1024 or identity_before != identity_after:
        raise SecurityError("runtime install receipt changed during its bounded read")
    digest = hashlib.sha256(payload).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise SecurityError("runtime install receipt digest differs from approved evidence")
    receipt = RuntimeInstallReceipt.model_validate_json(payload)
    if payload != canonical_json_bytes(receipt) + b"\n":
        raise SecurityError("runtime install receipt is not canonical")
    return receipt, digest


def load_persisted_runtime_receipt(
    path: Path,
    *,
    expected_sha256: str,
    runtime_id: str,
    runtime_lock_sha256: str,
) -> RuntimeInstallReceipt:
    """Validate a copied receipt without requiring its runtime to remain resident."""

    receipt, _ = _read_runtime_receipt(path, expected_sha256=expected_sha256)
    if receipt.runtime_id != runtime_id or receipt.runtime_lock_sha256 != runtime_lock_sha256:
        raise ContractError("persisted runtime receipt differs from the audition trial binding")
    return receipt
