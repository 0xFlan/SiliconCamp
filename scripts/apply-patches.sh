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
PATCH_DIR="$ROOT/patches/m1n1-windows"

# -e, not -d: a git worktree has a .git *file* pointing at the real repo.
if [ ! -e "$TREE/.git" ]; then
    echo "error: not a git checkout: $TREE" >&2
    exit 1
fi

# Bash 3.2 (the /bin/bash on macOS) has no mapfile; the glob sorts by name,
# which is the apply order 0001, 0002, ...
PATCHES=("$PATCH_DIR"/*.patch)
if [ ! -e "${PATCHES[0]}" ]; then
    echo "error: no patches found in $PATCH_DIR" >&2
    exit 1
fi

apply_patch() {
    local patch="$1"
    local label
    label="$(basename "$patch")"

    if git -C "$TREE" apply --check "$patch" 2>/dev/null; then
        git -C "$TREE" apply "$patch"
        echo "applied: $label"
    elif git -C "$TREE" apply --reverse --check "$patch" 2>/dev/null; then
        echo "already applied: $label"
    else
        echo "error: $label does not apply cleanly; inspect the tree first" >&2
        exit 1
    fi
}

for patch in "${PATCHES[@]}"; do
    apply_patch "$patch"
done
