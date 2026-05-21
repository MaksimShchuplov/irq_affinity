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
- **Optional RPS/XPS steering.** `--rps` points each NIC's RX/TX queue
  software steering (`rps_cpus` / `xps_cpus`) at the same NUMA node, so the
  receive/transmit softirq work stays local too.

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

# Leave irqbalance running / drop colors for logs:
sudo python3 irq.py --keep-irqbalance --no-color
```

Run `python3 irq.py --help` for the full option list.

## How it works

1. Discover NUMA topology from `/sys/devices/system/node/node*/cpulist`.
2. Match `/proc/interrupts` lines against the filters and group IRQs by
   device.
3. Resolve each IRQ's NUMA node from `/proc/irq/<n>/node`, falling back to
   the owning PCI device's `numa_node` (via `msi_irqs`/`irq`), then to a
   network device's `numa_node`, then to all CPUs.
4. Plan one CPU per IRQ, round-robin within its NUMA node.
5. Stop `irqbalance` (`systemctl stop`, else `killall`) unless
   `--keep-irqbalance` is set.
6. For each IRQ: skip if a driver `affinity_hint` disagrees (unless
   `--ignore-hints`), skip if already correct, otherwise write the CPU to
   `smp_affinity_list`. Write failures are counted and produce a non-zero
   exit code.
7. With `--rps`, write each NIC's NUMA-node CPU mask to every
   `queues/rx-*/rps_cpus` and `queues/tx-*/xps_cpus`.

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
