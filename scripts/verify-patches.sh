#!/usr/bin/env bash
# Verify the project's patches still apply to the exact submodule commits this
# repository pins.
#
# The patches are the difference between "the firmware builds" and "the firmware
# builds only on my machine". Submodules move, so this check must be cheap enough
# to run before every push. It uses throwaway git worktrees and never modifies
# the checked-out tree.
#
# Usage: ./scripts/verify-patches.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fail=0
checked=0

verify() {
    local repo="$1" patch="$2" label="$3"
    if [ ! -e "$repo/.git" ]; then
        echo "skip: $label (submodule not initialized)"
        return 0
    fi
    if [ ! -f "$patch" ]; then
        echo "FAIL: $label (patch missing)"
        fail=1
        return 0
    fi

    local tmp tree
    tmp="$(mktemp -d)"
    tree="$tmp/tree"
    # --detach on HEAD copies exactly the pinned commit, not the dirty state.
    git -C "$repo" worktree add --quiet --detach "$tree" HEAD 2>/dev/null || {
        rmdir "$tmp" 2>/dev/null || true
        echo "FAIL: $label (cannot create a worktree)"
        fail=1
        return 0
    }
    checked=$((checked + 1))
    if git -C "$tree" apply --check "$patch" 2>/dev/null; then
        echo "ok:   $label"
    else
        echo "FAIL: $label does not apply to $(git -C "$repo" rev-parse --short HEAD)"
        fail=1
    fi
    git -C "$repo" worktree remove --force "$tree" >/dev/null 2>&1 || true
    rm -rf "$tmp"
}

verify "$ROOT/third_party/m1n1-windows" \
       "$ROOT/patches/m1n1-windows/0001-vgic-m4-topology.patch" \
       "m1n1-windows 0001 (M4 vGIC topology)"

verify "$ROOT/third_party/m1n1-windows" \
       "$ROOT/patches/m1n1-windows/0002-psci-t8132.patch" \
       "m1n1-windows 0002 (M4 PSCI topology)"

verify "$ROOT/third_party/m1n1-windows" \
       "$ROOT/patches/m1n1-windows/0003-pcie-guest-handoff.patch" \
       "m1n1-windows 0003 (PCIe guest hand-off)"

verify "$ROOT/third_party/ntasp-firmware" \
       "$ROOT/patches/ntasp-firmware/0001-m4-uefi-platform.patch" \
       "ntasp-firmware 0001 (M4 UEFI platform)"

verify "$ROOT/third_party/ntasp-firmware/MU_BASECORE" \
       "$ROOT/patches/ntasp-firmware/0002-nasm-scope.patch" \
       "MU_BASECORE 0002 (NASM scope)"

echo
if [ "$fail" -ne 0 ]; then
    echo "Patch verification: FAILED"
    exit 1
fi
echo "Patch verification: OK ($checked patch(es) apply to the pinned commits)"
