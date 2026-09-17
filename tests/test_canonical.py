from __future__ import annotations

import ctypes
import errno
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel

import cole_voice_lab.canonical as canonical
from cole_voice_lab.canonical import (
    canonical_json_bytes,
    canonical_sha256,
    fsync_directory,
    json_ready,
    open_regular_no_follow,
    read_json_object,
    rename_no_replace,
    rename_no_replace_at,
    sha256_file,
    sha256_stream,
    write_canonical_json,
    write_new_private_file,
)
from cole_voice_lab.errors import SecurityError


class _JsonModel(BaseModel):
    z: int
    a: str


def test_canonical_json_is_order_independent_and_compact() -> None:
    left = {"z": [2, 1], "a": {"b": True}}
    right = {"a": {"b": True}, "z": [2, 1]}
    assert canonical_json_bytes(left) == b'{"a":{"b":true},"z":[2,1]}'
    assert canonical_sha256(left) == canonical_sha256(right)


def test_canonical_json_supports_models_and_rejects_non_finite_numbers() -> None:
    assert canonical_json_bytes(_JsonModel(z=2, a="café")) == '{"a":"café","z":2}'.encode()
    with pytest.raises(ValueError, match="JSON compliant"):
        canonical_json_bytes({"unsafe": float("nan")})


def test_stream_hashing_counts_multiple_chunks() -> None:
    payload = b"a" * (canonical._CHUNK_BYTES + 17)
    digest, size = sha256_stream(io.BytesIO(payload))

    assert digest == canonical.hashlib.sha256(payload).hexdigest()
    assert size == len(payload)


def test_atomic_json_write_returns_exact_file_digest(tmp_path: Path) -> None:
    output = tmp_path / "evidence" / "record.json"
    digest = write_canonical_json(output, {"value": 1})

    assert json.loads(output.read_bytes()) == {"value": 1}
    assert sha256_file(output) == (digest, len(output.read_bytes()))
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert not tuple(output.parent.glob("*.tmp-*"))


def test_hashing_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("private")
    link = tmp_path / "link"
    link.symlink_to(target)

    with pytest.raises(SecurityError, match="safely open"):
        sha256_file(link)


def test_safe_open_rejects_missing_and_non_regular_files(tmp_path: Path) -> None:
    with pytest.raises(SecurityError, match="safely open"):
        open_regular_no_follow(tmp_path / "missing")
    with pytest.raises(SecurityError, match="regular file"):
        open_regular_no_follow(tmp_path)


def test_safe_open_fallback_without_no_follow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "regular"
    path.write_bytes(b"payload")
    monkeypatch.delattr(canonical.os, "O_NOFOLLOW")

    with open_regular_no_follow(path) as stream:
        assert stream.read() == b"payload"


def test_atomic_write_cleans_up_after_publish_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "record.json"
    monkeypatch.setattr(canonical.secrets, "token_hex", lambda _: "fixed")

    def fail_publish(_source: object, _destination: object) -> None:
        raise OSError("publish failed")

    monkeypatch.setattr(canonical.os, "replace", fail_publish)
    with pytest.raises(OSError, match="publish failed"):
        write_canonical_json(output, {"value": 1})

    assert not output.exists()
    assert not (tmp_path / ".record.json.tmp-fixed").exists()


def test_atomic_write_preserves_original_error_if_temp_is_already_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "record.json"
    monkeypatch.setattr(canonical.secrets, "token_hex", lambda _: "fixed")

    def remove_then_fail(source: object, _destination: object) -> None:
        Path(source).unlink()
        raise OSError("publish failed")

    monkeypatch.setattr(canonical.os, "replace", remove_then_fail)
    with pytest.raises(OSError, match="publish failed"):
        write_canonical_json(output, {"value": 1})


