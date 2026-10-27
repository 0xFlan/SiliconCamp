#!/usr/bin/env python3
"""Check the ANS/ANS2 driver's hardware constants against m1n1.

The driver in
`third_party/ntasp-firmware/Silicon/Apple/AppleSiliconPkg/Drivers/AppleNANDStorageDxe`
is a port: every register offset, bit position and opcode in it was read out of
m1n1's `nvme.c`, `asc.c`, `sart.c` and `rtkit.c`.  A port drifts -- someone bumps
the m1n1 submodule, or fixes one side while debugging and forgets the other --
and the failure mode is a driver that writes plausible values to the wrong
registers, which a build and a boot cannot detect.

This checker extracts the macros from both sides, evaluates the C expressions
(or the function-like macros with `idx` bound to 0) and reports every constant
where they disagree.  It is the same idea as `check-acpi.py`: agreement between
two descriptions of the same hardware is a property a machine can verify.

Usage:
    ./scripts/check-ans2-constants.py [--m1n1 DIR] [--driver DIR]

Exit codes: 0 all constants agree (or the m1n1 tree is not checked out),
1 at least one disagreement.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_M1N1 = ROOT / "third_party" / "m1n1-windows" / "src"
DEFAULT_DRIVER = (
    ROOT
    / "third_party"
    / "ntasp-firmware"
    / "Silicon"
    / "Apple"
    / "AppleSiliconPkg"
    / "Drivers"
    / "AppleNANDStorageDxe"
    / "AppleNandStorage.h"
)

DEFINE_RE = re.compile(
    r"^#define\s+([A-Za-z_][A-Za-z0-9_]*)"  # name
    r"(?:\(([^)]*)\))?"  # optional parameters
    r"\s+(.+?)\s*$"
)

COMMENT_RE = re.compile(r"/\*.*?\*/|//.*$")
INT_SUFFIX_RE = re.compile(r"\b(0[xX][0-9a-fA-F]+|\d+)[uUlL]{1,3}\b")


class ExpressionError(Exception):
    """An expression used a construct this checker cannot evaluate."""


def strip_comments(line: str) -> str:
    return COMMENT_RE.sub("", line)


def parse_defines(path: Path) -> dict[str, tuple[list[str], str]]:
    """Map a macro name to (parameters, expression text)."""
    defines: dict[str, tuple[list[str], str]] = {}
    if not path.is_file():
        return defines

    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = strip_comments(raw).rstrip()
        match = DEFINE_RE.match(line)
        if not match:
            continue
        name, params, body = match.groups()
        parameters = [] if params is None else [p.strip() for p in params.split(",")]
        defines[name] = (parameters, INT_SUFFIX_RE.sub(r"\1", body.strip()))

    return defines


EDK2_BITS = {f"BIT{i}": 1 << i for i in range(64)}


def evaluate(expression: str, environment: dict[str, int]) -> int:
    """Evaluate the integer subset of C used by these headers."""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as error:
        raise ExpressionError(f"cannot parse {expression!r}") from error

    def walk(node: ast.AST) -> int:
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, int):
                return node.value
            raise ExpressionError(f"non-integer constant in {expression!r}")
        if isinstance(node, ast.Name):
            if node.id in environment:
                return environment[node.id]
            raise ExpressionError(f"unknown symbol {node.id!r}")
        if isinstance(node, ast.UnaryOp):
            value = walk(node.operand)
            if isinstance(node.op, ast.Invert):
                return ~value
            if isinstance(node.op, ast.USub):
                return -value
            if isinstance(node.op, ast.UAdd):
                return value
            raise ExpressionError(f"unsupported unary operator in {expression!r}")
        if isinstance(node, ast.BinOp):
            left = walk(node.left)
            right = walk(node.right)
            operations = {
                ast.Add: lambda a, b: a + b,
                ast.Sub: lambda a, b: a - b,
                ast.Mult: lambda a, b: a * b,
                ast.LShift: lambda a, b: a << b,
                ast.RShift: lambda a, b: a >> b,
                ast.BitOr: lambda a, b: a | b,
                ast.BitAnd: lambda a, b: a & b,
                ast.BitXor: lambda a, b: a ^ b,
            }
            for node_type, operation in operations.items():
                if isinstance(node.op, node_type):
                    return operation(left, right)
            raise ExpressionError(f"unsupported operator in {expression!r}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            arguments = [walk(argument) for argument in node.args]
            if node.func.id == "BIT" and len(arguments) == 1:
                return 1 << arguments[0]
            if node.func.id == "GENMASK" and len(arguments) == 2:
                high, low = arguments
                return ((1 << (high - low + 1)) - 1) << low
            if node.func.id in {"LShiftU64", "RShiftU64"} and len(arguments) == 2:
                if node.func.id == "LShiftU64":
                    return arguments[0] << arguments[1]
                return arguments[0] >> arguments[1]
            raise ExpressionError(f"unsupported call {node.func.id}() in {expression!r}")
        raise ExpressionError(f"unsupported expression {expression!r}")

    return walk(tree)


def mask_bounds(mask: int) -> tuple[int, int]:
    """Return (lowest set bit, highest set bit) of a contiguous mask."""
    if mask <= 0:
        raise ExpressionError("mask must be positive")
    low = (mask & -mask).bit_length() - 1
    return low, mask.bit_length() - 1


class Checker:
    def __init__(self, m1n1: Path, driver: Path) -> None:
        self.m1n1 = m1n1
        self.driver = driver
        self.driver_defines = parse_defines(driver)
        self.cache: dict[Path, dict[str, tuple[list[str], str]]] = {}
        self.failures = 0
        self.checks = 0

    def defines(self, path: Path) -> dict[str, tuple[list[str], str]]:
        if path not in self.cache:
            self.cache[path] = parse_defines(path)
        return self.cache[path]

    def source_value(self, source: Path, name: str) -> int:
        defines = self.defines(source)
        if name not in defines:
            raise ExpressionError(f"{source.name} has no {name}")
        parameters, body = defines[name]
        if parameters:
            raise ExpressionError(f"{name} in {source.name} is a function-like macro")
        return evaluate(body, {})

    def driver_value(self, name: str) -> int:
        if name not in self.driver_defines:
            raise ExpressionError(f"AppleNandStorage.h has no {name}")
        parameters, body = self.driver_defines[name]
        if parameters:
            raise ExpressionError(f"{name} in the driver header is function-like")
        return evaluate(body, EDK2_BITS)

    def same_value(self, source: Path, name: str, driver_name: str) -> None:
        self.checks += 1
        try:
            expected = self.source_value(source, name)
            actual = self.driver_value(driver_name)
        except ExpressionError as error:
            self.fail(f"{name} -> {driver_name}: {error}")
            return
        if expected != actual:
            self.fail(f"{name} -> {driver_name}: m1n1 has 0x{expected:x}, driver has 0x{actual:x}")

    def same_mask(self, source: Path, name: str, low_name: str, high_name: str) -> None:
        """m1n1 expresses a field as GENMASK/BIT; the driver as two positions."""
        self.checks += 1
        try:
            mask = self.source_value(source, name)
            low, high = mask_bounds(mask)
            driver_low = self.driver_value(low_name)
            driver_high = self.driver_value(high_name)
        except ExpressionError as error:
            self.fail(f"{name} -> ({low_name}, {high_name}): {error}")
            return
        if (low, high) != (driver_low, driver_high):
            self.fail(
                f"{name} -> ({low_name}, {high_name}): m1n1 mask 0x{mask:x} is bits "
                f"{high}:{low}, driver has bits {driver_high}:{driver_low}"
            )

    def same_bit(self, source: Path, name: str, driver_name: str) -> None:
        """m1n1 states a flag as BIT(n); the driver keeps the position."""
        self.checks += 1
        try:
            mask = self.source_value(source, name)
            low, high = mask_bounds(mask)
            driver_bit = self.driver_value(driver_name)
        except ExpressionError as error:
            self.fail(f"{name} -> {driver_name}: {error}")
            return
        if low != high:
            self.fail(f"{name} -> {driver_name}: m1n1 mask 0x{mask:x} is not a single bit")
            return
        if low != driver_bit:
            self.fail(f"{name} -> {driver_name}: m1n1 has bit {low}, driver has bit {driver_bit}")

    def same_offset(self, source: Path, name: str, driver_name: str) -> None:
        """Compare a function-like register macro at index 0 with a driver offset."""
        self.checks += 1
        defines = self.defines(source)
        if name not in defines:
            self.fail(f"{name} -> {driver_name}: {source.name} has no {name}")
            return
        parameters, body = defines[name]
        try:
            expected = evaluate(body, {"idx": 0})
            actual = self.driver_value(driver_name)
        except ExpressionError as error:
            self.fail(f"{name} -> {driver_name}: {error}")
            return
        if expected != actual:
            self.fail(f"{name} -> {driver_name}: m1n1 has 0x{expected:x}, driver has 0x{actual:x}")

    def fail(self, message: str) -> None:
        self.failures += 1
        print(f"  FAIL {message}")

    def report(self, label: str) -> None:
        print(f"  ok   {label}")


def run(m1n1: Path, driver: Path) -> int:
    if not m1n1.is_dir():
        print(f"skip: m1n1 sources not checked out at {m1n1}")
        return 0

    checker = Checker(m1n1, driver)
    if not driver.is_file():
        print(f"error: driver header not found at {driver}", file=sys.stderr)
        return 1

    nvme = m1n1 / "nvme.c"
    asc = m1n1 / "asc.c"
    sart = m1n1 / "sart.c"
    rtkit = m1n1 / "rtkit.c"

    for path in (nvme, asc, sart, rtkit):
        if not path.is_file():
            print(f"error: {path} is missing", file=sys.stderr)
            return 1

    print("NVMe engine and NVMMU (m1n1 src/nvme.c)")
    for name in (
        "NVME_CC",
        "NVME_CSTS",
        "NVME_AQA",
        "NVME_ASQ",
        "NVME_ACQ",
        "NVME_DB_ACQ",
        "NVME_DB_IOCQ",
        "NVME_BOOT_STATUS",
        "NVME_BOOT_STATUS_OK",
        "NVME_LINEAR_SQ_CTRL",
        "NVME_IOQ_CMDS",
        "NVME_IOQ_CQES",
        "NVME_MAX_PEND_CMDS_CTRL",
        "NVME_DB_LINEAR_ASQ",
        "NVME_DB_LINEAR_IOSQ",
        "NVMMU_NUM",
        "NVMMU_ASQ_BASE",
        "NVMMU_IOSQ_BASE",
        "NVMMU_TCB_INVAL",
        "NVMMU_TCB_STAT",
        "NVME_QUEUE_SIZE",
        "NVME_ADMIN_CMD_DELETE_SQ",
        "NVME_ADMIN_CMD_CREATE_SQ",
        "NVME_ADMIN_CMD_DELETE_CQ",
        "NVME_ADMIN_CMD_CREATE_CQ",
        "NVME_CMD_FLUSH",
        "NVME_CMD_WRITE",
        "NVME_CMD_READ",
    ):
        checker.same_value(nvme, name, name)

    checker.same_mask(nvme, "NVME_CC_SHN", "NVME_CC_SHN_START", "NVME_CC_SHN_END")
    checker.same_mask(nvme, "NVME_CSTS_SHST", "NVME_CSTS_SHST_START", "NVME_CSTS_SHST_END")
    checker.same_value(nvme, "NVME_CC_SHN_NORMAL", "NVME_CC_SHN_NORMAL")
    checker.same_value(nvme, "NVME_CSTS_SHST_DONE", "NVME_CSTS_SHST_DONE")
    checker.same_value(
        nvme, "NVMMU_TCB_DMA_FROM_DEVICE", "NVMMU_TCB_DMA_FROM_DEVICE"
    )
    checker.same_value(nvme, "NVMMU_TCB_DMA_TO_DEVICE", "NVMMU_TCB_DMA_TO_DEVICE")
    checker.report("offsets, opcodes, flags and DMA directions")

    print("ASC mailboxes (m1n1 src/asc.c)")
    for name in (
        "ASC_CPU_CONTROL",
        "ASC_CPU_CONTROL_START",
        "ASC_MBOX_A2I_CONTROL",
        "ASC_MBOX_A2I_SEND0",
        "ASC_MBOX_A2I_SEND1",
        "ASC_MBOX_I2A_CONTROL",
        "ASC_MBOX_I2A_RECV0",
        "ASC_MBOX_I2A_RECV1",
        "ASC_MBOX_CONTROL_FULL",
        "ASC_MBOX_CONTROL_EMPTY",
    ):
        checker.same_value(asc, name, name)
    checker.report("mailbox FIFOs and CPU control")

    print("SART windows (m1n1 src/sart.c)")
    checker.same_value(sart, "APPLE_SART_MAX_ENTRIES", "APPLE_SART_MAX_ENTRIES")
    for version in (0, 2):
        checker.same_mask(
            sart,
            f"APPLE_SART{version}_CONFIG_FLAGS",
            f"APPLE_SART{version}_CONFIG_FLAGS_START",
            f"APPLE_SART{version}_CONFIG_FLAGS_END",
        )
        checker.same_mask(
            sart,
            f"APPLE_SART{version}_CONFIG_SIZE",
            f"APPLE_SART{version}_CONFIG_SIZE_START",
            f"APPLE_SART{version}_CONFIG_SIZE_END",
        )
        checker.same_value(
            sart, f"APPLE_SART{version}_CONFIG_SIZE_SHIFT", f"APPLE_SART{version}_SIZE_SHIFT"
        )
        checker.same_value(
            sart, f"APPLE_SART{version}_PADDR_SHIFT", f"APPLE_SART{version}_PADDR_SHIFT"
        )
        checker.same_value(
            sart, f"APPLE_SART{version}_FLAGS_ALLOW", f"APPLE_SART{version}_FLAGS_ALLOW"
        )
        checker.same_offset(
            sart, f"APPLE_SART{version}_CONFIG", f"APPLE_SART{version}_CONFIG_OFFSET"
        )
        checker.same_offset(
            sart, f"APPLE_SART{version}_PADDR", f"APPLE_SART{version}_PADDR_OFFSET"
        )
    for version in (3, 4):
        checker.same_value(
            sart, f"APPLE_SART{version}_SIZE_SHIFT", f"APPLE_SART{version}_SIZE_SHIFT"
        )
        checker.same_value(
            sart, f"APPLE_SART{version}_PADDR_SHIFT", f"APPLE_SART{version}_PADDR_SHIFT"
        )
        checker.same_value(
            sart, f"APPLE_SART{version}_FLAGS_ALLOW", f"APPLE_SART{version}_FLAGS_ALLOW"
        )
        checker.same_mask(
            sart,
            f"APPLE_SART{version}_SIZE_MAX",
            f"APPLE_SART{version}_SIZE_START",
            f"APPLE_SART{version}_SIZE_END",
        )
        checker.same_offset(
            sart, f"APPLE_SART{version}_CONFIG", f"APPLE_SART{version}_CONFIG_OFFSET"
        )
        checker.same_offset(
            sart, f"APPLE_SART{version}_PADDR", f"APPLE_SART{version}_PADDR_OFFSET"
        )
        checker.same_offset(
            sart, f"APPLE_SART{version}_SIZE", f"APPLE_SART{version}_SIZE_OFFSET"
        )
    checker.report("all four register layouts")

    print("RTKit protocol (m1n1 src/rtkit.c)")
    for name in (
        "RTKIT_EP_MGMT",
        "RTKIT_EP_CRASHLOG",
        "RTKIT_EP_SYSLOG",
        "RTKIT_EP_DEBUG",
        "RTKIT_EP_IOREPORT",
        "RTKIT_EP_OSLOG",
        "MSG_BUFFER_REQUEST",
        "MSG_SYSLOG_INIT",
        "MSG_SYSLOG_LOG",
        "MSG_OSLOG_INIT",
        "MSG_OSLOG_ACK",
        "MGMT_MSG_HELLO",
        "MGMT_MSG_HELLO_ACK",
        "MGMT_MSG_IOP_PWR_STATE",
        "MGMT_MSG_IOP_PWR_STATE_ACK",
        "MGMT_MSG_EPMAP",
        "MGMT_MSG_AP_PWR_STATE",
        "MGMT_MSG_AP_PWR_STATE_ACK",
        "MGMT_MSG_START_EP",
        "RTKIT_MIN_VERSION",
        "RTKIT_MAX_VERSION",
    ):
        if name.startswith("MSG_"):
            checker.same_value(rtkit, name, "RTKIT_" + name)
        elif name.startswith("MGMT_MSG_"):
            checker.same_value(rtkit, name, "RTKIT_" + name)
        else:
            checker.same_value(rtkit, name, name)

    for name, low, high in (
        ("MGMT_TYPE", "RTKIT_MGMT_TYPE_START", "RTKIT_MGMT_TYPE_END"),
        ("MGMT_PWR_STATE", "RTKIT_MGMT_PWR_STATE_START", "RTKIT_MGMT_PWR_STATE_END"),
        (
            "MSG_BUFFER_REQUEST_SIZE",
            "RTKIT_MSG_BUFFER_REQUEST_SIZE_START",
            "RTKIT_MSG_BUFFER_REQUEST_SIZE_END",
        ),
        (
            "MSG_BUFFER_REQUEST_IOVA",
            "RTKIT_MSG_BUFFER_REQUEST_IOVA_START",
            "RTKIT_MSG_BUFFER_REQUEST_IOVA_END",
        ),
        (
            "MSG_SYSLOG_INIT_ENTRYSIZE",
            "RTKIT_MSG_SYSLOG_INIT_SIZE_START",
            "RTKIT_MSG_SYSLOG_INIT_SIZE_END",
        ),
        (
            "MSG_SYSLOG_INIT_COUNT",
            "RTKIT_MSG_SYSLOG_INIT_COUNT_START",
            "RTKIT_MSG_SYSLOG_INIT_COUNT_END",
        ),
        (
            "MSG_SYSLOG_LOG_INDEX",
            "RTKIT_MSG_SYSLOG_LOG_INDEX_START",
            "RTKIT_MSG_SYSLOG_LOG_INDEX_END",
        ),
        (
            "MGMT_MSG_HELLO_MINVER",
            "RTKIT_MGMT_MSG_HELLO_MINVER_START",
            "RTKIT_MGMT_MSG_HELLO_MINVER_END",
        ),
        (
            "MGMT_MSG_HELLO_MAXVER",
            "RTKIT_MGMT_MSG_HELLO_MAXVER_START",
            "RTKIT_MGMT_MSG_HELLO_MAXVER_END",
        ),
        (
            "MGMT_MSG_EPMAP_BASE",
            "RTKIT_MGMT_MSG_EPMAP_BASE_START",
            "RTKIT_MGMT_MSG_EPMAP_BASE_END",
        ),
        (
            "MGMT_MSG_EPMAP_BITMAP",
            "RTKIT_MGMT_MSG_EPMAP_BITMAP_START",
            "RTKIT_MGMT_MSG_EPMAP_BITMAP_END",
        ),
        (
            "MGMT_MSG_START_EP_IDX",
            "RTKIT_MGMT_MSG_START_EP_IDX_START",
            "RTKIT_MGMT_MSG_START_EP_IDX_END",
        ),
    ):
        checker.same_mask(rtkit, name, low, high)

    for name, driver_name in (
        ("MGMT_MSG_EPMAP_DONE", "RTKIT_MGMT_MSG_EPMAP_DONE_BIT"),
        ("MGMT_MSG_EPMAP_REPLY_DONE", "RTKIT_MGMT_MSG_EPMAP_REPLY_DONE_BIT"),
        ("MGMT_MSG_EPMAP_REPLY_MORE", "RTKIT_MGMT_MSG_EPMAP_REPLY_MORE_BIT"),
        ("MGMT_MSG_START_EP_FLAG", "RTKIT_MGMT_MSG_START_EP_FLAG_BIT"),
    ):
        checker.same_bit(rtkit, name, driver_name)
    checker.report("endpoints, management messages and field positions")

    print()
    if checker.failures:
        print(f"ANS2 constants: {checker.failures} of {checker.checks} checks failed")
        return 1

    print(f"ANS2 constants: OK ({checker.checks} checks against m1n1)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--m1n1", type=Path, default=DEFAULT_M1N1, help="m1n1 src directory")
    parser.add_argument("--driver", type=Path, default=DEFAULT_DRIVER, help="driver header")
    arguments = parser.parse_args()
    return run(arguments.m1n1, arguments.driver)


if __name__ == "__main__":
    sys.exit(main())
