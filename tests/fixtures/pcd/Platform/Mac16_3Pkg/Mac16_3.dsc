##
# Test fixture: the PCIe part of the Mac16_3 platform DSC, reduced to the lines
# scripts/check-acpi.py reads.  It is not a buildable DSC -- the real one is in
# the firmware submodule -- and it exists so that the PCD cross-check (and the
# sign of PcdPciMmio32Translation in particular) is covered by a test that runs
# without the 337 MB firmware submodule checked out.
#
# tests/test_check_acpi.py compares this file against the real one when the
# submodule is present, so the two cannot drift apart unnoticed.
##

[Defines]
  PLATFORM_NAME                  = Mac16_3
  SUPPORTED_ARCHITECTURES        = AARCH64

[PcdsFixedAtBuild.common]
  gAppleSiliconPkgTokenSpaceGuid.PcdSmbiosSystemModelNumber|"Mac16,3"
  gAppleSiliconPkgTokenSpaceGuid.PcdAppleNumDwc3Controllers|4
  gAppleSiliconPkgTokenSpaceGuid.PcdAppleNumDwc3Darts|4

[Components.common]

  Mac16_3Pkg/AcpiTables/DeviceAcpiTables.inf

!include T8132FamilyPkg/T8132FamilyPkg.dsc.inc
