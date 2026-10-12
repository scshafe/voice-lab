"""What machine a run trained or a benchmark rendered on, and who else was using its GPU.

@canonical host_fingerprint -- a model comparison is fair only between like machines (D-049),
so training and benchmarks record the host they ran on: ``HostFingerprint`` (the CPU model
and count, the memory, each GPU the kernel lists with its PCI ids, name, driver and VRAM, the
ROCm release when one is installed, and whether Apple's MPS is the accelerator).
``GpuContention`` is an advisory snapshot of the GPU's other users when the benchmark starts
and ends: the VRAM in use on each card and the processes holding the ROCm device (the PC's
llama-swap shares the card). Both are read without privileges, from ``/proc`` and ``/sys``
on Linux and ``sysctl`` on macOS; whatever cannot be read is ``None``, never guessed.

``record_training_host`` writes the training host's fingerprint into a run's directory
(``training-host.json``) before it trains; ``training_host`` reads it back (``None`` for a
run trained before it was written).
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Final, Literal

from pydantic import Field, ValidationError

from .canonical import open_regular_no_follow, write_canonical_json
from .errors import VoiceLabError
from .models import ContractModel, utc_now
from .paths import AssetLayout, confined_path
from .recording_contracts import UtcTimestamp

TRAINING_HOST_FILE: Final = "training-host.json"
MAX_HOST_RECORD_BYTES: Final = 64 * 1024
_CARD = re.compile(r"^card[0-9]{1,3}$")
_TEXT_LIMIT: Final = 128

type Reader = Callable[[Path], str | None]
type Sysctl = Callable[[str], str | None]


class Accelerator(ContractModel):
    """One GPU the kernel lists (``/sys/class/drm/cardN/device``), or Apple's MPS."""

    vendor_id: str | None = Field(default=None, max_length=16)
    device_id: str | None = Field(default=None, max_length=16)
    name: str | None = Field(default=None, max_length=_TEXT_LIMIT)
    driver: str | None = Field(default=None, max_length=64)
    driver_version: str | None = Field(default=None, max_length=64)
    vram_bytes: int | None = Field(default=None, ge=0)


class HostFingerprint(ContractModel):
    """The machine a measurement ran on (every field read without privileges, or ``None``)."""

    schema_version: Literal["cole-voice-lab.host-fingerprint/v1"] = (
        "cole-voice-lab.host-fingerprint/v1"
    )
    system: str = Field(min_length=1, max_length=_TEXT_LIMIT)
    machine: str = Field(min_length=1, max_length=64)
    cpu_model: str | None = Field(max_length=_TEXT_LIMIT)
    cpu_count: int | None = Field(ge=1)
    memory_bytes: int | None = Field(ge=0)
    accelerators: tuple[Accelerator, ...] = Field(max_length=8)
    rocm_version: str | None = Field(max_length=64)
    mps: bool
    recorded_at: UtcTimestamp


class GpuContention(ContractModel):
    """Advisory: the GPU's other users at one moment, as far as an unprivileged process sees.

    ``vram_used_bytes`` is every card's VRAM in use (amdgpu's ``mem_info_vram_used``, all
    processes together); ``gpu_processes`` the names of the other processes that hold the
    ROCm device (``/sys/class/kfd/kfd/proc``), ``None`` when that cannot be listed.
    """

    vram_used_bytes: int | None = Field(ge=0)
    vram_total_bytes: int | None = Field(ge=0)
    gpu_processes: tuple[str, ...] | None = Field(max_length=64)


def _read(path: Path) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            return stream.read(256 * 1024)
    except OSError:
        return None


def _sysctl(name: str) -> str | None:
    try:
        completed = subprocess.run(
            ["/usr/sbin/sysctl", "-n", name],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


def _text(value: str | None, limit: int = _TEXT_LIMIT) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.split())[:limit]
    return cleaned or None


def _integer(value: str | None) -> int | None:
    try:
        return None if value is None else int(value.strip())
    except ValueError:
        return None


def _cpu_model(root: Path, read: Reader) -> str | None:
    for line in (read(root / "proc/cpuinfo") or "").splitlines():
        key, _, value = line.partition(":")
        if key.strip() in {"model name", "Hardware", "Processor"}:
            return _text(value)
    return None


def _memory_bytes(root: Path, read: Reader) -> int | None:
    for line in (read(root / "proc/meminfo") or "").splitlines():
        if line.startswith("MemTotal:"):
            kilobytes = _integer(line.split()[1]) if len(line.split()) > 1 else None
            return None if kilobytes is None else kilobytes * 1024
    return None


