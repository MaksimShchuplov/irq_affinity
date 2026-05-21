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


def test_cpus_to_mask_str() -> None:
    assert irq.cpus_to_mask_str([0, 1]) == "00000003"
    assert irq.cpus_to_mask_str([]) == "0"
    assert irq.cpus_to_mask_str([32]) == "00000001,00000000"


def test_cpus_to_mask_str_roundtrips_through_normalize() -> None:
    cpus = [0, 3, 33]
    assert irq.mask_to_cpus(irq.normalize_affinity(irq.cpus_to_mask_str(cpus))) == cpus


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


def test_read_isolated_cpus(tmp_path: Path) -> None:
    (tmp_path / "isolated").write_text("2-3\n")
    (tmp_path / "nohz_full").write_text("3,5\n")
    assert irq.read_isolated_cpus(tmp_path) == {2, 3, 5}


def test_read_isolated_cpus_empty_and_missing(tmp_path: Path) -> None:
    (tmp_path / "isolated").write_text("\n")
    assert irq.read_isolated_cpus(tmp_path) == set()
    assert irq.read_isolated_cpus(tmp_path / "absent") == set()


def test_read_isolated_cpus_ignores_garbage(tmp_path: Path) -> None:
    (tmp_path / "nohz_full").write_text("(null)\n")
    assert irq.read_isolated_cpus(tmp_path) == set()


def test_filter_isolated_drops_cores() -> None:
    topology = {0: [0, 1, 2, 3], 1: [4, 5, 6, 7]}
    filtered, all_cpus = irq.filter_isolated(topology, list(range(8)), {2, 3})
    assert filtered == {0: [0, 1], 1: [4, 5, 6, 7]}
    assert all_cpus == [0, 1, 4, 5, 6, 7]


def test_filter_isolated_keeps_node_when_all_isolated() -> None:
    topology = {0: [0, 1], 1: [2, 3]}
    filtered, all_cpus = irq.filter_isolated(topology, [0, 1, 2, 3], {0, 1})
    # Node 0 would be emptied; keep its CPUs rather than strand its IRQs.
    assert filtered == {0: [0, 1], 1: [2, 3]}
    assert all_cpus == [2, 3]


def test_filter_isolated_noop_without_isolated() -> None:
    topology = {0: [0, 1]}
    filtered, all_cpus = irq.filter_isolated(topology, [0, 1], set())
    assert filtered == topology
    assert all_cpus == [0, 1]


def test_irq_numa_node(tmp_path: Path) -> None:
    (tmp_path / "45").mkdir()
    (tmp_path / "45" / "node").write_text("1\n")
    assert irq.irq_numa_node(45, tmp_path) == 1


def test_irq_numa_node_negative_and_missing(tmp_path: Path) -> None:
    (tmp_path / "46").mkdir()
    (tmp_path / "46" / "node").write_text("-1\n")
    assert irq.irq_numa_node(46, tmp_path) is None
    assert irq.irq_numa_node(99, tmp_path) is None


def test_pci_irq_numa_node_msi(tmp_path: Path) -> None:
    dev = tmp_path / "0000:01:00.0"
    (dev / "msi_irqs" / "45").mkdir(parents=True)
    (dev / "numa_node").write_text("1\n")
    assert irq.pci_irq_numa_node(45, tmp_path) == 1


def test_pci_irq_numa_node_legacy_irq_file(tmp_path: Path) -> None:
    dev = tmp_path / "0000:02:00.0"
    dev.mkdir()
    (dev / "irq").write_text("60\n")
    (dev / "numa_node").write_text("0\n")
    assert irq.pci_irq_numa_node(60, tmp_path) == 0


def test_pci_irq_numa_node_negative_is_none(tmp_path: Path) -> None:
    dev = tmp_path / "0000:03:00.0"
    (dev / "msi_irqs" / "70").mkdir(parents=True)
    (dev / "numa_node").write_text("-1\n")
    assert irq.pci_irq_numa_node(70, tmp_path) is None


def test_pci_irq_numa_node_not_found(tmp_path: Path) -> None:
    dev = tmp_path / "0000:04:00.0"
    (dev / "msi_irqs" / "10").mkdir(parents=True)
    (dev / "numa_node").write_text("0\n")
    assert irq.pci_irq_numa_node(999, tmp_path) is None


def test_pci_irq_numa_node_missing_base(tmp_path: Path) -> None:
    assert irq.pci_irq_numa_node(45, tmp_path / "absent") is None


def test_device_numa_node(tmp_path: Path) -> None:
    dev = tmp_path / "eth0" / "device"
    dev.mkdir(parents=True)
    (dev / "numa_node").write_text("1\n")
    assert irq.device_numa_node("eth0", tmp_path) == 1


