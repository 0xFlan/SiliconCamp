#!/usr/bin/env bash
# Build the NTASP UEFI firmware inside the Lima Linux VM.
#
# Usage: ./scripts/build-uefi-linux.sh [Mac16_3|MacBookProEarly2023]
#
# Prerequisites:
#   - Lima installed and a VM named "mu-build" created (see docs/build-uefi.md).
#   - The VM has clang/lld/iasl/Python and the repo mounted writable.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLATFORM="${1:-Mac16_3}"
VM="${LIMA_VM:-mu-build}"

case "$PLATFORM" in
    Mac16_3)
        BUILD_MODULE="Platform/Mac16_3Pkg/PlatformBuild.py"
        ;;
    MacBookProEarly2023)
        BUILD_MODULE="Platform/MacBookProEarly2023Pkg/PlatformBuild.py"
        ;;
    *)
        echo "error: unknown platform '$PLATFORM' (use Mac16_3 or MacBookProEarly2023)" >&2
        exit 1
        ;;
esac

# Fail before the 20-minute build, not after it, if the M4 patch is missing.
if [ ! -d "$ROOT/third_party/ntasp-firmware" ]; then
    echo "error: third_party/ntasp-firmware is not checked out" >&2
    echo "hint: git submodule update --init third_party/ntasp-firmware" >&2
    exit 1
fi
if [ "$PLATFORM" = "Mac16_3" ] && [ ! -f "$ROOT/third_party/ntasp-firmware/Platform/Mac16_3Pkg/Mac16_3.dsc" ]; then
    echo "error: the M4 platform package is not applied to the firmware tree" >&2
    echo "hint: ./scripts/apply-ntasp-patches.sh" >&2
    exit 1
fi

if ! command -v limactl >/dev/null 2>&1; then
    echo "error: limactl not found; install Lima with: brew install lima" >&2
    exit 1
fi

STATUS="$(limactl list --format '{{.Name}} {{.Status}}' 2>/dev/null | awk -v vm="$VM" '$1 == vm {print $2}')"
if [ "$STATUS" != "Running" ]; then
    echo "error: Lima VM '$VM' is not running (status: ${STATUS:-missing})" >&2
    echo "hint: limactl start --name=$VM --tty=false --vm-type=vz --mount-writable template:ubuntu-24.04" >&2
    exit 1
fi

# Quote the path for the guest shell; the project path has no spaces today.
QROOT="$(printf '%q' "$ROOT")"

echo "Building $PLATFORM in VM $VM"
limactl shell "$VM" -- bash -lc "
set -euo pipefail
cd $QROOT/third_party/ntasp-firmware
. .venv/bin/activate
export CLANGPDB_BIN=/usr/lib/llvm-18/bin/
export CLANGPDB_AARCH64_PREFIX=aarch64-linux-gnu-
stuart_update -c $BUILD_MODULE TOOL_CHAIN_TAG=CLANGPDB
stuart_build -c $BUILD_MODULE TOOL_CHAIN_TAG=CLANGPDB TARGET=DEBUG
"

echo
echo "FD output:"
find "$ROOT/third_party/ntasp-firmware/Build" -maxdepth 4 -name '*.fd' -print -exec ls -lh {} \;

if [ "$PLATFORM" = "Mac16_3" ]; then
    #
    # The ACPI/image checks never look inside the (compressed) FV, so a driver
    # that quietly dropped out of the image would still pass them.  The
    # internal-storage driver is the whole point of the ANS work, so its
    # presence is checked directly in the Ffs output.
    #
    ANS_MODULE="$(find "$ROOT/third_party/ntasp-firmware/Build" -maxdepth 7 -type d \
        -path '*/FV/Ffs/*AppleNANDStorageDxe' | head -n 1)"
    if [ -z "$ANS_MODULE" ]; then
        echo "error: AppleNANDStorageDxe is not in the FV; the internal SSD would not exist" >&2
        exit 1
    fi
    echo "FV contains $(basename "$ANS_MODULE")"
    echo
fi

if [ "$PLATFORM" = "Mac16_3" ]; then
    # The M4 package has validated expectations, so a failure here is real.
    "$ROOT/scripts/check-acpi.py" --platform "$PLATFORM"
else
    # The reference build proves the toolchain, not the M4 contract, and the
    # checker's expectations are written for Mac16_3.
    "$ROOT/scripts/check-acpi.py" --platform "$PLATFORM" --unvalidated-ok
fi
