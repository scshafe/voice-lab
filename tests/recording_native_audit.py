"""Canonical generated Python WAV-open evidence for private native benchmarks.

Startup controls prove that each actual service/worker installed its hook. Unknown
startup modes invalidate evidence; they never inherit the worker exemption.
"""

from __future__ import annotations

import inspect
import json
import os
import stat
from pathlib import Path
from typing import Any

from recording_fixtures import require_external_private_root

AUDIT_SCHEMA = "cole-voice-lab.recording-python-audit/v1"
SERVICE_IDENTITY_PREFLIGHT_COMMAND = (
    "from cole_voice_lab import recording_service; "
    "print(recording_service.recording_service_build_identity().model_dump_json())"
)


def _startup_role(argv: list[str], original_argv: list[str]) -> tuple[str, str | None]:
    """Use native mode flags and the interpreter's actual module invocation."""
    module = None
    if "-m" in original_argv:
        position = original_argv.index("-m")
        if position + 1 < len(original_argv):
            module = original_argv[position + 1]
    if "--native-workspace" in argv:
        return "service", module
    # Exact fixture/verification preflight (recording_native_benchmark.py and
    # verify_native_workspace.py). It is foreground work, never a worker exemption.
    if (
        argv == ["-c"]
        and len(original_argv) == 3
        and original_argv[1:] == ["-c", SERVICE_IDENTITY_PREFLIGHT_COMMAND]
    ):
        return "service", None
    if (
        module
        in {
            "cole_voice_lab.recording_analysis",
            "cole_voice_lab.recording_edit_jobs",
            "cole_voice_lab.recording_finetune_jobs",
            "cole_voice_lab.recording_package_jobs",
        }
        and "--worker" in argv
    ):
        return "worker", module
    if module == "cole_voice_lab.recording_discovery" and all(
        argument in argv
        for argument in (
            "--asset-root",
            "--group-id",
            "--inventory-fd",
            "--spool-fd",
            "--parent-fd",
        )
    ):
        return "worker", module
    return "unknown", module


_HOOK = """
import json, os, stat, sys, time

audit_path, asset_root, control_path, schema = __AUDIT_CONFIGURATION__
role, startup_module = _startup_role(list(sys.argv), list(sys.orig_argv))
startup_time = time.monotonic_ns()
startup_id = "%d-%d" % (os.getpid(), startup_time)

def append_audit(value):
    line = (json.dumps(value, separators=(",", ":"), ensure_ascii=True) + "\\n").encode("utf-8")
    descriptor = os.open(audit_path, os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        if os.write(descriptor, line) != len(line):
            raise RuntimeError("RECORDING.BENCHMARK_AUDIT_WRITE_FAILED")
    finally:
        os.close(descriptor)

append_audit({"kind": "startup", "schema_version": schema, "uptime_ns": startup_time,
    "pid": os.getpid(), "role": role, "startup_id": startup_id,
    "argv": list(sys.argv), "orig_argv": list(sys.orig_argv), "module": startup_module,
    "control_path": control_path, "asset_root": asset_root})

def observe(event, args):
    if event != "open" or not args or not isinstance(args[0], (str, bytes)):
        return
    path = os.path.abspath(os.fsdecode(args[0]))
    control = path == control_path
    if not control and not (path.endswith(".wav") and path.startswith(asset_root + os.sep)):
        return
    append_audit({"kind": "open", "uptime_ns": time.monotonic_ns(), "pid": os.getpid(),
        "role": role, "startup_id": startup_id, "path": path, "control": control})

sys.addaudithook(observe)
descriptor = os.open(control_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
try:
    metadata = os.fstat(descriptor)
    if not (stat.S_ISREG(metadata.st_mode) and metadata.st_size == 0 and
            metadata.st_uid == os.getuid() and metadata.st_nlink == 1 and
            not metadata.st_mode & 0o077):
        raise RuntimeError("RECORDING.BENCHMARK_AUDIT_INVALID_CONTROL")
finally:
    os.close(descriptor)
append_audit({"kind": "control_complete", "uptime_ns": time.monotonic_ns(), "pid": os.getpid(),
    "role": role, "startup_id": startup_id, "path": control_path})
"""


def _private_regular_file(path: Path, *, empty: bool = False) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o077
        or metadata.st_nlink != 1
        or (empty and metadata.st_size != 0)
    ):
        raise ValueError("RECORDING.BENCHMARK_AUDIT_UNSAFE_FILE")


def audit_site(directory: Path, asset_root: Path, log: Path) -> Path:
    """Write the private startup hook and one empty control outside the asset root."""
    require_external_private_root(asset_root)
    require_external_private_root(log.parent)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    require_external_private_root(directory)
    control = log.parent / "audit-positive-control.wav"
    if control.is_relative_to(asset_root):
        raise ValueError("RECORDING.BENCHMARK_AUDIT_CONTROL_INSIDE_ASSETS")
    for path in (log, control):
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            _private_regular_file(path, empty=path == control)
        else:
            os.close(descriptor)
    configuration = repr((str(log), str(asset_root), str(control), AUDIT_SCHEMA))
    # Embed this module's one role classifier, so generated startup and reader agree.
    content = (
        f"SERVICE_IDENTITY_PREFLIGHT_COMMAND = {SERVICE_IDENTITY_PREFLIGHT_COMMAND!r}\n"
        + inspect.getsource(_startup_role)
        + _HOOK.replace("__AUDIT_CONFIGURATION__", configuration)
    )
    site = directory / "sitecustomize.py"
    if site.exists() or site.is_symlink():
        _private_regular_file(site)
    site.write_text(content, encoding="utf-8")
    site.chmod(0o600)
    return directory


