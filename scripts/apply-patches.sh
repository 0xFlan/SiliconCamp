#!/usr/bin/env bash
# Apply the project's m1n1-windows patches to a clean checkout.
# Usage: ./scripts/apply-patches.sh [tree]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TREE="${1:-$ROOT/third_party/m1n1-windows}"
for p in /opt/homebrew/opt/llvm/bin /opt/homebrew/opt/lld/bin /opt/homebrew/opt/rustup/bin; do
    [ -d "$p" ] && PATH="$p:$PATH"
done
export PATH
PATCH="$ROOT/patches/m1n1-windows/0001-vgic-m4-topology.patch"

if [ ! -d "$TREE/.git" ]; then
    echo "error: not a git checkout: $TREE" >&2
    exit 1
fi

if [ ! -f "$PATCH" ]; then
    echo "error: patch not found: $PATCH" >&2
    exit 1
fi

if git -C "$TREE" apply --check "$PATCH" 2>/dev/null; then
    git -C "$TREE" apply "$PATCH"
    echo "applied: $(basename "$PATCH")"
else
    if git -C "$TREE" apply --reverse --check "$PATCH" 2>/dev/null; then
        echo "already applied: $(basename "$PATCH")"
    else
        echo "error: patch does not apply cleanly; inspect the tree first" >&2
        exit 1
    fi
fi
