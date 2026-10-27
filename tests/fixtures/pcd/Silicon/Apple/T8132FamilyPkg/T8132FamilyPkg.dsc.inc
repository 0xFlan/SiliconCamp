# Test fixture: the PCIe PCD block of T8132FamilyPkg.dsc.inc, reduced to the
# lines scripts/check-acpi.py reads.  See the header of the fixture Mac16_3.dsc.
#
# The values are the captured J624 (Mac16,3) ones from docs/pcie.md:
#   APCIE ECAM 0x1cb0000000 + 256 MB, bus range 0-8
#   32-bit window: bus 0x80000000 -> host 0xb80000000, 1 GB
#   prefetchable window: host 0xbc0000000 + 512 MB, no translation
#
# PcdPciMmio32Translation is host - bus (0xb00000000), not bus - host: the host
# bridge library negates it into the aperture's bus - host translation.

[PcdsPatchableInModule.common]
  gArmTokenSpaceGuid.PcdSystemMemoryBase|0x10000000000
  gArmTokenSpaceGuid.PcdSystemMemorySize|0x600000000 #24GB, Mac16,3

[PcdsFixedAtBuild.common]
  gAppleSiliconPkgTokenSpaceGuid.PcdAppleUartBase|0x3ad200000
  gAppleSiliconPkgTokenSpaceGuid.PcdAppleSocIdentifier|0x8132
  gArmTokenSpaceGuid.PcdGicDistributorBase|0x5000000000
  gArmTokenSpaceGuid.PcdGicRedistributorsBase|0x5100000000

  #PCIe settings
  gArmTokenSpaceGuid.PcdPciBusMin|0x0
  gArmTokenSpaceGuid.PcdPciBusMax|0x8
  gArmTokenSpaceGuid.PcdPciIoBase|0x0
  gArmTokenSpaceGuid.PcdPciIoSize|0x00010000
  # host - bus = 0xb80000000 - 0x80000000
  gArmTokenSpaceGuid.PcdPciMmio32Base|0x80000000
  gArmTokenSpaceGuid.PcdPciMmio32Size|0x40000000
  gEfiMdePkgTokenSpaceGuid.PcdPciMmio32Translation|0xB00000000
  gEfiMdePkgTokenSpaceGuid.PcdPciMmio64Translation|0x0
  gArmTokenSpaceGuid.PcdPciMmio64Base|0xBC0000000
  gArmTokenSpaceGuid.PcdPciMmio64Size|0x20000000
  gEfiMdePkgTokenSpaceGuid.PcdPciExpressBaseAddress|0x1CB0000000
  gAppleSiliconPkgTokenSpaceGuid.PcdPciExpressBarSize|0x10000000
  gAppleSiliconPkgTokenSpaceGuid.PcdPciExpressBarLimit|0x1CBFFFFFFF
  gEfiMdePkgTokenSpaceGuid.PcdPciIoTranslation|0x0

[LibraryClasses.common]
  MemoryInitPeiLib|T8132FamilyPkg/Library/MemoryInitPeiLib/MemoryInitPeiLib.inf
