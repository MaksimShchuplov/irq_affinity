#!/usr/bin/env python3
# -*- coding: UTF-8 -*-
"""NUMA-aware IRQ affinity for Linux.

Pin hardware IRQs (Intel 10G NICs, LSI MegaRAID, and anything you filter
on) to CPU cores by writing /proc/irq/<n>/smp_affinity_list. Unlike the
original round-robin script this:

  * keeps each device's IRQs on the CPUs of the device's own NUMA node;
  * respects the driver/kernel affinity_hint (so it does not fight the
    managed-IRQ logic of modern multi-queue hardware) unless overridden;
  * writes CPU *numbers* via smp_affinity_list, sidestepping the hex-mask
    grouping the kernel uses on machines with more than 32 CPUs.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

__author__ = "Maksim Shchuplov shchuplov@gmail.com"
__version__ = "3.0.0"

DEFAULT_FILTERS = ("eth", "megasas")
PROC_INTERRUPTS = Path("/proc/interrupts")
PROC_IRQ = Path("/proc/irq")
SYS_NODE = Path("/sys/devices/system/node")
SYS_CLASS_NET = Path("/sys/class/net")


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


# --- parsing helpers (pure) -------------------------------------------------


def parse_cpu_list(spec: str) -> list:
    """Parse a kernel CPU list such as "0-3,5,7-8" into sorted unique ints."""
    cpus = set()
    for part in spec.strip().split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            cpus.update(range(int(lo), int(hi) + 1))
        else:
            cpus.add(int(part))
    return sorted(cpus)


def format_cpu_list(cpus) -> str:
    """Render a set of CPU ids as a compact kernel list, e.g. [0,1,2,4] -> "0-2,4"."""
    cpus = sorted(set(cpus))
    if not cpus:
        return ""
    ranges = []
    start = prev = cpus[0]
    for cpu in cpus[1:]:
        if cpu == prev + 1:
            prev = cpu
        else:
            ranges.append((start, prev))
            start = prev = cpu
    ranges.append((start, prev))
    return ",".join(str(a) if a == b else f"{a}-{b}" for a, b in ranges)


def normalize_affinity(raw: str) -> int:
    """Parse an smp_affinity / affinity_hint hex mask into an int.

    The kernel groups the bitmask into comma-separated 32-bit words on
    machines with more than 32 CPUs (e.g. "00000001,00000000"); strip the
    commas before converting so the value is correct on any CPU count.
    """
    cleaned = raw.strip().replace(",", "")
    return int(cleaned, 16) if cleaned else 0


def mask_to_cpus(mask: int) -> list:
    """Expand an affinity bitmask into the list of CPU ids it selects."""
    return [bit for bit in range(mask.bit_length()) if mask >> bit & 1]


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
            device = line.split()[-1].split("-", 1)[0]
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


# --- topology discovery -----------------------------------------------------


def detect_cpu_count(interrupts_path: Path = PROC_INTERRUPTS) -> int:
    """Return the number of CPUs as reported by the /proc/interrupts header."""
    with interrupts_path.open("r") as fh:
        header = fh.readline()
    return sum(1 for tok in header.split() if tok.startswith("CPU"))


def read_numa_topology(node_base: Path = SYS_NODE) -> dict:
    """Return {numa_node_id: [cpu, ...]} from /sys/devices/system/node."""
    topology: dict = {}
    if not node_base.exists():
        return topology
    for node_dir in sorted(node_base.glob("node[0-9]*")):
        match = re.fullmatch(r"node(\d+)", node_dir.name)
        if not match:
            continue
        try:
            cpus = parse_cpu_list((node_dir / "cpulist").read_text())
        except OSError:
            continue
        if cpus:
            topology[int(match.group(1))] = cpus
    return topology


def device_numa_node(device: str, sys_class_net: Path = SYS_CLASS_NET):
    """Best-effort NUMA node for a network device, or None if unknown.

    Only network devices expose this cleanly via sysfs; controllers like
    megasas are not name-mappable here and fall back to None (all CPUs).
    """
    try:
        node = int((sys_class_net / device / "device" / "numa_node").read_text())
    except (OSError, ValueError):
        return None
    return node if node >= 0 else None


# --- planning (pure) --------------------------------------------------------


def plan_assignments(grouped, topology, device_nodes, all_cpus, numa=True) -> dict:
    """Return {irq: [cpu]} mapping each IRQ to one CPU.

    With ``numa`` enabled, a device's IRQs are spread round-robin only over
    the CPUs of the device's NUMA node (when known); otherwise they spread
    over every CPU.
    """
    plan: dict = {}
    for device, irqs in grouped.items():
        cpus = all_cpus
        if numa:
            node = device_nodes.get(device)
            if node is not None and topology.get(node):
                cpus = topology[node]
        if not cpus:
            cpus = all_cpus
        for index, irq in enumerate(sorted(irqs)):
            plan[irq] = [cpus[index % len(cpus)]]
    return plan


# --- I/O --------------------------------------------------------------------


def read_current_cpus(irq: int) -> set:
    path = PROC_IRQ / str(irq) / "smp_affinity_list"
    return set(parse_cpu_list(path.read_text()))


def read_affinity_hint(irq: int) -> set:
    """Return the CPUs the driver suggests for this IRQ, or an empty set."""
    try:
        raw = (PROC_IRQ / str(irq) / "affinity_hint").read_text()
    except OSError:
        return set()
    return set(mask_to_cpus(normalize_affinity(raw)))


def write_affinity_list(irq: int, cpus) -> None:
    path = PROC_IRQ / str(irq) / "smp_affinity_list"
    path.write_text(format_cpu_list(cpus))


def stop_irqbalance(dry_run: bool = False) -> None:
    """Best-effort stop of the irqbalance daemon."""
    if dry_run:
        print(f"{Colors.WARNING}[dry-run]{Colors.ENDC} would stop irqbalance")
        return
    if shutil.which("systemctl"):
        result = subprocess.run(
            ["systemctl", "stop", "irqbalance"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode == 0:
            return
    if shutil.which("killall"):
        subprocess.run(
            ["killall", "irqbalance"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def apply_assignments(grouped, plan, respect_hints=True, dry_run=False) -> int:
    """Write the planned affinities. Returns the number of failures."""
    errors = 0
    for device, irqs in grouped.items():
        print(f"{Colors.OKBLUE}{device}{Colors.ENDC}: {irqs}")
        for irq in sorted(irqs):
            cpus = plan[irq]
            target = format_cpu_list(cpus)
            label = f"irq {irq} -> cpu {target}"
            try:
                if respect_hints:
                    hint = read_affinity_hint(irq)
                    if hint and hint != set(cpus):
                        print(
                            f"{label}................"
                            f"{Colors.WARNING}[skip: driver hint cpu "
                            f"{format_cpu_list(hint)}]{Colors.ENDC}"
                        )
                        continue
                if read_current_cpus(irq) == set(cpus):
                    print(f"{label}................{Colors.OKGREEN}[already set]{Colors.ENDC}")
                    continue
                if dry_run:
                    print(f"{label}................{Colors.WARNING}[dry-run]{Colors.ENDC}")
                    continue
                write_affinity_list(irq, cpus)
                print(f"{label}................{Colors.OKGREEN}[OK!]{Colors.ENDC}")
            except OSError as exc:
                errors += 1
                print(
                    f"{label}................{Colors.FAIL}[FAIL: {exc}]{Colors.ENDC}",
                    file=sys.stderr,
                )
    return errors


# --- CLI --------------------------------------------------------------------


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="NUMA-aware IRQ affinity via /proc/irq/*/smp_affinity_list.",
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
        help="Print planned changes without touching the kernel or irqbalance.",
    )
    parser.add_argument(
        "--no-numa",
        action="store_true",
        help="Spread IRQs over all CPUs instead of the device's NUMA node.",
    )
    parser.add_argument(
        "--ignore-hints",
        action="store_true",
        help="Override the driver/kernel affinity_hint (managed IRQs).",
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

    grouped = group_irqs_by_device(parse_interrupts(PROC_INTERRUPTS, filters))
    if not grouped:
        print(f"No IRQs matched filters: {', '.join(filters)}")
        return 0

    topology = read_numa_topology()
    all_cpus = sorted({cpu for cpus in topology.values() for cpu in cpus})
    if not all_cpus:
        all_cpus = list(range(detect_cpu_count()))
    device_nodes = {device: device_numa_node(device) for device in grouped}

    numa = not args.no_numa and bool(topology)
    print(f"cpus: {len(all_cpus)}  numa nodes: {len(topology)}  numa-aware: {numa}")

    plan = plan_assignments(grouped, topology, device_nodes, all_cpus, numa=numa)

    if not args.keep_irqbalance:
        stop_irqbalance(dry_run=args.dry_run)

    if not args.dry_run and os.geteuid() != 0:
        print(
            f"{Colors.WARNING}warning: not running as root; writes to "
            f"/proc/irq may be denied.{Colors.ENDC}",
            file=sys.stderr,
        )

    errors = apply_assignments(
        grouped,
        plan,
        respect_hints=not args.ignore_hints,
        dry_run=args.dry_run,
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
