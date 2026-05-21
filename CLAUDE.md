# CLAUDE.md

Guidance for Claude Code (and other AI assistants) working in this repository.

## Project

`irq_affinity` is a small Linux sysadmin utility that pins hardware IRQs
(Intel 10G NICs, LSI MegaRAID controllers, and anything else you filter on)
to CPU cores by writing `/proc/irq/<n>/smp_affinity_list`. It is NUMA-aware
and respects the driver/kernel `affinity_hint`, which matters for
latency/throughput-sensitive workloads: HFT, telecom/NFV, and AI/ML
clusters (NUMA locality + RDMA/NCCL network tuning). It started life as a
round-robin replacement for `irqbalance` on old systems.

- Entry point: `irq.py` (`main()` returns an exit code).
- Runtime: Python 3.8+ stdlib only. No third-party dependencies.
- Target OS: Linux (`/proc/interrupts`, `/proc/irq`, `/sys/devices/system/node`).
- Privileges: writes to `/proc/irq/*` require root. The script warns if not root.

## Layout

```
irq.py                          # CLI entry point and library functions
tests/test_irq.py               # pytest unit tests (synthetic /proc, /sys via tmp_path)
systemd/irq-affinity.service    # sample oneshot unit for persistence
README.md                       # user docs
LICENSE                         # MIT
requirements-dev.txt
pyproject.toml                  # ruff + pytest config
```

## How to run

```bash
python3 irq.py --dry-run                  # show the NUMA-aware plan
sudo python3 irq.py                       # apply affinities
sudo python3 irq.py -f mlx5 -f nvme       # custom filters
sudo python3 irq.py --no-numa             # plain round-robin over all CPUs
sudo python3 irq.py --ignore-hints        # override managed-IRQ hints
sudo python3 irq.py --keep-irqbalance     # do not stop irqbalance
```

## How to test

```bash
python3 -m pytest -q
ruff check . && ruff format --check .
```

Tests never touch real `/proc` or `/sys`. Pure helpers (`parse_cpu_list`,
`format_cpu_list`, `normalize_affinity`, `mask_to_cpus`, `cpus_to_mask_str`,
`parse_interrupts`, `group_irqs_by_device`, `plan_assignments`,
`device_cpus`) are tested directly; topology/queue readers
(`read_numa_topology`, `irq_numa_node`, `device_numa_node`,
`list_queue_affinity_files`, `apply_rps`) take a base-path arg so they can be
pointed at a `tmp_path`; the I/O in `apply_assignments` is exercised by
monkeypatching `read_current_cpus` / `read_affinity_hint` /
`write_affinity_list`. Keep new sysfs/procfs access behind small functions
so this stays possible.

## Conventions

- Python 3, 4-space indent, f-strings, `pathlib.Path` for filesystem work.
- Keep the script dependency-free: no pip packages at runtime.
- No `os.system` / `shell=True`. Write directly to sysfs / procfs, or use
  `subprocess.run` with an argv list.
- Prefer returning exit codes from `main()` over calling `sys.exit` deep in
  the call graph.
- Do not silently swallow `OSError` on `/proc/irq` writes — report, count
  the failure, and exit non-zero.

## Things to be careful about

- Write CPU *numbers* to `smp_affinity_list`, not hex masks to
  `smp_affinity`. The hex mask is comma-grouped into 32-bit words above 32
  CPUs; `parse_cpu_list` / `format_cpu_list` avoid that class of bug.
  `normalize_affinity` exists only to read the hex `affinity_hint`.
- Default behaviour respects the driver `affinity_hint` (managed IRQs).
  Only override it behind `--ignore-hints`; silently fighting the kernel's
  managed-IRQ placement causes write failures (EIO) and worse locality.
- NUMA node resolution is best-effort and per-IRQ: prefer
  `/proc/irq/<n>/node` (any device), fall back to a NIC's
  `/sys/class/net/<dev>/device/numa_node`, then to all CPUs. Add lookups,
  don't assume a node.
- RPS/XPS (`--rps`) writes a hex mask (`cpus_to_mask_str`) to
  `queues/{rx-*/rps_cpus,tx-*/xps_cpus}` — these files take masks, not CPU
  lists, unlike `smp_affinity_list`. It only applies to network devices.
- `/proc/interrupts` varies across kernels. The parser skips the header
  line and requires a numeric IRQ prefix — add new filters, not new
  parsers, when possible.
- Stopping irqbalance: prefer `systemctl stop irqbalance`, fall back to
  `killall` only when systemd is absent.

## Git workflow for AI sessions

- Commit messages: imperative mood, subject ≤72 chars, body explains the
  "why".
- Do not open a pull request unless the user asks for one.
