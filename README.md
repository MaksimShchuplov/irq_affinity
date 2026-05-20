# irq_affinity

Pin hardware IRQs to CPU cores on Linux. Originally written for Intel 10G
NICs and LSI MegaRAID controllers on older systems where `irqbalance`
did a poor job.

The script scans `/proc/interrupts`, groups IRQs by device, and writes a
round-robin CPU affinity mask to each `/proc/irq/<n>/smp_affinity`.

## Requirements

- Linux with `/proc/interrupts` and `/proc/irq`
- Python 3.8 or newer (standard library only)
- Root privileges to write `/proc/irq/*/smp_affinity`

## Usage

```bash
# Preview changes without touching the kernel:
python3 irq.py --dry-run

# Apply default affinities for eth* and megasas* IRQs:
sudo python3 irq.py

# Custom filter list (repeatable):
sudo python3 irq.py -f eth -f nvme -f mlx5

# Leave irqbalance running:
sudo python3 irq.py --keep-irqbalance

# Disable ANSI colors (useful in logs):
sudo python3 irq.py --no-color
```

Run `python3 irq.py --help` for the full option list.

## How it works

1. Read the CPU count from the header of `/proc/interrupts`.
2. Build hex affinity masks (`1`, `2`, `4`, `8`, `10`, …) for each CPU.
3. For each line whose device name matches a filter, extract the IRQ
   number and group IRQs by device.
4. Stop `irqbalance` (unless `--keep-irqbalance` is set) so it doesn't
   fight with our assignments.
5. Walk the IRQs for each device and write `smp_affinity` round-robin
   across the CPU masks, skipping IRQs that already have the desired
   mask.

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python3 -m pytest -q
```

See [CLAUDE.md](./CLAUDE.md) for conventions and guidance aimed at AI
coding assistants.

## License

MIT — see [LICENSE](./LICENSE).
