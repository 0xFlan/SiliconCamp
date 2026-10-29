#!/usr/bin/env bash
# Verify the project's patches still apply to the exact submodule commits this
# repository pins.
#
# The patches are the difference between "the firmware builds" and "the firmware
# builds only on my machine". Submodules move, so this check must be cheap enough
# to run before every push. It uses throwaway git worktrees and never modifies
# the checked-out tree.
#
# The m1n1-windows patches are cumulative: 0002 assumes 0001, 0003 assumes both,
# and 0004 assumes all three. They are therefore verified as a series in a single
# worktree, in filename order, which is also the order apply-patches.sh uses.
# Checking each one against a pristine HEAD instead would pass for a patch that
# only applies to a tree some earlier patch already changed.
#
# Usage: ./scripts/verify-patches.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fail=0
checked=0
series=0

# verify_one TREE PATCH LABEL
#
# Throws nothing, but returns 1 on failure so the series loop can stop.
verify_one() {
    local tree="$1" patch="$2" label="$3"
    if [ ! -f "$patch" ]; then
        echo "FAIL: $label (patch missing)"
        return 1
    fi
    if git -C "$tree" apply --check "$patch" 2>/dev/null; then
        git -C "$tree" apply "$patch" 2>/dev/null
        echo "ok:   $label"
        return 0
    fi
    echo "FAIL: $label does not apply to the tree left by the previous patch"
    return 1
}

# verify_series_list REPO LABEL PATCH...
#
# Applies the named patches, in the order given, to one worktree of REPO@HEAD.
# This is what proves the cumulative contract: a later patch is checked against
# the tree its predecessors produced, exactly as a user would apply them.
verify_series_list() {
    local repo="$1" label="$2"
    shift 2
    local patches=("$@")
    if [ ! -e "$repo/.git" ]; then
        echo "skip: $label (submodule not initialized)"
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

    local patch name series_ok=0
    for patch in "${patches[@]}"; do
        name="$(basename "$patch" .patch)"
        if verify_one "$tree" "$patch" "$label $name"; then
            checked=$((checked + 1))
        else
            series_ok=1
            break
        fi
    done

    if [ "$series_ok" -eq 0 ]; then
        series=$((series + 1))
    else
        fail=1
    fi

    git -C "$repo" worktree remove --force "$tree" >/dev/null 2>&1 || true
    rm -rf "$tmp"
}

# verify_series REPO DIR LABEL
#
# The directory form, used where every patch in DIR belongs to the same repo.
verify_series() {
    local repo="$1" dir="$2" label="$3"

    local patches=("$dir"/*.patch)
    if [ ! -e "${patches[0]}" ]; then
        echo "FAIL: $label (no patches in $dir)"
        fail=1
        return 0
    fi

    verify_series_list "$repo" "$label" "${patches[@]}"
}

# verify REPO PATCH LABEL
#
# For a standalone patch that is not part of a cumulative series.
verify() {
    local repo="$1" patch="$2" label="$3"
    if [ ! -e "$repo/.git" ]; then
        echo "skip: $label (submodule not initialized)"
        return 0
    fi

    local tmp tree
    tmp="$(mktemp -d)"
    tree="$tmp/tree"
    git -C "$repo" worktree add --quiet --detach "$tree" HEAD 2>/dev/null || {
        rmdir "$tmp" 2>/dev/null || true
        echo "FAIL: $label (cannot create a worktree)"
        fail=1
        return 0
    }
    if verify_one "$tree" "$patch" "$label"; then
        checked=$((checked + 1))
    else
        fail=1
    fi
    git -C "$repo" worktree remove --force "$tree" >/dev/null 2>&1 || true
    rm -rf "$tmp"
}

verify_series "$ROOT/third_party/m1n1-windows" \
              "$ROOT/patches/m1n1-windows" \
              "m1n1-windows"

# 0003 and 0004 edit files 0001 creates, so they are one cumulative series.
verify_series_list "$ROOT/third_party/ntasp-firmware" \
                   "ntasp-firmware" \
                   "$ROOT/patches/ntasp-firmware/0001-m4-uefi-platform.patch" \
                   "$ROOT/patches/ntasp-firmware/0003-vgic-selftest.patch" \
                   "$ROOT/patches/ntasp-firmware/0004-vgic-selftest-group1-bit.patch"

verify "$ROOT/third_party/ntasp-firmware/MU_BASECORE" \
       "$ROOT/patches/ntasp-firmware/0002-nasm-scope.patch" \
       "MU_BASECORE 0002 (NASM scope)"

echo
if [ "$fail" -ne 0 ]; then
    echo "Patch verification: FAILED"
    exit 1
fi
echo "Patch verification: OK ($checked patch(es) in $series cumulative series applied to the pinned commits)"
