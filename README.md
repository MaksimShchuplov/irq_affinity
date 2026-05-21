# irq_affinity

NUMA-aware IRQ affinity for Linux. Pins hardware IRQs (Intel 10G NICs,
LSI MegaRAID, and anything you filter on) to CPU cores by writing
`/proc/irq/<n>/smp_affinity_list`.

Originally a round-robin replacement for `irqbalance` on old systems, it
now keeps each device's IRQs on its own NUMA node and defers to the
driver/kernel affinity hint — which is exactly what latency- and
throughput-sensitive workloads want: **HFT**, **telecom/NFV**, and
**AI/ML clusters** where NUMA locality and network tuning (RDMA/NCCL)
decide the numbers.

## Why this instead of round-robin

- **NUMA-aware.** Each device's IRQs land on the CPUs of the device's own
  NUMA node, so interrupt handling stays local to the cores that touch
  the data — no cross-socket bouncing for RDMA/NCCL or 10G+ traffic.
- **Respects managed IRQs.** Modern multi-queue NICs publish an
  `affinity_hint`; the tool honours it instead of fighting the kernel's
  managed-IRQ logic. Override with `--ignore-hints` when you really know
  better.
- **CPU-list writes.** Uses `smp_affinity_list` (CPU numbers), so it works
  unchanged on machines with more than 32 CPUs, where the hex-mask format
  is comma-grouped.

## Requirements

- Linux with `/proc/interrupts`, `/proc/irq`, and `/sys/devices/system/node`
- Python 3.8 or newer (standard library only)
- Root privileges to write `/proc/irq/*/smp_affinity_list`

## Usage

```bash
# Preview the NUMA-aware plan without touching anything:
python3 irq.py --dry-run

# Apply for eth* and megasas* IRQs:
sudo python3 irq.py

# Custom filters (repeatable) — e.g. NVMe and Mellanox for an ML node:
sudo python3 irq.py -f mlx5 -f nvme

# Ignore the driver hint and force our layout onto managed IRQs:
sudo python3 irq.py --ignore-hints

# Plain round-robin over all CPUs (disable NUMA awareness):
sudo python3 irq.py --no-numa

# Leave irqbalance running / drop colors for logs:
sudo python3 irq.py --keep-irqbalance --no-color
```

Run `python3 irq.py --help` for the full option list.

## How it works

1. Discover NUMA topology from `/sys/devices/system/node/node*/cpulist`.
2. Match `/proc/interrupts` lines against the filters and group IRQs by
   device.
3. Resolve each device's NUMA node (network devices via
   `/sys/class/net/<dev>/device/numa_node`; others fall back to all CPUs).
4. Plan one CPU per IRQ, round-robin within the device's node.
5. Stop `irqbalance` (`systemctl stop`, else `killall`) unless
   `--keep-irqbalance` is set.
6. For each IRQ: skip if a driver `affinity_hint` disagrees (unless
   `--ignore-hints`), skip if already correct, otherwise write the CPU to
   `smp_affinity_list`. Write failures are counted and produce a non-zero
   exit code.

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python3 -m pytest -q
ruff check . && ruff format --check .
```

See [CLAUDE.md](./CLAUDE.md) for conventions and guidance aimed at AI
coding assistants.

## License

MIT — see [LICENSE](./LICENSE).