def test_atomic_write_closes_descriptor_when_fdopen_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    descriptor: int | None = None
    real_fstat = os.fstat

    def fail_fdopen(open_descriptor: int, *_args: object, **_kwargs: object) -> None:
        nonlocal descriptor
        descriptor = open_descriptor
        raise OSError("fdopen failed")

    monkeypatch.setattr(canonical.os, "fdopen", fail_fdopen)
    with pytest.raises(OSError, match="fdopen failed"):
        write_canonical_json(tmp_path / "record.json", {"value": 1})

    assert descriptor is not None
    with pytest.raises(OSError):
        real_fstat(descriptor)
    assert not tuple(tmp_path.glob("*.tmp-*"))


def test_read_json_object_accepts_only_objects(tmp_path: Path) -> None:
    object_path = tmp_path / "object.json"
    object_path.write_text('{"value": 1}', encoding="utf-8")
    array_path = tmp_path / "array.json"
    array_path.write_text("[]", encoding="utf-8")

    assert read_json_object(object_path) == {"value": 1}
    with pytest.raises(ValueError, match="expected a JSON object"):
        read_json_object(array_path)


def test_json_ready_preserves_dynamic_json_containers() -> None:
    mapping = {"value": [1, 2]}
    sequence = [mapping]
    assert json_ready(mapping) is mapping
    assert json_ready(sequence) is sequence


# -- write-once publishing ----------------------------------------------------------------


class FakeRename:
    """A C function stand-in: records its arguments, sets errno and returns ``result``."""

    def __init__(self, result: int = 0, error: int = 0) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[object, ...]] = []
        self.argtypes: tuple[object, ...] = ()
        self.restype: object = None

    def __call__(self, *arguments: object) -> int:
        self.calls.append(arguments)
        ctypes.set_errno(self.error)
        return self.result


class FakeLibc:
    def __init__(self, **functions: FakeRename) -> None:
        self.__dict__.update(functions)


def fake_platform(monkeypatch: pytest.MonkeyPatch, platform: str, **functions: FakeRename) -> None:
    monkeypatch.setattr(canonical, "_system_platform", lambda: platform)
    monkeypatch.setattr(canonical, "_c_library", lambda: FakeLibc(**functions))


def test_the_darwin_rename_flag_matches_the_sdk_header() -> None:
    if sys.platform != "darwin" or shutil.which("xcrun") is None:
        pytest.skip("needs the macOS SDK (xcrun)")
    sdk = subprocess.run(
        ["xcrun", "--show-sdk-path"], capture_output=True, text=True, timeout=60, check=True
    ).stdout.strip()
    header = (Path(sdk) / "usr/include/sys/stdio.h").read_text(encoding="utf-8")
    assert re.search(r"#define\s+RENAME_EXCL\s+0x0*4\b", header)
    assert re.search(
        r"int\s+renameatx_np\(int, const char \*, int, const char \*, unsigned int\)", header
    )
    assert canonical._RENAME_EXCL == 0x00000004
    fcntl_header = (Path(sdk) / "usr/include/sys/fcntl.h").read_text(encoding="utf-8")
    assert re.search(r"#define\s+AT_FDCWD\s+-2\b", fcntl_header)
    assert canonical._DARWIN_AT_FDCWD == -2


def test_the_platform_and_c_library_seams_are_the_real_ones() -> None:
    assert canonical._system_platform() == sys.platform
    assert canonical._c_library() is canonical._c_library()


@pytest.mark.skipif(sys.platform != "darwin", reason="exercises the real renamex_np")
def test_rename_no_replace_never_replaces_a_file_or_directory(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.write_bytes(b"new")
    target.write_bytes(b"kept")
    with pytest.raises(FileExistsError) as caught:
        rename_no_replace(source, target)
    assert caught.value.errno == errno.EEXIST and caught.value.filename == str(target)
    assert (source.read_bytes(), target.read_bytes()) == (b"new", b"kept")
    source_directory = tmp_path / "source-directory"
    target_directory = tmp_path / "target-directory"
    source_directory.mkdir()
    target_directory.mkdir()
    with pytest.raises(FileExistsError):
        rename_no_replace(source_directory, target_directory)
    # Even an empty directory or a dangling symlink at the target is kept.
    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "nowhere")
    with pytest.raises(FileExistsError):
        rename_no_replace(source, dangling)
    assert dangling.is_symlink() and source.read_bytes() == b"new"


