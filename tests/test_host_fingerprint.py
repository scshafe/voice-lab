"""The host a run trained or a benchmark rendered on, and the GPU's other users
(host_fingerprint.py, D-049): read from a synthetic /proc and /sys tree, never the real one's
values (only that reading this machine works)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cole_voice_lab.errors import VoiceLabError
from cole_voice_lab.host_fingerprint import (
    TRAINING_HOST_FILE,
    gpu_contention,
    host_fingerprint,
    record_training_host,
    training_host,
)
from cole_voice_lab.paths import AssetLayout


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    write(root / "proc/cpuinfo", "processor\t: 0\nmodel name\t: Synthetic CPU 8000\n")
    write(root / "proc/meminfo", "MemTotal:       65536000 kB\nMemFree: 1 kB\n")
    card = root / "sys/class/drm/card0/device"
    write(card / "vendor", "0x1002\n")
    write(card / "device", "0x73bf\n")
    write(card / "mem_info_vram_total", str(16 * 2**30) + "\n")
    write(card / "mem_info_vram_used", str(5 * 2**30) + "\n")
    write(root / "sys/bus/pci/drivers/amdgpu/.keep", "")
    os.symlink(root / "sys/bus/pci/drivers/amdgpu", card / "driver")
    write(root / "sys/module/amdgpu/version", "6.12.0\n")
    write(root / "sys/class/drm/card0-DP-1/status", "connected\n")  # a connector, not a card
    write(root / "opt/rocm/.info/version", "7.0.0-1\n")
    for pid, name in ((101, "llama-server"), (202, "python3"), (303, "")):
        (root / f"sys/class/kfd/kfd/proc/{pid}").mkdir(parents=True)
        if name:
            write(root / f"proc/{pid}/comm", name + "\n")
    return root


def test_a_linux_host_is_read_from_proc_and_sys(tree: Path) -> None:
    host = host_fingerprint(root=tree, system="Linux 7.2.8", machine="x86_64")
    assert (host.cpu_model, host.memory_bytes) == ("Synthetic CPU 8000", 65536000 * 1024)
    (card,) = host.accelerators
    assert (card.vendor_id, card.device_id, card.driver) == ("0x1002", "0x73bf", "amdgpu")
    assert (card.driver_version, card.vram_bytes, card.name) == ("6.12.0", 16 * 2**30, None)
    assert host.rocm_version == "7.0.0-1" and not host.mps


def test_what_cannot_be_read_is_none(tmp_path: Path) -> None:
    host = host_fingerprint(root=tmp_path, system="Linux 7.2.8", machine="x86_64")
    assert (host.cpu_model, host.memory_bytes, host.accelerators, host.rocm_version) == (
        None,
        None,
        (),
        None,
    )
    write(tmp_path / "proc/meminfo", "MemTotal: lots kB\n")
    write(tmp_path / "proc/cpuinfo", "flags: fpu\n")
    assert host_fingerprint(root=tmp_path, system="Linux", machine="x86_64").memory_bytes is None
    quiet = gpu_contention(root=tmp_path)
    assert (quiet.vram_used_bytes, quiet.vram_total_bytes, quiet.gpu_processes) == (
        None,
        None,
        None,
    )


def test_a_mac_is_read_from_sysctl() -> None:
    values = {"machdep.cpu.brand_string": "Apple M2", "hw.ncpu": "8", "hw.memsize": "17179869184"}
    host = host_fingerprint(sysctl=values.get, system="Darwin 25.0.0", machine="arm64")
    assert (host.cpu_model, host.cpu_count, host.memory_bytes) == ("Apple M2", 8, 2**34)
    assert host.mps and host.accelerators[0].driver == "metal"
    intel = host_fingerprint(sysctl=lambda name: None, system="Darwin 25.0.0", machine="x86_64")
    assert not intel.mps and intel.accelerators == () and intel.cpu_model is None


def test_this_machine_can_be_read() -> None:
    host = host_fingerprint()
    assert host.system and host.machine
    gpu_contention()  # whatever this machine lets an unprivileged process see


def test_the_gpus_other_users_are_listed(tree: Path) -> None:
    seen = gpu_contention(root=tree, own_pid=202)
    assert (seen.vram_used_bytes, seen.vram_total_bytes) == (5 * 2**30, 16 * 2**30)
    assert seen.gpu_processes == ("llama-server", "pid 303")


def test_a_runs_training_host_is_recorded_and_read(tree: Path, tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "lab")
    assert training_host(layout, "derived/finetune/run-1") is None
    host = host_fingerprint(root=tree, system="Linux 7.2.8", machine="x86_64")
    path = record_training_host(layout, "derived/finetune/run-1", host)
    assert (
        path.name == TRAINING_HOST_FILE and training_host(layout, "derived/finetune/run-1") == host
    )
    assert record_training_host(layout, "derived/finetune/run-2").is_file()  # this machine's
    path.chmod(0o600)
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(VoiceLabError, match="not a host fingerprint"):
        training_host(layout, "derived/finetune/run-1")
