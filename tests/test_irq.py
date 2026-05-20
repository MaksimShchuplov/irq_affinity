"""Unit tests for irq.py. No /proc access — all fixtures are synthetic."""

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


def test_detect_cpu_count(interrupts_file: Path) -> None:
    assert irq.detect_cpu_count(interrupts_file) == 4


def test_build_affinity_masks_small() -> None:
    assert irq.build_affinity_masks(4) == ["1", "2", "4", "8"]


def test_build_affinity_masks_large() -> None:
    masks = irq.build_affinity_masks(28)
    assert masks[0] == "1"
    assert masks[8] == "100"
    assert masks[-1] == "8000000"
    assert len(masks) == 28
    # all unique and strictly increasing as ints
    as_ints = [int(m, 16) for m in masks]
    assert as_ints == sorted(set(as_ints))


def test_build_affinity_masks_rejects_nonpositive() -> None:
    with pytest.raises(ValueError):
        irq.build_affinity_masks(0)


def test_parse_interrupts_default_filters(interrupts_file: Path) -> None:
    pairs = list(irq.parse_interrupts(interrupts_file, irq.DEFAULT_FILTERS))
    assert pairs == [
        ("eth0", 45),
        ("eth0", 46),
        ("eth1", 47),
        ("megasas0", 60),
        ("megasas0", 61),
    ]


def test_parse_interrupts_custom_filter(interrupts_file: Path) -> None:
    pairs = list(irq.parse_interrupts(interrupts_file, ("megasas",)))
    assert [device for device, _ in pairs] == ["megasas0", "megasas0"]


def test_parse_interrupts_skips_header(interrupts_file: Path) -> None:
    # The header mentions CPU names, but no IRQ number — must be skipped.
    pairs = list(irq.parse_interrupts(interrupts_file, ("CPU",)))
    assert pairs == []


def test_group_irqs_by_device_dedupes_and_sorts() -> None:
    grouped = irq.group_irqs_by_device(
        [("eth0", 46), ("eth0", 45), ("eth0", 45), ("megasas0", 60)]
    )
    assert grouped == {"eth0": [45, 46], "megasas0": [60]}


def test_apply_affinity_dry_run_makes_no_writes(monkeypatch, capsys) -> None:
    def boom(*_a, **_kw):
        raise AssertionError("write_affinity must not run in dry-run mode")

    monkeypatch.setattr(irq, "write_affinity", boom)
    monkeypatch.setattr(irq, "read_current_affinity", lambda _irq: "0")

    grouped = {"eth0": [45, 46]}
    errors = irq.apply_affinity(grouped, ["1", "2"], dry_run=True)
    assert errors == 0
    assert "dry-run" in capsys.readouterr().out


def test_apply_affinity_skips_when_already_set(monkeypatch, capsys) -> None:
    # IRQs [45, 46] round-robin over ["1", "2"] → 45→"1", 46→"2".
    expected = {45: "1", 46: "2"}
    monkeypatch.setattr(irq, "read_current_affinity", lambda n: expected[n])
    monkeypatch.setattr(
        irq, "write_affinity", lambda *a, **kw: pytest.fail("should not write")
    )

    errors = irq.apply_affinity({"eth0": [45, 46]}, ["1", "2"], dry_run=False)
    assert errors == 0
    assert "already set" in capsys.readouterr().out


def test_apply_affinity_counts_os_errors(monkeypatch, capsys) -> None:
    monkeypatch.setattr(irq, "read_current_affinity", lambda _irq: "0")

    def fail_write(_irq, _mask):
        raise PermissionError("denied")

    monkeypatch.setattr(irq, "write_affinity", fail_write)

    errors = irq.apply_affinity({"eth0": [45, 46]}, ["1", "2"], dry_run=False)
    assert errors == 2
    captured = capsys.readouterr()
    assert "FAIL" in captured.err


def test_parse_args_defaults() -> None:
    args = irq.parse_args([])
    assert args.filter is None
    assert args.dry_run is False
    assert args.keep_irqbalance is False


def test_parse_args_multiple_filters() -> None:
    args = irq.parse_args(["-f", "eth", "-f", "nvme", "--dry-run"])
    assert args.filter == ["eth", "nvme"]
    assert args.dry_run is True
