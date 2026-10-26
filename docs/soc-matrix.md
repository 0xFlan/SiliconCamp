# Apple Silicon coverage

The goal is Windows on every Apple Silicon Mac, not one machine. That makes
coverage the roadmap: which machines have firmware, which have none, and which
are missing only because nobody has written the tables.

`scripts/soc-coverage.py` answers it against the vendored firmware tree. The
machine list is transcribed from Asahi Linux's public device list; the package
inventory is read from `Silicon/Apple/*FamilyPkg` and `Platform/*Pkg`.

```bash
./scripts/soc-coverage.py          # report
./scripts/soc-coverage.py --json   # machine-readable
```

One Asahi row is deliberately different: Asahi lists the M3 Ultra Mac Studio as
`Mac15,4`, which is already the iMac M3 2-port. The Studio is `Mac15,14`
(Apple's model page), and `tests/test_soc_coverage.py` fails on any duplicated
model identifier.

## Current state (2026-10-26)

```
machines: 59
  SoC family has a silicon package: 33
  machine has a platform package: 1
  no silicon package at all: 26
```

The first number counts machines whose **SoC family** has a package. That is not
a machine port: it means the SoC half of a possible firmware exists. Only
`Mac16,3` has a platform package, and nothing in this repository has booted.

| Family | SoCs | Machines | Silicon package |
|---|---|---|---|
| T810X | T8103 | 5 | present |
| T811X | T8112 | 4 | present |
| T600X | T6000, T6001 | 5 | present |
| T602X | T6020, T6021, T6022 | 8 | present |
| T8122 | T8122 | 5 | present |
| T8132 | T8132 (M4) | 6 | present, **M4 package added here** |
| T604X | T6040, T6041 | 6 | **missing** |
| T603X | T6030, T6031, T6034 | 6 | **missing** |
| T6032 | T6032 | 1 | **missing** |
| T605X | T6050, T6051, T6052 | 7 | **missing** |
| T814X | T8142 | 3 | **missing** |
| T8152 | T8152 | 1 | **missing** |
| T8040 | T8040 | 1 | **missing** |
| — | T6002 (M1 Ultra) | 1 | **missing** |

Only `Mac16,3` (`Mac16_3Pkg`) has a platform package in this repository. NTASP's
other platform packages are product-named (`MacBookProEarly2023Pkg`,
`MacStudio2022Pkg`, ...) and predate the model-identifier convention, so the
report's "platform package by model identifier" list is intentionally short.

## How a family gets added

1. Collect values from a public source (device tree) **and** the machine's own
   `ioreg`/ADT; record both in `docs/hardware-values.md` or `docs/t604x.md`.
2. Add `Silicon/Apple/<family>FamilyPkg` with PCDs, memory map, and ACPI tables.
3. Add a platform package for the machine, or make the tables runtime-generated
   when the SoC has multiple SKUs with different core counts.
4. Build it, then `./scripts/check-acpi.py --print-topology` and add a
   validated `PLATFORMS` entry.
5. Re-run `./scripts/soc-coverage.py`; the gap should shrink.

`docs/t604x.md` records the T604X data that already exists and the four things
blocking it, including the SKU problem that a static table cannot solve.
