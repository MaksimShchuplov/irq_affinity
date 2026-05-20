# CLAUDE.md

Guidance for Claude Code (and other AI assistants) working in this repository.

## Project

`irq_affinity` is a small Linux sysadmin utility that pins hardware IRQs
(Intel 10G NICs, LSI MegaRAID controllers, and anything else you filter on)
to specific CPU cores by writing `/proc/irq/<n>/smp_affinity`. It is a
replacement for `irqbalance` on older systems where the daemon misbehaves.

- Entry point: `irq.py` (`main()` returns an exit code).
- Runtime: Python 3.8+ stdlib only. No third-party dependencies.
- Target OS: Linux (reads `/proc/interrupts`, writes `/proc/irq/*/smp_affinity`).
- Privileges: writes to `/proc/irq/*` require root. The script warns if not root.

## Layout

```
irq.py            # CLI entry point and library functions
tests/test_irq.py # pytest-based unit tests (use synthetic /proc/interrupts)
README.md         # user docs
LICENSE           # MIT
requirements-dev.txt
```

## How to run

```bash
python3 irq.py --dry-run                  # see what it would do
sudo python3 irq.py                       # apply affinities
sudo python3 irq.py -f eth -f nvme        # custom filters
sudo python3 irq.py --keep-irqbalance     # do not stop irqbalance
```

## How to test

```bash
python3 -m pytest -q
```

Tests never touch real `/proc/irq`. They feed fake interrupts text through
`parse_interrupts` and assert on the grouping / mask generation helpers.
If you add code that writes to `/proc/irq`, wrap it behind a dependency so
tests can stub it out (see how `read_current_affinity` / `write_affinity`
are isolated in small functions).

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

- Affinity masks are CPU-count dependent. Use `build_affinity_masks()`; do
  not hard-code the hex list (the original script had gaps and an
  out-of-order entry at CPU 26/27).
- `/proc/interrupts` varies across kernels. The parser skips the header
  line and requires a numeric IRQ prefix — add new filters, not new
  parsers, when possible.
- `killall irqbalance` is a last resort. Prefer `systemctl stop
  irqbalance` when available; only fall back to killall if systemd is
  absent.

## Git workflow for AI sessions

- Commit messages: imperative mood, subject ≤72 chars, body explains the
  "why".
- Do not open a pull request unless the user asks for one.
