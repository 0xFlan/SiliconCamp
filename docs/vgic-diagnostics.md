# vGIC diagnostics

How the emulated GIC reports what a guest is doing, why it no longer prints one
line per register access, and how to read its state after a hang when there is
no log to read.

**Status:** machine-checked offline (`scripts/check-vgic.py`,
`tests/test_check_vgic.py`). Not yet observed on the M4.

---

## The problem this replaced

`hv_vgic.c` used to build with `ENABLE_VGIC_LOGGING 1`, so every emulated
register access was a `printf()` — and m1n1's console is a UART. At 115200 baud
a 60-byte line costs about 5 ms, and a guest that programs the GIC performs tens
of thousands of accesses: Windows' HAL walks roughly 1020 SPIs at three
registers each, walks one redistributor frame per PE, and programs the
`GICR_*_BASER` window. Thirty thousand accesses is two and a half minutes of
nothing but output.

That is not a slow boot. It is a stall that is indistinguishable from the bug
the logging was added to find, and it would have been the first thing a real
operating system hit. The trace had to go, but deleting it would have left the
next hang completely silent, so it was replaced rather than removed.

---

## The design

Three mechanisms, each for a different question:

| Question | Mechanism | Cost per access |
|---|---|---|
| What did the guest do just before it stopped? | `vgic_diag.ring` (last 64 accesses) | a few stores |
| Did the guest ever touch *this* register? | `vgic_diag.dist_touched` / `redist_touched` bitmaps | one atomic OR |
| Something went wrong and I want to know now | `vgic_note()`, budgeted to 32 lines | compare and a counter |

`vgic_log()` still exists, for the detailed per-access text that a targeted
debugging session may want. It is compiled to nothing unless
`ENABLE_VGIC_LOGGING` is set, and `scripts/check-vgic.py` fails if any patch
sets it, because a build that boots an OS must not carry it.

`vgic_note()` is for events that are exceptional by nature — an unknown
register, a write to a read-only or RES0 field, a pending/active register the
emulation does not model. Those are genuine bug signals, so silence would be
wrong; but they are not trusted to be rare, because a guest that spins on one
produces them forever. The budget is why the same log line can be both useful
and safe: the first 32 print, the rest are counted in `vgic_diag.suppressed`.

Severity follows the message tag. Every site that said `[WARN]`, `[ERR]` or
`[ERROR]` is a `vgic_note()`; every site that said `[INFO]` is a `vgic_log()`.
The distinction was already in the source, it just was not acted on.

---

## Reading the state after a hang

The interesting case is a guest that stops without panicking: m1n1 is still
running at EL2, so the proxy still answers even though the guest has gone quiet.
`vgic_diag` is a global, so its address is a symbol.

```python
from m1n1.hv import HV
hv = HV(proxy)                          # existing session
base = hv.resolve_symbol("vgic_diag")

accesses   = proxy.read64(base + 0)     # every emulated access
unknown    = proxy.read64(base + 8)     # registers with no model
ro         = proxy.read64(base + 16)    # writes to read-only / RES0 fields
unmodelled = proxy.read64(base + 24)    # pending/active/LPI registers
warnings   = proxy.read64(base + 32)    # lines actually printed
suppressed = proxy.read64(base + 40)    # lines refused by the budget
# count and next are two u32 at 48 and 52
```

The ring starts at offset 56. Each entry is 24 bytes:

| Offset | Field | Meaning |
|---|---|---|
| +0 | `val` | the value read or written |
| +8 | `off` | offset within the space, and within the frame for a redistributor |
| +12 | `space` | `'D'` distributor, `'R'` redistributor, `'I'` ITS |
| +13 | `frame` | redistributor frame index (the vCPU), 0 elsewhere |
| +14 | `write` | 1 for a write |
| +15 | `handled` | 0 if the access hit the unknown-register path |
| +16 | `width` | access size in log2 bytes |

Walk it oldest-first from `next`:

