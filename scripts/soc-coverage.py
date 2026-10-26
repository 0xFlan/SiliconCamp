#!/usr/bin/env python3
"""Report Apple Silicon coverage in the NTASP firmware tree.

The project goal is Windows on every Apple Silicon Mac, so the useful question
is not "does platform X build" but "which machines have no firmware package at
all". This reads the machine list published by Asahi Linux and the packages that
exist in the vendored firmware tree, then reports the gap.

The machine table below is transcribed from Asahi's public device list
(https://asahilinux.org/docs/hw/devices/device-list/): marketing name, Model
identifier, Apple product (j) code, and SoC. It is data, not a guess; update it
when Apple ships a new family.

Usage:
    ./scripts/soc-coverage.py
    ./scripts/soc-coverage.py --tree /path/to/ntasp-firmware
    ./scripts/soc-coverage.py --json

Exit status is 0 when the report was produced; coverage itself is not an error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# (marketing name, model identifier, product code, SoC)
DEVICES = [
    ("Mac mini (M1, 2020)", "Macmini9,1", "J274AP", "T8103"),
    ("MacBook Pro (13-inch, M1, 2020)", "MacBookPro17,1", "J293AP", "T8103"),
    ("MacBook Air (M1, 2020)", "MacBookAir10,1", "J313AP", "T8103"),
    ("iMac (24-inch 4-port, M1, 2021)", "iMac21,1", "J456AP", "T8103"),
    ("iMac (24-inch 2-port, M1, 2021)", "iMac21,2", "J457AP", "T8103"),
    ("MacBook Pro (16-inch, M1 Pro, 2021)", "MacBookPro18,1", "J316sAP", "T6000"),
    ("MacBook Pro (16-inch, M1 Max, 2021)", "MacBookPro18,2", "J316cAP", "T6001"),
    ("MacBook Pro (14-inch, M1 Pro, 2021)", "MacBookPro18,3", "J314sAP", "T6000"),
    ("MacBook Pro (14-inch, M1 Max, 2021)", "MacBookPro18,4", "J314cAP", "T6001"),
    ("Mac Studio (M1 Max, 2022)", "Mac13,1", "J375cAP", "T6001"),
    ("Mac Studio (M1 Ultra, 2022)", "Mac13,2", "J375dAP", "T6002"),
    ("MacBook Pro (13-inch, M2, 2022)", "Mac14,7", "J493AP", "T8112"),
    ("MacBook Air (13-inch, M2, 2022)", "Mac14,2", "J413AP", "T8112"),
    ("Mac mini (M2, 2023)", "Mac14,3", "J473AP", "T8112"),
    ("Mac mini (M2 Pro, 2023)", "Mac14,12", "J474sAP", "T6020"),
    ("MacBook Pro (14-inch, M2 Pro, 2023)", "Mac14,9", "J414sAP", "T6020"),
    ("MacBook Pro (16-inch, M2 Pro, 2023)", "Mac14,10", "J416sAP", "T6020"),
    ("MacBook Pro (14-inch, M2 Max, 2023)", "Mac14,5", "J414cAP", "T6021"),
    ("MacBook Pro (16-inch, M2 Max, 2023)", "Mac14,6", "J416cAP", "T6021"),
    ("MacBook Air (15-inch, M2, 2023)", "Mac14,15", "J415AP", "T8112"),
    ("Mac Studio (M2 Max, 2023)", "Mac14,13", "J475cAP", "T6021"),
    ("Mac Studio (M2 Ultra, 2023)", "Mac14,14", "J475dAP", "T6022"),
    ("Mac Pro (2023)", "Mac14,8", "J180dAP", "T6022"),
    ("iMac (24-inch 2-port, M3, 2023)", "Mac15,4", "J433AP", "T8122"),
    ("iMac (24-inch 4-port, M3, 2023)", "Mac15,5", "J434AP", "T8122"),
    ("MacBook Pro (14-inch, M3, 2023)", "Mac15,3", "J504AP", "T8122"),
    ("MacBook Pro (14-inch, M3 Pro, 2023)", "Mac15,6", "J514sAP", "T6030"),
    ("MacBook Pro (16-inch, M3 Pro, 2023)", "Mac15,7", "J516sAP", "T6030"),
    ("MacBook Pro (14-inch, M3 Max, 2023)", "Mac15,8", "J514cAP", "T6031"),
    ("MacBook Pro (16-inch, M3 Max, 2023)", "Mac15,9", "J516cAP", "T6031"),
    ("MacBook Pro (14-inch, M3 Max, 2023)", "Mac15,10", "J514mAP", "T6034"),
    ("MacBook Pro (16-inch, M3 Max, 2023)", "Mac15,11", "J516mAP", "T6034"),
    ("MacBook Air (13-inch, M3, 2024)", "Mac15,12", "J613AP", "T8122"),
    ("MacBook Air (15-inch, M3, 2024)", "Mac15,13", "J615AP", "T8122"),
    ("Mac Studio (M3 Ultra, 2025)", "Mac15,14", "J575dAP", "T6032"),
    ("iMac (24-inch 2-port, M4, 2024)", "Mac16,2", "J623AP", "T8132"),
    ("iMac (24-inch 4-port, M4, 2024)", "Mac16,3", "J624AP", "T8132"),
    ("Mac mini (M4, 2024)", "Mac16,10", "J773gAP", "T8132"),
    ("Mac mini (M4 Pro, 2024)", "Mac16,11", "J773sAP", "T6040"),
    ("MacBook Pro (14-inch, M4, 2024)", "Mac16,1", "J604AP", "T8132"),
    ("MacBook Pro (14-inch, M4 Pro, 2024)", "Mac16,8", "J614sAP", "T6040"),
    ("MacBook Pro (16-inch, M4 Pro, 2024)", "Mac16,7", "J616sAP", "T6040"),
    ("MacBook Pro (14-inch, M4 Max, 2024)", "Mac16,6", "J614cAP", "T6041"),
    ("MacBook Pro (16-inch, M4 Max, 2024)", "Mac16,5", "J616cAP", "T6041"),
    ("MacBook Air (13-inch, M4, 2025)", "Mac16,12", "J713AP", "T8132"),
    ("MacBook Air (15-inch, M4, 2025)", "Mac16,13", "J715AP", "T8132"),
    ("Mac Studio (M4 Max, 2025)", "Mac16,9", "J575cAP", "T6041"),
    ("MacBook Neo (A18 Pro, 2026)", "Mac17,5", "J700AP", "T8040"),
    ("Mac mini (M5 Pro, 2026)", "Mac17,16", "J873sAP", "T6050"),
    ("MacBook Pro (14-inch, M5, 2025)", "Mac17,2", "J704AP", "T8142"),
    ("MacBook Pro (14-inch, M5 Pro, 2026)", "Mac17,9", "J714sAP", "T6050"),
    ("MacBook Pro (16-inch, M5 Pro, 2026)", "Mac17,8", "J716sAP", "T6050"),
    ("MacBook Pro (14-inch, M5 Max, 2026)", "Mac17,7", "J714cAP", "T6051"),
    ("MacBook Pro (16-inch, M5 Max, 2026)", "Mac17,6", "J716cAP", "T6051"),
    ("MacBook Air (13-inch, M5, 2026)", "Mac17,3", "J813AP", "T8142"),
    ("MacBook Air (15-inch, M5, 2026)", "Mac16,4", "J815AP", "T8142"),
    ("Mac Studio (M5 Max, 2026)", "Mac17,14", "J775cAP", "T6051"),
    ("Mac Studio (M5 Ultra, 2026)", "Mac17,15", "J775dAP", "T6052"),
    ("Mac mini (M6, 2026)", "Mac18,5", "J873gAP", "T8152"),
]

# A silicon package covers every SoC in its family. The NTASP tree names them
# T810XFamilyPkg, T602XFamilyPkg, ...; update this map when adding a family.
FAMILY_SOCS = {
    "T810X": ("T8103",),
    "T811X": ("T8112",),
    "T600X": ("T6000", "T6001"),
    "T602X": ("T6020", "T6021", "T6022"),
    "T8122": ("T8122",),
    "T8132": ("T8132",),
    # Not implemented yet; listed so the report says what would cover them.
    "T604X": ("T6040", "T6041"),
    "T603X": ("T6030", "T6031", "T6034"),
    "T6032": ("T6032",),
    "T605X": ("T6050", "T6051", "T6052"),
    "T814X": ("T8142",),
    "T8152": ("T8152",),
    "T8040": ("T8040",),
}


def scan(tree: str) -> dict:
    silicon_dir = os.path.join(tree, "Silicon", "Apple")
    platform_dir = os.path.join(tree, "Platform")
    families = set()
    if os.path.isdir(silicon_dir):
        for name in os.listdir(silicon_dir):
            if name.endswith("FamilyPkg"):
                families.add(name[: -len("FamilyPkg")])
    platforms = set()
    if os.path.isdir(platform_dir):
        platforms = {n for n in os.listdir(platform_dir) if n.endswith("Pkg")}
    return {"families": families, "platforms": platforms}


def family_for(soc: str) -> str | None:
    for family, socs in FAMILY_SOCS.items():
        if soc in socs:
            return family
    return None


def build_report(tree: str) -> dict:
    found = scan(tree)
    machines = []
    for name, model, product, soc in DEVICES:
        family = family_for(soc)
        silicon = family in found["families"] if family else False
        package = model.replace(",", "_") + "Pkg"
        machines.append({
            "name": name,
            "model": model,
            "product": product,
            "soc": soc,
            "family": family,
            "silicon": silicon,
            "platform": package in found["platforms"],
        })
    return {
        "tree": tree,
        "families_found": sorted(found["families"]),
        "platforms_found": sorted(found["platforms"]),
        "machines": machines,
    }


def print_text(report: dict) -> None:
    machines = report["machines"]
    covered = [m for m in machines if m["silicon"]]
    platforms = [m for m in machines if m["platform"]]
    print(f"firmware tree: {report['tree']}")
    print(f"machines: {len(machines)}")
    # A silicon family package is not a machine port: it is the SoC half of the
    # firmware.  Keep the two numbers separate or the report flatters itself.
    print(f"  SoC family has a silicon package: {len(covered)}")
    print(f"  machine has a platform package: {len(platforms)}")
    print(f"  no silicon package at all: {len(machines) - len(covered)}\n")

    print("silicon families")
    families: dict[str, list[dict]] = {}
    for machine in machines:
        families.setdefault(machine["family"] or "?", []).append(machine)
    for family in sorted(families):
        group = families[family]
        state = "present" if group[0]["silicon"] else "MISSING"
        socs = ", ".join(sorted({m["soc"] for m in group}))
        print(f"  {family:<7} {state:<8} {len(group):>2} machines   {socs}")

    missing = [m for m in machines if not m["silicon"]]
    if missing:
        print("\nmachines with no silicon package")
        for machine in missing:
            print(f"  {machine['model']:<14} {machine['product']:<8} "
                  f"{machine['soc']:<6} {machine['name']}")

    if platforms:
        print("\nmachines with a platform package (by model identifier)")
        for machine in platforms:
            print(f"  {machine['model']:<14} {machine['name']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", default=None,
                        help="ntasp-firmware checkout (default: the submodule)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    tree = args.tree or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "third_party", "ntasp-firmware")
    if not os.path.isdir(tree):
        sys.exit(f"error: firmware tree not found: {tree}")

    report = build_report(tree)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_text(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
