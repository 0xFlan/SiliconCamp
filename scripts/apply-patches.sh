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

# series_already_applied TREE
#
# True when the working tree is exactly HEAD plus the whole series.
#
# The per-patch reverse check below cannot answer this.  These are cumulative
# patches written against the tree their predecessors produce, so 0003's
# context stops existing once 0007 edits the same lines, and `git apply
# --reverse --check 0003` fails on a correctly patched tree.  Instead this
# stages the working tree into a throwaway index and reverse-applies the series
# in reverse order there.  Reconstructing HEAD proves the tree is HEAD plus the
# series, and neither the real index nor the working tree is touched.
series_already_applied() {
    local tree="$1"
    local tmp index
    tmp="$(mktemp -d)"
    index="$tmp/index"

    local ok=0
    (
        export GIT_INDEX_FILE="$index"
        git -C "$tree" add -A >/dev/null 2>&1 || exit 1

        local i
        for ((i = ${#PATCHES[@]} - 1; i >= 0; i--)); do
            git -C "$tree" apply --cached --reverse "${PATCHES[i]}" >/dev/null 2>&1 || exit 1
        done

        #
        # An uninitialised submodule (m1n1's artwork) is an empty directory in
        # the working tree, so staging the tree drops its gitlink.  That one
        # difference is not a deviation from the series; any other difference
        # is.
        #
        local path
        while IFS= read -r path; do
            [ -z "$path" ] && continue
            case "$(git -C "$tree" ls-tree HEAD -- "$path")" in
                160000*) ;;
                *) exit 1 ;;
            esac
        done <<< "$(git -C "$tree" diff --cached --name-only)"
    ) || ok=1

    rm -rf "$tmp"
    return "$ok"
}

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

if series_already_applied "$TREE"; then
    echo "already applied: all ${#PATCHES[@]} patches (tree is HEAD plus the series)"
    exit 0
fi

for patch in "${PATCHES[@]}"; do
    apply_patch "$patch"
done