```python
def trace(proxy, hv, n=16):
    base = hv.resolve_symbol("vgic_diag")
    count = proxy.readmem(base + 48, 4)
    next_slot = int.from_bytes(proxy.readmem(base + 52, 4), "little")
    start = (next_slot - count) % 64
    for i in range(count):
        e = proxy.readmem(base + 56 + ((start + i) % 64) * 24, 24)
        val = int.from_bytes(e[0:8], "little")
        off = int.from_bytes(e[8:12], "little")
        space, frame, write, handled, width = e[12], e[13], e[14], e[15], e[16]
        print(f"{chr(space)} frame={frame} off=0x{off:x} "
              f"{'W' if write else 'R'} {1 << width}B "
              f"{'handled' if handled else 'UNKNOWN'} 0x{val:x}")
```

The two bitmaps answer the question a ring cannot: *did this ever happen at
all?* They are one bit per 8 bytes of the space, so bit `off >> 3` of
`dist_touched` says whether the distributor offset `off` was ever accessed, and
`redist_touched` says the same for a redistributor register — **across all
frames**. `dist_touched` starts at 1592, `redist_touched` at 2616.

```python
def touched(proxy, hv, space, off):
    base = hv.resolve_symbol("vgic_diag")
    bitmap = base + (1592 if space == "D" else 2616)
    word = proxy.read64(bitmap + ((off >> 3) // 64) * 8)
    return bool(word & (1 << ((off >> 3) % 64)))

# Did any CPU ever write GICR_WAKER (offset 0x14)?  This is the direct
# observation of defect 1 in docs/status.md: before 0004, only frame 0 decoded.
print(touched(proxy, hv, "R", 0x14))
```

### Why these field offsets are quoted

They are ABI, not API: they depend on `vgic_trace_entry` staying 24 bytes and
`u64` staying 8-byte aligned, which holds for the AArch64 PCS this build uses.
They are recorded here so a reader does not have to open the header, and
`scripts/check-vgic.py` pins the *shape* (ring size, saturating count, atomic
bitmap) so a change that moves them also fails a check. Regenerate them with:

```bash
cc -o /tmp/off - <<'EOF'
#include <stdio.h>
#include <stdint.h>
#include <stddef.h>
typedef uint64_t u64; typedef uint32_t u32; typedef uint8_t u8;
#define GICR_FRAME_SIZE 0x20000
#define VGIC_RING_SIZE 64
typedef struct { u64 val; u32 off; u8 space, frame, write, handled, width; } vgic_trace_entry;
typedef struct {
    u64 accesses, unknown, ro_violation, unmodelled, warnings, suppressed;
    u32 count, next;
    vgic_trace_entry ring[VGIC_RING_SIZE];
    u64 dist_touched[0x10000 / 8 / 64];
    u64 redist_touched[GICR_FRAME_SIZE / 8 / 64];
} vgic_diag_t;
int main(void) {
    printf("size %zu ring %zu dist %zu redist %zu\n", sizeof(vgic_diag_t),
           offsetof(vgic_diag_t, ring), offsetof(vgic_diag_t, dist_touched),
           offsetof(vgic_diag_t, redist_touched));
    return 0;
}
EOF
/tmp/off
```

As of this build (m1n1.macho SHA-256 `222b6f9bf8e8…`) that prints
`size 4664 ring 56 dist 1592 redist 2616`.

---

## What each counter means for the bring-up

| Symptom | Reading |
|---|---|
| `accesses` stops growing | the guest stopped programming the GIC — the hang is elsewhere |
| `accesses` grows but nothing else does | the guest is spinning on a register that always answers the same way |
| `unknown` climbs | the guest expects a register the emulation does not have; the ring's `off` names it |
| `unmodelled` climbs | the guest is using pending/active state, which is not modelled; interrupts may be lost silently |
| `ro_violation` climbs | the guest is writing a field this emulation treats as fixed; either the guest is wrong or the emulation is |
| `suppressed` non-zero | the log was truncated — read the counters, not the log |
| `redist_touched` has no bit for frame 3's `GICR_WAKER` | that PE was never woken; see the frame decode in `docs/architecture.md` |

---

## What this is not

It is not a GIC conformance test. It records what the guest did, not whether
what the guest did was correct. The conformance question is answered by the
UEFI self-test described in `docs/next-steps.md`, which drives the contract
deliberately instead of waiting for a guest to exercise it by accident.