@pytest.mark.skipif(sys.platform != "darwin", reason="exercises the real renamex_np")
def test_rename_no_replace_moves_files_directories_and_symlinks(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"payload")
    identity = source.stat().st_ino
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    rename_no_replace(source, elsewhere / "moved")
    moved = elsewhere / "moved"
    assert not source.exists() and moved.read_bytes() == b"payload"
    assert moved.stat().st_ino == identity
    directory = tmp_path / "directory"
    directory.mkdir()
    (directory / "inside").write_bytes(b"inside")
    rename_no_replace(directory, elsewhere / "directory")
    assert (elsewhere / "directory" / "inside").read_bytes() == b"inside"
    link = tmp_path / "link"
    link.symlink_to(moved)
    rename_no_replace(link, elsewhere / "link")
    assert (elsewhere / "link").is_symlink() and moved.read_bytes() == b"payload"


@pytest.mark.skipif(sys.platform != "darwin", reason="exercises the real renamex_np")
def test_rename_no_replace_reports_other_failures_with_the_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    with pytest.raises(FileNotFoundError) as caught:
        rename_no_replace(tmp_path / "missing", target)
    assert caught.value.filename == str(target)
    assert not isinstance(caught.value, canonical.NoReplaceRenameUnavailableError)
    directory = tmp_path / "directory"
    directory.mkdir()
    # Renaming a directory into itself is EINVAL, which also reports a refused flag.
    with pytest.raises(canonical.NoReplaceRenameUnavailableError) as refused:
        rename_no_replace(directory, directory / "inside")
    assert refused.value.errno == errno.EINVAL
    assert not isinstance(refused.value, canonical.NoReplaceRenameUnsupportedPlatformError)
    assert directory.is_dir() and not (directory / "inside").exists()