def _invalid(reason: str) -> ValueError:
    return ValueError(f"RECORDING.BENCHMARK_AUDIT_INVALID: {reason}")


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _invalid("duplicate JSON key")
        result[key] = value
    return result


def _integer(value: Any) -> bool:
    return type(value) is int and value > 0


def read_audit(log: Path) -> list[dict[str, Any]]:
    """Validate startup binding and return opens, including explicit control rows.

    Real-source and control opens share the same hook. Every startup must have one
    control. The caller excludes `control is True` from historical inspection counts.
    """
    _private_regular_file(log)
    starts: dict[str, dict[str, Any]] = {}
    controls: dict[str, int] = {}
    completions: dict[str, int] = {}
    entries: list[dict[str, Any]] = []
    startup_keys = {
        "kind",
        "schema_version",
        "uptime_ns",
        "pid",
        "role",
        "startup_id",
        "argv",
        "orig_argv",
        "module",
        "control_path",
        "asset_root",
    }
    open_keys = {"kind", "uptime_ns", "pid", "role", "startup_id", "path", "control"}
    text = log.read_text(encoding="utf-8")
    if not text or not text.endswith("\n"):
        raise _invalid("missing or truncated startup evidence")
    for line in text.splitlines():
        try:
            value = json.loads(line, object_pairs_hook=_object)
        except (json.JSONDecodeError, UnicodeError) as error:
            raise _invalid("malformed JSON record") from error
        if (
            not isinstance(value, dict)
            or not _integer(value.get("pid"))
            or not _integer(value.get("uptime_ns"))
        ):
            raise _invalid("record identity or clock is invalid")
        identity = value.get("startup_id")
        if not isinstance(identity, str) or not identity or len(identity) > 128:
            raise _invalid("startup identity is invalid")
        if value.get("kind") == "startup":
            if (
                set(value) != startup_keys
                or value["schema_version"] != AUDIT_SCHEMA
                or identity in starts
            ):
                raise _invalid("startup schema or identity is invalid")
            if any(
                not isinstance(value[key], list)
                or not all(isinstance(arg, str) for arg in value[key])
                for key in ("argv", "orig_argv")
            ):
                raise _invalid("startup argv is invalid")
            role, module = _startup_role(value["argv"], value["orig_argv"])
            if role == "unknown":
                raise ValueError("RECORDING.BENCHMARK_AUDIT_UNKNOWN_ROLE")
            if value["role"] != role or value["module"] != module:
                raise _invalid("startup role differs from actual argv")
            if value["control_path"] != str(log.parent / "audit-positive-control.wav"):
                raise _invalid("startup control path is invalid")
            if (
                not isinstance(value["asset_root"], str)
                or not Path(value["asset_root"]).is_absolute()
            ):
                raise _invalid("startup asset root is invalid")
            starts[identity] = value
            controls[identity] = 0
            completions[identity] = 0
            continue
        if value.get("kind") == "control_complete":
            if set(value) != open_keys - {"control"} or identity not in starts:
                raise _invalid("control completion has no valid startup")
            startup = starts[identity]
            if (
                controls[identity] != 1
                or completions[identity] != 0
                or value["pid"] != startup["pid"]
                or value["role"] != startup["role"]
                or value["path"] != startup["control_path"]
            ):
                raise _invalid("control completion differs from its open")
            control = next(
                entry
                for entry in reversed(entries)
                if entry["startup_id"] == identity and entry["control"]
            )
            if value["uptime_ns"] < control["uptime_ns"]:
                raise _invalid("control completion precedes its open")
            control["control_completed_uptime_ns"] = value["uptime_ns"]
            completions[identity] = 1
            continue
        if set(value) != open_keys or value.get("kind") != "open" or identity not in starts:
            raise _invalid("open has no valid startup")
        startup = starts[identity]
        if (
            value["pid"] != startup["pid"]
            or value["role"] != startup["role"]
            or value["uptime_ns"] < startup["uptime_ns"]
        ):
            raise _invalid("open differs from startup identity or clock")
        path = value["path"]
        if (
            not isinstance(path, str)
            or not Path(path).is_absolute()
            or not path.endswith(".wav")
            or type(value["control"]) is not bool
        ):
            raise _invalid("open path or control flag is invalid")
        if value["control"] != (path == startup["control_path"]):
            raise _invalid("control flag does not match exact control path")
        if not value["control"] and not path.startswith(startup["asset_root"] + os.sep):
            raise _invalid("source open is outside the declared asset root")
        result = {
            key: value[key] for key in ("uptime_ns", "pid", "role", "path", "control", "startup_id")
        }
        if value["control"]:
            controls[identity] += 1
            result.update({key: startup[key] for key in ("argv", "orig_argv", "module")})
        entries.append(result)
    if (
        not starts
        or any(count != 1 for count in controls.values())
        or any(count != 1 for count in completions.values())
    ):
        raise _invalid("each startup requires exactly one real open positive control")
    return entries
