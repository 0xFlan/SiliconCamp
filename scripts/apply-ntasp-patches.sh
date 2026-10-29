#!/usr/bin/env bash
# Apply the project's NTASP firmware patches to a clean checkout.
# Usage: ./scripts/apply-ntasp-patches.sh [tree]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TREE="${1:-$ROOT/third_party/ntasp-firmware}"
PATCH_DIR="$ROOT/patches/ntasp-firmware"
MAIN_PATCH="$PATCH_DIR/0001-m4-uefi-platform.patch"
NASM_PATCH="$PATCH_DIR/0002-nasm-scope.patch"
SELFTEST_PATCH="$PATCH_DIR/0003-vgic-selftest.patch"
GROUP1_PATCH="$PATCH_DIR/0004-vgic-selftest-group1-bit.patch"

if [ ! -d "$TREE/.git" ] && [ ! -f "$TREE/.git" ]; then
    echo "error: not a git checkout: $TREE" >&2
    exit 1
fi

if [ ! -f "$MAIN_PATCH" ]; then
    echo "error: patch not found: $MAIN_PATCH" >&2
    exit 1
fi

# apply_patch REPO PATCH LABEL [MARKER]
#
# MARKER is a path, relative to REPO, that only exists once the patch has been
# applied.  It is the fallback for a patch that an earlier patch has since
# edited: 0003 changes files 0001 creates, so once 0003 is in place neither a
# forward nor a reverse check of 0001 can succeed, even though 0001 is applied.
apply_patch() {
    local repo="$1"
    local patch="$2"
    local label="$3"
    local marker="${4:-}"

    if git -C "$repo" apply --check "$patch" 2>/dev/null; then
        git -C "$repo" apply "$patch"
        echo "applied: $label"
    elif git -C "$repo" apply --reverse --check "$patch" 2>/dev/null; then
        echo "already applied: $label"
    elif [ -n "$marker" ] && [ -e "$repo/$marker" ]; then
        echo "already applied: $label (a later patch has since edited it)"
    else
        echo "error: $label does not apply cleanly; inspect the tree first" >&2
        return 1
    fi
}

apply_patch "$TREE" "$MAIN_PATCH" "0001-m4-uefi-platform.patch" \
            "Platform/Mac16_3Pkg/Mac16_3Pkg.dec"

if [ ! -f "$SELFTEST_PATCH" ]; then
    echo "error: patch not found: $SELFTEST_PATCH" >&2
    exit 1
fi
apply_patch "$TREE" "$SELFTEST_PATCH" "0003-vgic-selftest.patch" \
            "Silicon/Apple/AppleSiliconPkg/Drivers/AppleVgicSelfTestDxe/AppleVgicSelfTestDxe.inf"

#
# 0004 edits a file 0003 creates, so the .inf marker would already exist and
# would make a missing 0004 look applied.  Its own reverse check is the marker.
#
if [ ! -f "$GROUP1_PATCH" ]; then
    echo "error: patch not found: $GROUP1_PATCH" >&2
    exit 1
fi
apply_patch "$TREE" "$GROUP1_PATCH" "0004-vgic-selftest-group1-bit.patch"

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