def test_the_linux_branch_calls_renameat2_with_noreplace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    renameat2 = FakeRename()
    fake_platform(monkeypatch, "linux", renameat2=renameat2)
    rename_no_replace(tmp_path / "source", tmp_path / "target")
    assert renameat2.calls == [
        (-100, os.fsencode(tmp_path / "source"), -100, os.fsencode(tmp_path / "target"), 1)
    ]
    assert renameat2.argtypes == (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    assert renameat2.restype is ctypes.c_int


def test_the_darwin_branch_calls_renameatx_np_with_rename_excl(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    renameatx_np = FakeRename()
    fake_platform(monkeypatch, "darwin", renameatx_np=renameatx_np)
    rename_no_replace(tmp_path / "source", tmp_path / "target")
    rename_no_replace_at(3, "source", 4, "target")
    assert renameatx_np.calls == [
        (-2, os.fsencode(tmp_path / "source"), -2, os.fsencode(tmp_path / "target"), 4),
        (3, b"source", 4, b"target", 4),
    ]
    assert renameatx_np.argtypes == (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    assert renameatx_np.restype is ctypes.c_int


def test_the_linux_branch_renames_between_the_given_descriptors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renameat2 = FakeRename(result=-1, error=errno.EEXIST)
    fake_platform(monkeypatch, "linux-gnu", renameat2=renameat2)
    with pytest.raises(FileExistsError) as caught:
        rename_no_replace_at(5, "temporary", 6, "blob")
    assert renameat2.calls == [(5, b"temporary", 6, b"blob", 1)]
    assert caught.value.filename == "blob"


@pytest.mark.parametrize(
    "arguments",
    [
        (-1, "source", 4, "target"),
        (3, "source", -100, "target"),
        (3, "", 4, "target"),
        (3, "source", 4, "."),
        (3, "..", 4, "target"),
        (3, "source", 4, "nested/target"),
        (3, "/absolute", 4, "target"),
        (3, "source", 4, "target\x00.json"),
        (3, "source\x00", 4, "target"),
    ],
)
def test_rename_no_replace_at_refuses_anything_but_one_component_per_descriptor(
    monkeypatch: pytest.MonkeyPatch, arguments: tuple[int, str, int, str]
) -> None:
    renameatx_np = FakeRename()
    fake_platform(monkeypatch, "darwin", renameatx_np=renameatx_np)
    with pytest.raises(ValueError, match="descriptor|component|null byte"):
        rename_no_replace_at(*arguments)
    assert renameatx_np.calls == []


@pytest.mark.parametrize("which", ["source", "target"])
def test_rename_no_replace_refuses_an_embedded_nul_before_any_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, which: str
) -> None:
    renameatx_np = FakeRename()
    fake_platform(monkeypatch, "darwin", renameatx_np=renameatx_np)
    source = tmp_path / ("source\x00evil" if which == "source" else "source")
    target = tmp_path / ("target\x00evil" if which == "target" else "target")
    with pytest.raises(ValueError, match="embedded null byte"):
        rename_no_replace(source, target)
    assert renameatx_np.calls == []


@pytest.mark.skipif(sys.platform != "darwin", reason="exercises the real renameatx_np")
def test_rename_no_replace_never_publishes_under_a_name_cut_at_a_nul(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"payload")
    with pytest.raises(ValueError, match="embedded null byte"):
        rename_no_replace(source, tmp_path / "target\x00.json")
    with pytest.raises(ValueError, match="embedded null"):
        write_new_private_file(tmp_path / "record\x00.json", b"payload")
    assert private_entries(tmp_path) == ["source"]


def open_directory(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY)


@pytest.mark.skipif(sys.platform != "darwin", reason="exercises the real renameatx_np")
def test_rename_no_replace_at_moves_between_directories_and_never_replaces(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "entry").write_bytes(b"moved")
    (first / "other").write_bytes(b"new")
    (second / "kept").write_bytes(b"kept")
    source, target = open_directory(first), open_directory(second)
    try:
        rename_no_replace_at(source, "entry", target, "entry")
        with pytest.raises(FileExistsError) as caught:
            rename_no_replace_at(source, "other", target, "kept")
        with pytest.raises(FileNotFoundError):
            rename_no_replace_at(source, "missing", target, "anything")
    finally:
        os.close(source)
        os.close(target)
    assert caught.value.errno == errno.EEXIST and caught.value.filename == "kept"
    assert private_entries(first) == ["other"]
    assert (second / "entry").read_bytes() == b"moved"
    assert (second / "kept").read_bytes() == b"kept"
    assert (first / "other").read_bytes() == b"new"


@pytest.mark.skipif(sys.platform != "darwin", reason="exercises the real renameatx_np")
def test_rename_no_replace_at_follows_the_open_directory_not_its_old_path(
    tmp_path: Path,
) -> None:
    shard = tmp_path / "shard"
    outside = tmp_path / "outside"
    shard.mkdir()
    outside.mkdir()
    (tmp_path / "temporary").write_bytes(b"payload")
    parent, target = open_directory(tmp_path), open_directory(shard)
    try:
        # The directory is swapped for a symlink after it was opened.
        shard.rename(tmp_path / "moved-shard")
        shard.symlink_to(outside)
        rename_no_replace_at(parent, "temporary", target, "blob")
    finally:
        os.close(parent)
        os.close(target)
    assert (tmp_path / "moved-shard" / "blob").read_bytes() == b"payload"
    assert private_entries(outside) == []


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        (errno.EEXIST, FileExistsError),
        (errno.EINVAL, canonical.NoReplaceRenameUnavailableError),
        (errno.ENOSYS, canonical.NoReplaceRenameUnavailableError),
        (errno.ENOTSUP, canonical.NoReplaceRenameUnavailableError),
        (errno.EOPNOTSUPP, canonical.NoReplaceRenameUnavailableError),
        (errno.ENOENT, FileNotFoundError),
        (errno.EACCES, PermissionError),
        (errno.EXDEV, OSError),
        (errno.EIO, OSError),
    ],
)
def test_the_linux_branch_maps_errno(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, number: int, expected: type[OSError]
) -> None:
    fake_platform(monkeypatch, "linux", renameat2=FakeRename(result=-1, error=number))
    with pytest.raises(OSError) as caught:
        rename_no_replace(tmp_path / "source", tmp_path / "target")
    assert type(caught.value) is expected
    assert caught.value.errno == number
    assert caught.value.filename == str(tmp_path / "target")
    assert os.strerror(number) in str(caught.value)


