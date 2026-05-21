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

- **NUMA-aware, per IRQ.** Each IRQ's NUMA node is read from
  `/proc/irq/<n>/node`, falling back to a PCI reverse-map
  (`/sys/bus/pci/devices/<addr>/msi_irqs/<n>` → that device's `numa_node`),
  so it works for any device — NICs, MegaRAID, NVMe, GPUs. Interrupt
  handling stays local to the cores that touch the data, no cross-socket
  bouncing for RDMA/NCCL or 10G+ traffic.
- **Respects managed IRQs.** Modern multi-queue NICs publish an
  `affinity_hint`; the tool honours it instead of fighting the kernel's
  managed-IRQ logic. Override with `--ignore-hints` when you really know
  better.
- **CPU-list writes.** Uses `smp_affinity_list` (CPU numbers), so it works
  unchanged on machines with more than 32 CPUs, where the hex-mask format
  is comma-grouped.
- **Optional RPS/XPS steering.** `--rps` keeps each NIC's queue softirq work
  on the device's NUMA node: RX (`rps_cpus`) gets the whole node mask so the
  kernel hashes flows across local cores, while each TX queue (`xps_cpus`)
  is pinned to one node core round-robin (canonical XPS, avoids lock
  contention).
- **Isolated-core aware.** CPUs listed in `isolcpus=`/`nohz_full=`
  (`/sys/devices/system/cpu/{isolated,nohz_full}`) are kept free of device
  IRQs by default — exactly what you want when those cores run a pinned RT
  or HFT thread. Use `--use-isolated` to opt out.
- **Auditable.** `--verify` is a read-only mode that prints an
  IRQ→CPU→node table and exits non-zero on any drift, so you can gate it in
  CI or monitoring.

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

# Also steer RX/TX queue RPS/XPS onto each NIC's NUMA node:
sudo python3 irq.py --rps

# Ignore the driver hint and force our layout onto managed IRQs:
sudo python3 irq.py --ignore-hints

# Plain round-robin over all CPUs (disable NUMA awareness):
sudo python3 irq.py --no-numa

# Audit only: print the IRQ->CPU->node table, exit non-zero on drift:
python3 irq.py --verify --no-color

# Permit pinning onto isolcpus/nohz_full cores (off by default):
sudo python3 irq.py --use-isolated

# Leave irqbalance running / drop colors for logs:
sudo python3 irq.py --keep-irqbalance --no-color
```

Run `python3 irq.py --help` for the full option list.

## How it works

1. Discover NUMA topology from `/sys/devices/system/node/node*/cpulist`.
2. Drop isolated cores (`isolcpus=`/`nohz_full=`) from the CPU pool unless
   `--use-isolated` is given.
3. Match `/proc/interrupts` lines against the filters and group IRQs by
   device.
4. Resolve each IRQ's NUMA node from `/proc/irq/<n>/node`, falling back to
   the owning PCI device's `numa_node` (via `msi_irqs`/`irq`), then to a
   network device's `numa_node`, then to all CPUs.
5. Plan one CPU per IRQ, round-robin within its NUMA node. With `--verify`,
   print the plan-vs-current table and stop here.
6. Stop `irqbalance` (`systemctl stop`, else `killall`) unless
   `--keep-irqbalance` is set.
7. For each IRQ: skip if a driver `affinity_hint` disagrees (unless
   `--ignore-hints`), skip if already correct, otherwise write the CPU to
   `smp_affinity_list`. Write failures are counted and produce a non-zero
   exit code.
8. With `--rps`, set every `queues/rx-*/rps_cpus` to the NIC's full
   NUMA-node mask, and pin each `queues/tx-*/xps_cpus` to one node core
   round-robin.

## Persisting across reboots

IRQ affinity resets on reboot, driver reload, and device hotplug. A sample
oneshot unit lives in [`systemd/irq-affinity.service`](./systemd/irq-affinity.service):

```bash
sudo install -D irq.py /opt/irq_affinity/irq.py
sudo cp systemd/irq-affinity.service /etc/systemd/system/
# edit ExecStart in the unit to match your filters/flags, then:
sudo systemctl daemon-reload
sudo systemctl enable --now irq-affinity.service
```

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