def _cards(root: Path) -> list[Path]:
    drm = root / "sys/class/drm"
    try:
        names = sorted(name for name in os.listdir(drm) if _CARD.match(name))
    except OSError:
        return []
    return [drm / name / "device" for name in names]


def _accelerator(device: Path, root: Path, read: Reader) -> Accelerator:
    try:
        driver = os.path.basename(os.readlink(device / "driver"))
    except OSError:
        driver = None
    version = None if driver is None else _text(read(root / f"sys/module/{driver}/version"), 64)
    return Accelerator(
        vendor_id=_text(read(device / "vendor"), 16),
        device_id=_text(read(device / "device"), 16),
        name=_text(read(device / "product_name")),
        driver=driver,
        driver_version=version,
        vram_bytes=_integer(read(device / "mem_info_vram_total")),
    )


def host_fingerprint(
    *,
    root: Path = Path("/"),
    read: Reader = _read,
    sysctl: Sysctl = _sysctl,
    system: str | None = None,
    machine: str | None = None,
) -> HostFingerprint:
    """This machine's fingerprint (``root``, ``read``, ``sysctl`` and the names are test seams)."""
    system = system or f"{platform.system()} {platform.release()}"
    machine = machine or platform.machine() or "unknown"
    if system.startswith("Darwin"):
        apple = machine == "arm64"
        return HostFingerprint(
            system=system[:_TEXT_LIMIT],
            machine=machine[:64],
            cpu_model=_text(sysctl("machdep.cpu.brand_string")),
            cpu_count=_integer(sysctl("hw.ncpu")),
            memory_bytes=_integer(sysctl("hw.memsize")),
            accelerators=(Accelerator(name="Apple GPU (MPS)", driver="metal"),) if apple else (),
            rocm_version=None,
            mps=apple,
            recorded_at=utc_now(),
        )
    return HostFingerprint(
        system=system[:_TEXT_LIMIT],
        machine=machine[:64],
        cpu_model=_cpu_model(root, read),
        cpu_count=os.cpu_count(),
        memory_bytes=_memory_bytes(root, read),
        accelerators=tuple(_accelerator(card, root, read) for card in _cards(root)[:8]),
        rocm_version=_text(read(root / "opt/rocm/.info/version"), 64),
        mps=False,
        recorded_at=utc_now(),
    )


def gpu_contention(
    *, root: Path = Path("/"), read: Reader = _read, own_pid: int | None = None
) -> GpuContention:
    """The GPU's other users now, as far as this process can see (advisory)."""
    used: list[int] = []
    total: list[int] = []
    for card in _cards(root):
        card_used = _integer(read(card / "mem_info_vram_used"))
        card_total = _integer(read(card / "mem_info_vram_total"))
        if card_used is not None:
            used.append(card_used)
        if card_total is not None:
            total.append(card_total)
    me = os.getpid() if own_pid is None else own_pid
    processes: tuple[str, ...] | None
    try:
        pids = sorted(
            int(name) for name in os.listdir(root / "sys/class/kfd/kfd/proc") if name.isdigit()
        )
    except OSError:
        processes = None
    else:
        names = (
            _text(read(root / f"proc/{pid}/comm"), 64) or f"pid {pid}" for pid in pids if pid != me
        )
        processes = tuple(sorted(names))[:64]
    return GpuContention(
        vram_used_bytes=sum(used) if used else None,
        vram_total_bytes=sum(total) if total else None,
        gpu_processes=processes,
    )


def record_training_host(
    layout: AssetLayout, run_root: str, fingerprint: HostFingerprint | None = None
) -> Path:
    """Write this host's fingerprint (or ``fingerprint``) as ``<run_root>/training-host.json``."""
    directory = confined_path(layout.root, run_root)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / TRAINING_HOST_FILE
    write_canonical_json(path, host_fingerprint() if fingerprint is None else fingerprint)
    return path


def training_host(layout: AssetLayout, run_root: str) -> HostFingerprint | None:
    """The fingerprint a run recorded before it trained (``None`` when it recorded none).

    Raises: ``VoiceLabError`` for a record that is there but is not a fingerprint.
    """
    path = confined_path(layout.root, run_root) / TRAINING_HOST_FILE
    if not os.path.lexists(path):
        return None
    with open_regular_no_follow(path) as stream:
        data = stream.read(MAX_HOST_RECORD_BYTES + 1)
    try:
        return HostFingerprint.model_validate_json(data)
    except ValidationError as error:
        raise VoiceLabError(f"{path.name} is not a host fingerprint: {error}") from error