@pytest.mark.parametrize("platform", ["win32", "freebsd14", "cygwin"])
def test_a_platform_without_a_no_replace_rename_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform: str
) -> None:
    fake_platform(monkeypatch, platform)
    source = tmp_path / "source"
    source.write_bytes(b"kept")
    with pytest.raises(canonical.NoReplaceRenameUnsupportedPlatformError) as caught:
        rename_no_replace(source, tmp_path / "target")
    assert caught.value.errno == errno.ENOTSUP
    assert platform in str(caught.value)
    assert isinstance(caught.value, canonical.NoReplaceRenameUnavailableError)
    assert source.read_bytes() == b"kept" and not (tmp_path / "target").exists()


def test_a_c_library_without_renameat2_is_refused_as_unsupported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_platform(monkeypatch, "linux")
    with pytest.raises(canonical.NoReplaceRenameUnsupportedPlatformError, match="C library"):
        rename_no_replace(tmp_path / "source", tmp_path / "target")


def test_fsync_directory_flushes_only_a_real_directory(tmp_path: Path) -> None:
    fsync_directory(tmp_path)
    regular = tmp_path / "regular"
    regular.write_bytes(b"")
    link = tmp_path / "link"
    link.symlink_to(tmp_path)
    with pytest.raises(NotADirectoryError):
        fsync_directory(regular)
    with pytest.raises(OSError):
        fsync_directory(link)
    with pytest.raises(FileNotFoundError):
        fsync_directory(tmp_path / "missing")


def test_fsync_directory_closes_its_descriptor_when_fsync_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    closed: list[int] = []
    real_close = os.close

    def fail(_descriptor: int) -> None:
        raise OSError(errno.EIO, "synthetic fsync failure")

    def close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(canonical.os, "fsync", fail)
    monkeypatch.setattr(canonical.os, "close", close)
    with pytest.raises(OSError, match="synthetic fsync failure"):
        fsync_directory(tmp_path)
    assert len(closed) == 1


def private_entries(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir())


@pytest.mark.parametrize("mode", [0o600, 0o400, 0o700])
def test_write_new_private_file_publishes_complete_private_bytes(tmp_path: Path, mode: int) -> None:
    path = tmp_path / "record.json"
    previous = os.umask(0o277)
    try:
        write_new_private_file(path, b"complete bytes", mode=mode)
    finally:
        os.umask(previous)
    metadata = path.lstat()
    assert path.read_bytes() == b"complete bytes"
    assert stat.S_ISREG(metadata.st_mode) and stat.S_IMODE(metadata.st_mode) == mode
    assert metadata.st_nlink == 1
    assert private_entries(tmp_path) == ["record.json"]


def test_write_new_private_file_defaults_to_0600_and_accepts_empty_bytes(tmp_path: Path) -> None:
    path = tmp_path / "empty"
    write_new_private_file(path, b"")
    assert path.read_bytes() == b"" and stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o1600, 0o4600, 0o10600])
def test_write_new_private_file_refuses_a_non_private_mode_before_any_io(
    tmp_path: Path, mode: int
) -> None:
    with pytest.raises(ValueError, match="owner bits only"):
        write_new_private_file(tmp_path / "record", b"bytes", mode=mode)
    assert private_entries(tmp_path) == []


def test_write_new_private_file_never_replaces_and_leaves_no_temporary(tmp_path: Path) -> None:
    path = tmp_path / "record"
    path.write_bytes(b"kept")
    path.chmod(0o644)
    with pytest.raises(FileExistsError):
        write_new_private_file(path, b"new")
    assert path.read_bytes() == b"kept" and stat.S_IMODE(path.stat().st_mode) == 0o644
    assert private_entries(tmp_path) == ["record"]
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "nowhere")
    with pytest.raises(FileExistsError):
        write_new_private_file(link, b"new")
    assert link.is_symlink() and not (tmp_path / "nowhere").exists()
    assert private_entries(tmp_path) == ["link", "record"]


