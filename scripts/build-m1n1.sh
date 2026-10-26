#!/usr/bin/env bash
# Build the NTASP m1n1-windows hypervisor used by the project.
# Override M1N1_TREE to build another m1n1 tree instead.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TREE="${M1N1_TREE:-$ROOT/third_party/m1n1-windows}"
for p in /opt/homebrew/opt/llvm/bin /opt/homebrew/opt/lld/bin /opt/homebrew/opt/rustup/bin; do
    [ -d "$p" ] && PATH="$p:$PATH"
done
export PATH

if [ ! -d "$TREE" ]; then
    echo "error: m1n1 tree not found: $TREE" >&2
    echo "hint: git submodule update --init --depth 1 third_party/m1n1-windows" >&2
    exit 1
fi

if ! command -v llvm-config >/dev/null 2>&1; then
    echo "error: llvm-config not found; install with: brew install llvm lld" >&2
    exit 1
fi

if ! command -v ld.lld >/dev/null 2>&1; then
    echo "error: ld.lld not found; install with: brew install lld" >&2
    exit 1
fi

if ! rustup target list --installed 2>/dev/null | grep -qx 'aarch64-unknown-none-softfloat'; then
    echo "error: Rust target aarch64-unknown-none-softfloat is missing" >&2
    echo "hint: rustup target add aarch64-unknown-none-softfloat" >&2
    exit 1
fi

echo "Building: $TREE"
make -C "$TREE" -j"$(sysctl -n hw.ncpu)"

echo
ls -lh "$TREE/build/m1n1.bin" "$TREE/build/m1n1.macho"
echo
echo "Built: $TREE/build/m1n1.bin"
