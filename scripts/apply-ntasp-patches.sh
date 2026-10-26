#!/usr/bin/env bash
# Apply the project's NTASP firmware patches to a clean checkout.
# Usage: ./scripts/apply-ntasp-patches.sh [tree]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TREE="${1:-$ROOT/third_party/ntasp-firmware}"
PATCH_DIR="$ROOT/patches/ntasp-firmware"
MAIN_PATCH="$PATCH_DIR/0001-m4-uefi-platform.patch"
NASM_PATCH="$PATCH_DIR/0002-nasm-scope.patch"

if [ ! -d "$TREE/.git" ] && [ ! -f "$TREE/.git" ]; then
    echo "error: not a git checkout: $TREE" >&2
    exit 1
fi

if [ ! -f "$MAIN_PATCH" ]; then
    echo "error: patch not found: $MAIN_PATCH" >&2
    exit 1
fi

apply_patch() {
    local repo="$1"
    local patch="$2"
    local label="$3"

    if git -C "$repo" apply --check "$patch" 2>/dev/null; then
        git -C "$repo" apply "$patch"
        echo "applied: $label"
    elif git -C "$repo" apply --reverse --check "$patch" 2>/dev/null; then
        echo "already applied: $label"
    else
        echo "error: $label does not apply cleanly; inspect the tree first" >&2
        return 1
    fi
}

apply_patch "$TREE" "$MAIN_PATCH" "0001-m4-uefi-platform.patch"

if [ -e "$TREE/MU_BASECORE/.git" ]; then
    if [ ! -f "$NASM_PATCH" ]; then
        echo "error: patch not found: $NASM_PATCH" >&2
        exit 1
    fi
    apply_patch "$TREE/MU_BASECORE" "$NASM_PATCH" "0002-nasm-scope.patch"
else
    echo "note: MU_BASECORE is not initialized yet." >&2
    echo "      Run stuart_setup once, then re-run this script to apply 0002-nasm-scope.patch." >&2
fi

echo
echo "next: ./scripts/build-uefi-linux.sh Mac16_3"