def test_write_new_private_file_needs_an_existing_parent(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        write_new_private_file(tmp_path / "missing" / "record", b"bytes")
    assert private_entries(tmp_path) == []


def test_write_new_private_file_writes_its_temporary_beside_the_target_then_syncs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[tuple[str, object]] = []
    real_rename = canonical.rename_no_replace
    monkeypatch.setattr(canonical.secrets, "token_hex", lambda size: "0123456789abcdef")

    def rename(source: Path, target: Path) -> None:
        metadata = source.lstat()
        events.append(("rename", (source.name, stat.S_IMODE(metadata.st_mode))))
        real_rename(source, target)

    def sync(directory: Path) -> None:
        events.append(("sync", (directory, (tmp_path / "record").read_bytes())))

    monkeypatch.setattr(canonical, "rename_no_replace", rename)
    monkeypatch.setattr(canonical, "fsync_directory", sync)
    write_new_private_file(tmp_path / "record", b"bytes", mode=0o400)
    assert events == [
        ("rename", (".record.tmp-0123456789abcdef", 0o400)),
        ("sync", (tmp_path, b"bytes")),
    ]


def test_write_new_private_file_refuses_a_planted_temporary_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(canonical.secrets, "token_hex", lambda size: "fixed")
    planted = tmp_path / ".record.tmp-fixed"
    planted.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(FileExistsError):
        write_new_private_file(tmp_path / "record", b"bytes")
    assert planted.is_symlink()
    assert private_entries(tmp_path) == [".record.tmp-fixed"]


@pytest.mark.parametrize("stage", ["write", "rename"])
def test_write_new_private_file_removes_its_temporary_on_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stage: str
) -> None:
    def fail(*_arguments: object) -> None:
        raise OSError(errno.EIO, f"synthetic {stage} failure")

    if stage == "write":
        monkeypatch.setattr(canonical.os, "fsync", fail)
    else:
        monkeypatch.setattr(canonical, "rename_no_replace", fail)
    with pytest.raises(OSError, match=f"synthetic {stage} failure"):
        write_new_private_file(tmp_path / "record", b"bytes")
    assert private_entries(tmp_path) == []


def test_write_new_private_file_keeps_the_original_error_if_its_temporary_is_gone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def remove_then_fail(source: Path, _target: Path) -> None:
        source.unlink()
        raise OSError(errno.EIO, "synthetic rename failure")

    monkeypatch.setattr(canonical, "rename_no_replace", remove_then_fail)
    with pytest.raises(OSError, match="synthetic rename failure"):
        write_new_private_file(tmp_path / "record", b"bytes")
    assert private_entries(tmp_path) == []


def test_write_new_private_file_closes_its_descriptor_when_fdopen_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    descriptor: int | None = None
    real_fstat = os.fstat

    def fail_fdopen(open_descriptor: int, *_args: object, **_kwargs: object) -> None:
        nonlocal descriptor
        descriptor = open_descriptor
        raise OSError("fdopen failed")

    monkeypatch.setattr(canonical.os, "fdopen", fail_fdopen)
    with pytest.raises(OSError, match="fdopen failed"):
        write_new_private_file(tmp_path / "record", b"bytes")
    assert descriptor is not None
    with pytest.raises(OSError):
        real_fstat(descriptor)
    assert private_entries(tmp_path) == []


def test_a_failed_directory_sync_is_reported_after_the_file_is_published(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail(_directory: Path) -> None:
        raise OSError(errno.EIO, "synthetic directory sync failure")

    monkeypatch.setattr(canonical, "fsync_directory", fail)
    with pytest.raises(OSError, match="directory sync failure"):
        write_new_private_file(tmp_path / "record", b"bytes")
    assert (tmp_path / "record").read_bytes() == b"bytes"
    assert private_entries(tmp_path) == ["record"]
