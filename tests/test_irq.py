"""Unit tests for irq.py. No /proc or /sys access — all fixtures are synthetic."""

from pathlib import Path

import pytest

import irq  # noqa: I001

SAMPLE_INTERRUPTS = """\
           CPU0       CPU1       CPU2       CPU3
 27:          0          0          0          0  IO-APIC-edge      timer
 45:       1234       5678         10          0  PCI-MSI-edge      eth0-TxRx-0
 46:        100        200        300        400  PCI-MSI-edge      eth0-TxRx-1
 47:          1          2          3          4  PCI-MSI-edge      eth1-TxRx-0
 60:         11         22         33         44  PCI-MSI-edge      megasas0-msix0
 61:          9          8          7          6  PCI-MSI-edge      megasas0-msix1
NMI:          0          0          0          0   Non-maskable interrupts
"""


@pytest.fixture
def interrupts_file(tmp_path: Path) -> Path:
    p = tmp_path / "interrupts"
    p.write_text(SAMPLE_INTERRUPTS)
    return p


# --- parsing helpers --------------------------------------------------------


def test_parse_cpu_list_ranges_and_singletons() -> None:
    assert irq.parse_cpu_list("0-3,5,7-8") == [0, 1, 2, 3, 5, 7, 8]
    assert irq.parse_cpu_list("2") == [2]
    assert irq.parse_cpu_list("") == []


def test_format_cpu_list_collapses_ranges() -> None:
    assert irq.format_cpu_list([0, 1, 2, 4]) == "0-2,4"
    assert irq.format_cpu_list([5]) == "5"
    assert irq.format_cpu_list([0, 2, 4]) == "0,2,4"
    assert irq.format_cpu_list([]) == ""


def test_parse_cpu_list_format_roundtrip() -> None:
    assert irq.format_cpu_list(irq.parse_cpu_list("0-2,4")) == "0-2,4"


def test_normalize_affinity_plain_and_grouped() -> None:
    assert irq.normalize_affinity("ff\n") == 255
    assert irq.normalize_affinity("00000001,00000000") == 1 << 32
    assert irq.normalize_affinity("") == 0


def test_mask_to_cpus() -> None:
    assert irq.mask_to_cpus(0b1011) == [0, 1, 3]
    assert irq.mask_to_cpus(1 << 32) == [32]
    assert irq.mask_to_cpus(0) == []


def test_parse_interrupts_default_filters(interrupts_file: Path) -> None:
    pairs = list(irq.parse_interrupts(interrupts_file, irq.DEFAULT_FILTERS))
    assert pairs == [
        ("eth0", 45),
        ("eth0", 46),
        ("eth1", 47),
        ("megasas0", 60),
        ("megasas0", 61),
    ]


def test_parse_interrupts_skips_header(interrupts_file: Path) -> None:
    assert list(irq.parse_interrupts(interrupts_file, ("CPU",))) == []


def test_group_irqs_by_device_dedupes_and_sorts() -> None:
    grouped = irq.group_irqs_by_device([("eth0", 46), ("eth0", 45), ("eth0", 45), ("megasas0", 60)])
    assert grouped == {"eth0": [45, 46], "megasas0": [60]}


# --- topology discovery -----------------------------------------------------


def test_detect_cpu_count(interrupts_file: Path) -> None:
    assert irq.detect_cpu_count(interrupts_file) == 4


def test_read_numa_topology(tmp_path: Path) -> None:
    (tmp_path / "node0").mkdir()
    (tmp_path / "node1").mkdir()
    (tmp_path / "node0" / "cpulist").write_text("0-3\n")
    (tmp_path / "node1" / "cpulist").write_text("4-7\n")
    assert irq.read_numa_topology(tmp_path) == {0: [0, 1, 2, 3], 1: [4, 5, 6, 7]}


def test_read_numa_topology_missing_base(tmp_path: Path) -> None:
    assert irq.read_numa_topology(tmp_path / "absent") == {}


def test_device_numa_node(tmp_path: Path) -> None:
    dev = tmp_path / "eth0" / "device"
    dev.mkdir(parents=True)
    (dev / "numa_node").write_text("1\n")
    assert irq.device_numa_node("eth0", tmp_path) == 1


def test_device_numa_node_negative_is_none(tmp_path: Path) -> None:
    dev = tmp_path / "eth0" / "device"
    dev.mkdir(parents=True)
    (dev / "numa_node").write_text("-1\n")
    assert irq.device_numa_node("eth0", tmp_path) is None


def test_device_numa_node_missing_is_none(tmp_path: Path) -> None:
    assert irq.device_numa_node("megasas0", tmp_path) is None


# --- planning ---------------------------------------------------------------


