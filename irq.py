#!/usr/bin/env python3
# -*- coding: UTF-8 -*-
"""NUMA-aware IRQ affinity for Linux.

Pin hardware IRQs (Intel 10G NICs, LSI MegaRAID, and anything you filter
on) to CPU cores by writing /proc/irq/<n>/smp_affinity_list. Unlike the
original round-robin script this:

  * keeps each IRQ on the CPUs of its own NUMA node, resolved per-IRQ from
    /proc/irq/<n>/node (works for any PCI device, not just NICs);
  * respects the driver/kernel affinity_hint (so it does not fight the
    managed-IRQ logic of modern multi-queue hardware) unless overridden;
  * writes CPU *numbers* via smp_affinity_list, sidestepping the hex-mask
    grouping the kernel uses on machines with more than 32 CPUs;
  * optionally steers per-queue RPS/XPS (software receive/transmit
    steering) onto the same NUMA node for network devices (--rps).
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

__author__ = "Maksim Shchuplov shchuplov@gmail.com"
__version__ = "3.2.0"

DEFAULT_FILTERS = ("eth", "megasas")
PROC_INTERRUPTS = Path("/proc/interrupts")
PROC_IRQ = Path("/proc/irq")
SYS_NODE = Path("/sys/devices/system/node")
SYS_CLASS_NET = Path("/sys/class/net")
SYS_PCI_DEVICES = Path("/sys/bus/pci/devices")
SYS_CPU = Path("/sys/devices/system/cpu")


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


def cpus_to_mask_str(cpus) -> str:
    """Render CPU ids as a kernel hex mask, comma-grouped into 32-bit words.

    Inverse of normalize_affinity; used for rps_cpus / xps_cpus, which take
    a hex mask rather than a CPU list. Example: [0, 1] -> "00000003",
    [32] -> "00000001,00000000".
    """
    mask = 0
    for cpu in cpus:
        mask |= 1 << cpu
    if mask == 0:
        return "0"
    words = []
    while mask:
        words.append(f"{mask & 0xFFFFFFFF:08x}")
        mask >>= 32
    return ",".join(reversed(words))


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


def read_isolated_cpus(cpu_base: Path = SYS_CPU) -> set:
    """CPUs isolated via isolcpus= / nohz_full= boot params.

    Reads /sys/devices/system/cpu/{isolated,nohz_full}. These cores are kept
    free of device IRQs for latency-sensitive workloads (HFT, NFV, RT).
    """
    isolated: set = set()
    for name in ("isolated", "nohz_full"):
        try:
            raw = (cpu_base / name).read_text().strip()
        except OSError:
            continue
        try:
            isolated.update(parse_cpu_list(raw))
        except ValueError:
            continue
    return isolated


def filter_isolated(topology: dict, all_cpus, isolated):
    """Drop isolated CPUs from the topology and CPU pool.

    If filtering would leave a node (or the whole pool) empty, the original
    set is kept so IRQs are never stranded with nowhere to go.
    """
    if not isolated:
        return topology, list(all_cpus)
    filtered = {}
    for node, cpus in topology.items():
        kept = [c for c in cpus if c not in isolated]
        filtered[node] = kept if kept else cpus
    kept_all = [c for c in all_cpus if c not in isolated]
    return filtered, (kept_all if kept_all else list(all_cpus))


def irq_numa_node(irq: int, proc_irq: Path = PROC_IRQ):
    """NUMA node of an IRQ from /proc/irq/<n>/node, or None if unknown.

    This is the canonical per-IRQ source and works for any device, so it is
    preferred over name-based sysfs lookups.
    """
    try:
        node = int((proc_irq / str(irq) / "node").read_text())
    except (OSError, ValueError):
        return None
    return node if node >= 0 else None


def pci_irq_numa_node(irq: int, pci_devices: Path = SYS_PCI_DEVICES):
    """NUMA node of the PCI device that owns ``irq``, or None.

    Reverse-maps the IRQ to its owning device via
    /sys/bus/pci/devices/<addr>/msi_irqs/<n> (MSI/MSI-X) or the legacy
    <addr>/irq file, then reads that device's numa_node. Works for any PCI
    device (megasas, NVMe, GPUs) when /proc/irq/<n>/node is unavailable.
    """
    if not pci_devices.exists():
        return None
    irq_s = str(irq)
    for dev in sorted(pci_devices.iterdir()):
        owns = (dev / "msi_irqs" / irq_s).exists()
        if not owns:
            try:
                owns = (dev / "irq").read_text().strip() == irq_s
            except OSError:
                owns = False
        if not owns:
            continue
        try:
            node = int((dev / "numa_node").read_text())
        except (OSError, ValueError):
            return None
        return node if node >= 0 else None
    return None


def device_numa_node(device: str, sys_class_net: Path = SYS_CLASS_NET):
    """Best-effort NUMA node for a network device, or None if unknown.

    Used as a fallback when neither /proc/irq/<n>/node nor the PCI
    reverse-map yields a node.
    """
    try:
        node = int((sys_class_net / device / "device" / "numa_node").read_text())
    except (OSError, ValueError):
        return None
    return node if node >= 0 else None


# --- planning (pure) --------------------------------------------------------


def plan_assignments(grouped, topology, irq_nodes, all_cpus, numa=True) -> dict:
    """Return {irq: [cpu]} mapping each IRQ to one CPU.

    With ``numa`` enabled, each IRQ is spread round-robin only over the CPUs
    of its own NUMA node (grouped per device so a device's IRQs fan out);
    IRQs whose node is unknown, and everything when ``numa`` is off, spread
    over every CPU.
    """
    plan: dict = {}
    for irqs in grouped.values():
        buckets: dict = {}
        for irq in sorted(irqs):
            node = irq_nodes.get(irq) if numa else None
            buckets.setdefault(node, []).append(irq)
        for node, node_irqs in buckets.items():
            cpus = topology.get(node) if node is not None else None
            if not cpus:
                cpus = all_cpus
            for index, irq in enumerate(node_irqs):
                plan[irq] = [cpus[index % len(cpus)]]
    return plan


def device_cpus(irqs, topology, irq_nodes, all_cpus) -> list:
    """CPUs of the NUMA node a device's IRQs sit on, else all CPUs."""
    for irq in sorted(irqs):
        node = irq_nodes.get(irq)
        if node is not None and topology.get(node):
            return topology[node]
    return all_cpus


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


def _queue_index(path: Path) -> int:
    match = re.search(r"-(\d+)$", path.parent.name)
    return int(match.group(1)) if match else 0


def list_queue_files(device: str, kind: str, sys_class_net: Path = SYS_CLASS_NET) -> list:
    """Return a network device's queue steering files, sorted by queue index.

    ``kind`` is "rx" (rps_cpus) or "tx" (xps_cpus).
    """
    base = sys_class_net / device / "queues"
    if not base.exists():
        return []
    prefix, fname = ("rx-", "rps_cpus") if kind == "rx" else ("tx-", "xps_cpus")
    files = [q / fname for q in base.glob(prefix + "*") if (q / fname).exists()]
    return sorted(files, key=_queue_index)


def _write_queue_mask(path: Path, cpus, dry_run: bool) -> int:
    label = f"  {path.parent.parent.parent.name} {path.parent.name}/{path.name} -> cpu {format_cpu_list(cpus)}"
    try:
        if dry_run:
            print(f"{label}................{Colors.WARNING}[dry-run]{Colors.ENDC}")
            return 0
        path.write_text(cpus_to_mask_str(cpus))
        print(f"{label}................{Colors.OKGREEN}[OK!]{Colors.ENDC}")
        return 0
    except OSError as exc:
        print(f"{label}................{Colors.FAIL}[FAIL: {exc}]{Colors.ENDC}", file=sys.stderr)
        return 1


def apply_rps(device: str, cpus, dry_run=False, sys_class_net: Path = SYS_CLASS_NET) -> int:
    """Steer a network device's RX/TX queues onto ``cpus``. Returns failures.

    RX (rps_cpus) gets the whole NUMA-node mask so the kernel hashes flows
    across the local cores; TX (xps_cpus) pins each queue to a single node
    core round-robin, the canonical XPS layout that avoids lock contention.
    """
    if not cpus:
        return 0
    errors = 0
    for path in list_queue_files(device, "rx", sys_class_net):
        errors += _write_queue_mask(path, cpus, dry_run)
    for index, path in enumerate(list_queue_files(device, "tx", sys_class_net)):
        errors += _write_queue_mask(path, [cpus[index % len(cpus)]], dry_run)
    return errors


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


def verify_assignments(grouped, plan, irq_nodes) -> int:
    """Print a read-only IRQ -> CPU -> node audit table. Returns drift count.

    "Drift" is any IRQ whose current affinity differs from the plan (or that
    cannot be read), so the result is suitable as a CI / monitoring gate.
    """
    print(
        f"{'DEVICE':<14} {'IRQ':>5} {'NODE':>4} {'CURRENT':>12} {'PLANNED':>12} {'HINT':>8}  STATUS"
    )
    drift = 0
    for device, irqs in grouped.items():
        for irq in sorted(irqs):
            planned = plan[irq]
            node = irq_nodes.get(irq)
            node_s = str(node) if node is not None else "-"
            try:
                current = read_current_cpus(irq)
                current_s = format_cpu_list(current) or "-"
                ok = current == set(planned)
            except OSError:
                current_s = "error"
                ok = False
            hint = read_affinity_hint(irq)
            hint_s = format_cpu_list(hint) if hint else "-"
            status = (
                f"{Colors.OKGREEN}ok{Colors.ENDC}" if ok else f"{Colors.WARNING}drift{Colors.ENDC}"
            )
            if not ok:
                drift += 1
            print(
                f"{device:<14} {irq:>5} {node_s:>4} "
                f"{current_s:>12} {format_cpu_list(planned):>12} {hint_s:>8}  {status}"
            )
    return drift


def apply_assignments(grouped, plan, respect_hints=True, dry_run=False) -> int:
    """Write the planned IRQ affinities. Returns the number of failures."""
    errors = 0
    for device, irqs in grouped.items():
        print(f"{Colors.OKBLUE}{device}{Colors.ENDC}: {irqs}")
        for irq in sorted(irqs):
            cpus = plan[irq]
            label = f"irq {irq} -> cpu {format_cpu_list(cpus)}"
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
        "--verify",
        action="store_true",
        help="Read-only audit: print an IRQ->CPU->node table and exit "
        "non-zero if any affinity has drifted from the plan.",
    )
    parser.add_argument(
        "--use-isolated",
        action="store_true",
        help="Allow pinning to isolcpus/nohz_full cores (skipped by default).",
    )
    parser.add_argument(
        "--no-numa",
        action="store_true",
        help="Spread IRQs over all CPUs instead of each IRQ's NUMA node.",
    )
    parser.add_argument(
        "--ignore-hints",
        action="store_true",
        help="Override the driver/kernel affinity_hint (managed IRQs).",
    )
    parser.add_argument(
        "--rps",
        action="store_true",
        help="Also steer RX/TX queue RPS/XPS onto each NIC's NUMA node.",
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

    isolated = set() if args.use_isolated else read_isolated_cpus()
    topology, all_cpus = filter_isolated(topology, all_cpus, isolated)

    irq_nodes: dict = {}
    for device, irqs in grouped.items():
        net_fallback = device_numa_node(device)
        for irq in irqs:
            node = irq_numa_node(irq)
            if node is None:
                node = pci_irq_numa_node(irq)
            if node is None:
                node = net_fallback
            irq_nodes[irq] = node

    numa = not args.no_numa and bool(topology)
    print(f"cpus: {len(all_cpus)}  numa nodes: {len(topology)}  numa-aware: {numa}")
    if isolated:
        print(f"isolated cpus skipped: {format_cpu_list(isolated)}")

    plan = plan_assignments(grouped, topology, irq_nodes, all_cpus, numa=numa)

    if args.verify:
        return 1 if verify_assignments(grouped, plan, irq_nodes) else 0

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

    if args.rps:
        print("RPS/XPS queue steering:")
        for device, irqs in grouped.items():
            if not (SYS_CLASS_NET / device).exists():
                continue
            cpus = device_cpus(irqs, topology, irq_nodes, all_cpus) if numa else all_cpus
            errors += apply_rps(device, cpus, dry_run=args.dry_run)

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
