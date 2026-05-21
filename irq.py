#!/usr/bin/env python3
# -*- coding: UTF-8 -*-
"""Distribute hardware IRQs across CPU cores via /proc/irq/*/smp_affinity.

Originally targeted at Intel 10G NICs and LSI MegaRAID controllers on
older Linux systems where irqbalance did a poor job.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

__author__ = "Maksim Shchuplov shchuplov@gmail.com"
__version__ = "2.0.0"

DEFAULT_FILTERS = ("eth", "megasas")
PROC_INTERRUPTS = Path("/proc/interrupts")
PROC_IRQ = Path("/proc/irq")


class Colors:
    HEADER = "\033[95m"
    OKBLUE = "\033[94m"
    OKGREEN = "\033[92m"
    WARNING = "\033[93m"
    FAIL = "\033[91m"
    ENDC = "\033[0m"

    @classmethod
    def disable(cls):
        for name in ("HEADER", "OKBLUE", "OKGREEN", "WARNING", "FAIL", "ENDC"):
            setattr(cls, name, "")


def detect_cpu_count(interrupts_path: Path = PROC_INTERRUPTS) -> int:
    """Return the number of CPUs as reported by /proc/interrupts header."""
    with interrupts_path.open("r") as fh:
        header = fh.readline()
    return sum(1 for tok in header.split() if tok.startswith("CPU"))


def build_affinity_masks(cpu_count: int) -> list:
    """Return hex affinity mask strings for CPUs 0..cpu_count-1."""
    if cpu_count <= 0:
        raise ValueError("cpu_count must be positive")
    return [f"{1 << cpu:x}" for cpu in range(cpu_count)]


def parse_interrupts(interrupts_path: Path, filters):
    """Yield (device_name, irq_number) pairs for lines matching any filter."""
    pattern = re.compile(r"^\s*(\d+):")
    with interrupts_path.open("r") as fh:
        next(fh, None)  # skip CPU header
        for line in fh:
            if not any(f in line for f in filters):
                continue
            match = pattern.match(line)
            if not match:
                continue
            irq = int(match.group(1))
            tokens = line.split()
            device = tokens[-1].split("-", 1)[0]
            yield device, irq


def group_irqs_by_device(pairs):
    """Collapse (device, irq) pairs into {device: sorted-unique-irqs}."""
    grouped: dict = {}
    for device, irq in pairs:
        grouped.setdefault(device, [])
        if irq not in grouped[device]:
            grouped[device].append(irq)
    for irqs in grouped.values():
        irqs.sort()
    return grouped


def normalize_affinity(raw: str) -> int:
    """Parse an smp_affinity value into an int.

    The kernel groups the bitmask into comma-separated 32-bit words on
    machines with more than 32 CPUs (e.g. "00000001,00000000"); strip the
    commas before converting so the comparison works on any CPU count.
    """
    cleaned = raw.strip().replace(",", "")
    return int(cleaned, 16) if cleaned else 0


def read_current_affinity(irq: int) -> int:
    path = PROC_IRQ / str(irq) / "smp_affinity"
    return normalize_affinity(path.read_text())


def write_affinity(irq: int, mask: str) -> None:
    path = PROC_IRQ / str(irq) / "smp_affinity"
    with path.open("w") as fh:
        fh.write(mask)


def stop_irqbalance(dry_run: bool = False) -> None:
    """Best-effort stop of the irqbalance daemon."""
    if not shutil.which("killall"):
        return
    if dry_run:
        print(f"{Colors.WARNING}[dry-run]{Colors.ENDC} would stop irqbalance")
        return
    subprocess.run(
        ["killall", "irqbalance"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def apply_affinity(grouped, masks, dry_run: bool = False) -> int:
    """Assign each IRQ to a CPU, round-robin across the available masks."""
    errors = 0
    for device, irqs in grouped.items():
        print(f"{Colors.OKBLUE}{device}{Colors.ENDC}: {irqs}")
        for index, irq in enumerate(irqs):
            mask = masks[index % len(masks)]
            label = f"setting up irq {irq} to CPU core {index % len(masks)}"
            try:
                if read_current_affinity(irq) == int(mask, 16):
                    print(f"{label}................{Colors.OKGREEN}[already set]{Colors.ENDC}")
                    continue
                if dry_run:
                    print(
                        f"{label}................{Colors.WARNING}[dry-run mask={mask}]{Colors.ENDC}"
                    )
                    continue
                write_affinity(irq, mask)
                print(f"{label}................{Colors.OKGREEN}[OK!]{Colors.ENDC}")
            except OSError as exc:
                errors += 1
                print(
                    f"{label}................{Colors.FAIL}[FAIL: {exc}]{Colors.ENDC}",
                    file=sys.stderr,
                )
    return errors


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pin hardware IRQs to CPU cores via /proc/irq/*/smp_affinity.",
    )
    parser.add_argument(
        "-f",
        "--filter",
        action="append",
        default=None,
        help=(
            "Substring to match in /proc/interrupts lines. Repeatable. "
            f"Default: {', '.join(DEFAULT_FILTERS)}"
        ),
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Print planned changes without touching smp_affinity or irqbalance.",
    )
    parser.add_argument(
        "--keep-irqbalance",
        action="store_true",
        help="Do not attempt to stop the irqbalance daemon.",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI color output.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.no_color or not sys.stdout.isatty():
        Colors.disable()

    filters = tuple(args.filter) if args.filter else DEFAULT_FILTERS

    if not PROC_INTERRUPTS.exists():
        print(f"{PROC_INTERRUPTS} not found; this tool requires Linux.", file=sys.stderr)
        return 2

    cpu_count = detect_cpu_count()
    print(f"cpuused : {cpu_count}")
    masks = build_affinity_masks(cpu_count)

    grouped = group_irqs_by_device(parse_interrupts(PROC_INTERRUPTS, filters))
    if not grouped:
        print(f"No IRQs matched filters: {', '.join(filters)}")
        return 0

    if not args.keep_irqbalance:
        stop_irqbalance(dry_run=args.dry_run)

    if not args.dry_run and os.geteuid() != 0:
        print(
            f"{Colors.WARNING}warning: not running as root; writes to "
            f"/proc/irq may be denied.{Colors.ENDC}",
            file=sys.stderr,
        )

    errors = apply_affinity(grouped, masks, dry_run=args.dry_run)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