def test_plan_assignments_numa_aware() -> None:
    grouped = {"eth0": [45, 46, 47]}
    topology = {0: [0, 1], 1: [2, 3]}
    device_nodes = {"eth0": 0}
    plan = irq.plan_assignments(grouped, topology, device_nodes, [0, 1, 2, 3], numa=True)
    # Three IRQs round-robin over node 0's CPUs [0, 1].
    assert plan == {45: [0], 46: [1], 47: [0]}


def test_plan_assignments_unknown_node_uses_all_cpus() -> None:
    grouped = {"megasas0": [60, 61]}
    topology = {0: [0, 1], 1: [2, 3]}
    device_nodes = {"megasas0": None}
    plan = irq.plan_assignments(grouped, topology, device_nodes, [0, 1, 2, 3], numa=True)
    assert plan == {60: [0], 61: [1]}


def test_plan_assignments_no_numa() -> None:
    grouped = {"eth0": [45, 46, 47]}
    topology = {0: [0, 1]}
    device_nodes = {"eth0": 0}
    plan = irq.plan_assignments(grouped, topology, device_nodes, [0, 1, 2, 3], numa=False)
    # NUMA disabled: spread over all CPUs.
    assert plan == {45: [0], 46: [1], 47: [2]}


# --- apply ------------------------------------------------------------------


def test_apply_dry_run_makes_no_writes(monkeypatch, capsys) -> None:
    monkeypatch.setattr(irq, "write_affinity_list", lambda *a: pytest.fail("no write"))
    monkeypatch.setattr(irq, "read_current_cpus", lambda _irq: set())
    monkeypatch.setattr(irq, "read_affinity_hint", lambda _irq: set())

    plan = {45: [0], 46: [1]}
    errors = irq.apply_assignments({"eth0": [45, 46]}, plan, dry_run=True)
    assert errors == 0
    assert "dry-run" in capsys.readouterr().out


def test_apply_skips_when_already_set(monkeypatch, capsys) -> None:
    monkeypatch.setattr(irq, "read_affinity_hint", lambda _irq: set())
    monkeypatch.setattr(irq, "read_current_cpus", lambda n: {0 if n == 45 else 1})
    monkeypatch.setattr(irq, "write_affinity_list", lambda *a: pytest.fail("no write"))

    plan = {45: [0], 46: [1]}
    errors = irq.apply_assignments({"eth0": [45, 46]}, plan, dry_run=False)
    assert errors == 0
    assert "already set" in capsys.readouterr().out


def test_apply_respects_driver_hint(monkeypatch, capsys) -> None:
    # Driver hint points elsewhere than our plan -> we must not override.
    monkeypatch.setattr(irq, "read_affinity_hint", lambda _irq: {2})
    monkeypatch.setattr(irq, "read_current_cpus", lambda _irq: set())
    monkeypatch.setattr(irq, "write_affinity_list", lambda *a: pytest.fail("no write"))

    errors = irq.apply_assignments({"eth0": [45]}, {45: [0]}, respect_hints=True)
    assert errors == 0
    assert "driver hint" in capsys.readouterr().out


def test_apply_ignore_hints_writes(monkeypatch, capsys) -> None:
    writes = []
    monkeypatch.setattr(irq, "read_affinity_hint", lambda _irq: {2})
    monkeypatch.setattr(irq, "read_current_cpus", lambda _irq: set())
    monkeypatch.setattr(irq, "write_affinity_list", lambda i, c: writes.append((i, c)))

    errors = irq.apply_assignments({"eth0": [45]}, {45: [0]}, respect_hints=False)
    assert errors == 0
    assert writes == [(45, [0])]


def test_apply_counts_os_errors(monkeypatch, capsys) -> None:
    monkeypatch.setattr(irq, "read_affinity_hint", lambda _irq: set())
    monkeypatch.setattr(irq, "read_current_cpus", lambda _irq: set())

    def fail_write(_irq, _cpus):
        raise PermissionError("denied")

    monkeypatch.setattr(irq, "write_affinity_list", fail_write)

    errors = irq.apply_assignments({"eth0": [45, 46]}, {45: [0], 46: [1]}, dry_run=False)
    assert errors == 2
    assert "FAIL" in capsys.readouterr().err


# --- CLI --------------------------------------------------------------------


def test_parse_args_defaults() -> None:
    args = irq.parse_args([])
    assert args.filter is None
    assert args.dry_run is False
    assert args.no_numa is False
    assert args.ignore_hints is False


def test_parse_args_flags() -> None:
    args = irq.parse_args(["-f", "eth", "-f", "nvme", "--dry-run", "--no-numa", "--ignore-hints"])
    assert args.filter == ["eth", "nvme"]
    assert args.dry_run is True
    assert args.no_numa is True
    assert args.ignore_hints is True