def test_device_numa_node_negative_and_missing(tmp_path: Path) -> None:
    dev = tmp_path / "eth0" / "device"
    dev.mkdir(parents=True)
    (dev / "numa_node").write_text("-1\n")
    assert irq.device_numa_node("eth0", tmp_path) is None
    assert irq.device_numa_node("megasas0", tmp_path) is None


# --- planning ---------------------------------------------------------------


def test_plan_assignments_numa_aware_per_irq() -> None:
    grouped = {"eth0": [45, 46, 47]}
    topology = {0: [0, 1], 1: [2, 3]}
    irq_nodes = {45: 0, 46: 0, 47: 0}
    plan = irq.plan_assignments(grouped, topology, irq_nodes, [0, 1, 2, 3], numa=True)
    # Three IRQs on node 0 round-robin over its CPUs [0, 1].
    assert plan == {45: [0], 46: [1], 47: [0]}


def test_plan_assignments_mixed_nodes() -> None:
    grouped = {"eth0": [45, 46, 47, 48]}
    topology = {0: [0, 1], 1: [2, 3]}
    irq_nodes = {45: 0, 46: 1, 47: 0, 48: 1}
    plan = irq.plan_assignments(grouped, topology, irq_nodes, [0, 1, 2, 3], numa=True)
    # node 0 bucket [45, 47] -> [0, 1]; node 1 bucket [46, 48] -> [2, 3].
    assert plan == {45: [0], 47: [1], 46: [2], 48: [3]}


def test_plan_assignments_unknown_node_uses_all_cpus() -> None:
    grouped = {"megasas0": [60, 61]}
    topology = {0: [0, 1], 1: [2, 3]}
    irq_nodes = {60: None, 61: None}
    plan = irq.plan_assignments(grouped, topology, irq_nodes, [0, 1, 2, 3], numa=True)
    assert plan == {60: [0], 61: [1]}


def test_plan_assignments_no_numa() -> None:
    grouped = {"eth0": [45, 46, 47]}
    topology = {0: [0, 1]}
    irq_nodes = {45: 0, 46: 0, 47: 0}
    plan = irq.plan_assignments(grouped, topology, irq_nodes, [0, 1, 2, 3], numa=False)
    assert plan == {45: [0], 46: [1], 47: [2]}


def test_device_cpus_prefers_known_node() -> None:
    topology = {0: [0, 1], 1: [2, 3]}
    assert irq.device_cpus([45, 46], topology, {45: None, 46: 1}, [0, 1, 2, 3]) == [2, 3]
    assert irq.device_cpus([60], topology, {60: None}, [0, 1, 2, 3]) == [0, 1, 2, 3]


# --- apply (IRQ affinity) ---------------------------------------------------


def test_apply_dry_run_makes_no_writes(monkeypatch, capsys) -> None:
    monkeypatch.setattr(irq, "write_affinity_list", lambda *a: pytest.fail("no write"))
    monkeypatch.setattr(irq, "read_current_cpus", lambda _irq: set())
    monkeypatch.setattr(irq, "read_affinity_hint", lambda _irq: set())

    errors = irq.apply_assignments({"eth0": [45, 46]}, {45: [0], 46: [1]}, dry_run=True)
    assert errors == 0
    assert "dry-run" in capsys.readouterr().out


def test_apply_skips_when_already_set(monkeypatch, capsys) -> None:
    monkeypatch.setattr(irq, "read_affinity_hint", lambda _irq: set())
    monkeypatch.setattr(irq, "read_current_cpus", lambda n: {0 if n == 45 else 1})
    monkeypatch.setattr(irq, "write_affinity_list", lambda *a: pytest.fail("no write"))

    errors = irq.apply_assignments({"eth0": [45, 46]}, {45: [0], 46: [1]}, dry_run=False)
    assert errors == 0
    assert "already set" in capsys.readouterr().out


def test_apply_respects_driver_hint(monkeypatch, capsys) -> None:
    monkeypatch.setattr(irq, "read_affinity_hint", lambda _irq: {2})
    monkeypatch.setattr(irq, "read_current_cpus", lambda _irq: set())
    monkeypatch.setattr(irq, "write_affinity_list", lambda *a: pytest.fail("no write"))

    errors = irq.apply_assignments({"eth0": [45]}, {45: [0]}, respect_hints=True)
    assert errors == 0
    assert "driver hint" in capsys.readouterr().out


def test_apply_ignore_hints_writes(monkeypatch) -> None:
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


# --- verify -----------------------------------------------------------------


