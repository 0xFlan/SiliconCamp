#!/usr/bin/env bash
# Check the macOS host toolchain for the M4Windows bring-up tree.
# This script only inspects the host; it does not install anything.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
for p in /opt/homebrew/opt/llvm/bin /opt/homebrew/opt/lld/bin /opt/homebrew/opt/rustup/bin; do
    [ -d "$p" ] && PATH="$p:$PATH"
done
export PATH

fail=0

report() {
    printf '%-28s %s\n' "$1" "$2"
}

need() {
    if command -v "$1" >/dev/null 2>&1; then
        report "$1" "$(command -v "$1")"
    else
        report "$1" "MISSING"
        fail=1
    fi
}

echo "== host =="
report "uname -m" "$(uname -m)"
report "macOS" "$(sw_vers -productVersion 2>/dev/null || echo unknown)"
report "project root" "$ROOT"

echo
echo "== required tools =="
need git
need make
need clang
need llvm-config
need ld.lld
need llvm-objcopy
need rustup
need cargo
need python3
need qemu-system-aarch64

echo
echo "== versions =="
report "clang" "$(clang --version 2>/dev/null | head -1)"
report "llvm" "$(llvm-config --version 2>/dev/null || echo MISSING)"
report "lld" "$(ld.lld --version 2>/dev/null | head -1 || echo MISSING)"
report "rustc" "$(rustc --version 2>/dev/null || echo MISSING)"
report "cargo" "$(cargo --version 2>/dev/null || echo MISSING)"
report "qemu" "$(qemu-system-aarch64 --version 2>/dev/null | head -1 || echo MISSING)"

echo
echo "== rust target =="
if rustup target list --installed 2>/dev/null | grep -qx 'aarch64-unknown-none-softfloat'; then
    report "aarch64-unknown-none-softfloat" "installed"
else
    report "aarch64-unknown-none-softfloat" "MISSING (run: rustup target add aarch64-unknown-none-softfloat)"
    fail=1
fi

echo
echo "== source trees =="
for tree in third_party/m1n1 third_party/m1n1-windows third_party/ntasp-firmware; do
    if [ -d "$ROOT/$tree" ]; then
        report "$tree" "present"
    else
        report "$tree" "MISSING (run: git submodule update --init --depth 1)"
        fail=1
    fi
done

echo
if [ "$fail" -ne 0 ]; then
    echo "Host check: FAILED (see MISSING items above)."
    exit 1
fi

echo "Host check: OK."