def test_verify_reports_no_drift(monkeypatch, capsys) -> None:
    monkeypatch.setattr(irq, "read_current_cpus", lambda n: {0 if n == 45 else 1})
    monkeypatch.setattr(irq, "read_affinity_hint", lambda _irq: set())
    drift = irq.verify_assignments({"eth0": [45, 46]}, {45: [0], 46: [1]}, {45: 0, 46: 0})
    assert drift == 0
    out = capsys.readouterr().out
    assert "STATUS" in out
    assert "drift" not in out


def test_verify_counts_drift(monkeypatch, capsys) -> None:
    monkeypatch.setattr(irq, "read_current_cpus", lambda _irq: {3})
    monkeypatch.setattr(irq, "read_affinity_hint", lambda _irq: set())
    drift = irq.verify_assignments({"eth0": [45, 46]}, {45: [0], 46: [1]}, {45: 0, 46: 1})
    assert drift == 2
    assert "drift" in capsys.readouterr().out


def test_verify_handles_read_error(monkeypatch, capsys) -> None:
    def boom(_irq):
        raise OSError("no such irq")

    monkeypatch.setattr(irq, "read_current_cpus", boom)
    monkeypatch.setattr(irq, "read_affinity_hint", lambda _irq: set())
    drift = irq.verify_assignments({"eth0": [45]}, {45: [0]}, {45: None})
    assert drift == 1
    assert "error" in capsys.readouterr().out


# --- apply (RPS/XPS) --------------------------------------------------------


def _make_net_device(tmp_path: Path, name: str, rx: int, tx: int) -> Path:
    queues = tmp_path / name / "queues"
    for i in range(rx):
        d = queues / f"rx-{i}"
        d.mkdir(parents=True)
        (d / "rps_cpus").write_text("0")
    for i in range(tx):
        d = queues / f"tx-{i}"
        d.mkdir(parents=True)
        (d / "xps_cpus").write_text("0")
    return tmp_path


def test_list_queue_files_sorted_numerically(tmp_path: Path) -> None:
    base = _make_net_device(tmp_path, "eth0", rx=12, tx=1)
    rx = irq.list_queue_files("eth0", "rx", base)
    assert [p.parent.name for p in rx][:3] == ["rx-0", "rx-1", "rx-2"]
    # rx-10/rx-11 must sort after rx-9, not lexically after rx-1.
    assert rx[-1].parent.name == "rx-11"


def test_list_queue_files_non_network(tmp_path: Path) -> None:
    assert irq.list_queue_files("megasas0", "rx", tmp_path) == []


def test_apply_rps_hybrid_layout(tmp_path: Path) -> None:
    base = _make_net_device(tmp_path, "eth0", rx=2, tx=3)
    errors = irq.apply_rps("eth0", [4, 5], dry_run=False, sys_class_net=base)
    assert errors == 0
    q = base / "eth0" / "queues"
    # RX: whole node mask on every rx queue (cpus 4,5 -> bits -> 0x30).
    assert (q / "rx-0" / "rps_cpus").read_text() == "00000030"
    assert (q / "rx-1" / "rps_cpus").read_text() == "00000030"
    # TX: one node CPU per queue, round-robin (4, 5, 4).
    assert (q / "tx-0" / "xps_cpus").read_text() == "00000010"
    assert (q / "tx-1" / "xps_cpus").read_text() == "00000020"
    assert (q / "tx-2" / "xps_cpus").read_text() == "00000010"


def test_apply_rps_dry_run(tmp_path: Path) -> None:
    base = _make_net_device(tmp_path, "eth0", rx=1, tx=0)
    irq.apply_rps("eth0", [0, 1], dry_run=True, sys_class_net=base)
    # Unchanged in dry-run.
    assert (base / "eth0" / "queues" / "rx-0" / "rps_cpus").read_text() == "0"


def test_apply_rps_no_queues_is_noop(tmp_path: Path) -> None:
    assert irq.apply_rps("megasas0", [0, 1], sys_class_net=tmp_path) == 0


# --- CLI --------------------------------------------------------------------


def test_parse_args_defaults() -> None:
    args = irq.parse_args([])
    assert args.filter is None
    assert args.dry_run is False
    assert args.no_numa is False
    assert args.ignore_hints is False
    assert args.rps is False
    assert args.verify is False
    assert args.use_isolated is False


def test_parse_args_flags() -> None:
    args = irq.parse_args(
        [
            "-f",
            "eth",
            "-f",
            "nvme",
            "--dry-run",
            "--no-numa",
            "--ignore-hints",
            "--rps",
            "--verify",
            "--use-isolated",
        ]
    )
    assert args.filter == ["eth", "nvme"]
    assert args.dry_run is True
    assert args.no_numa is True
    assert args.ignore_hints is True
    assert args.rps is True
    assert args.verify is True
    assert args.use_isolated is True
