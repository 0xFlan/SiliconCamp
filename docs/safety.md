# Safety and recovery

## The risk

Booting a custom payload on Apple Silicon is not a normal reboot. The relevant operations are:

- changing the boot security policy (`bputil`) to Permissive Security;
- installing a custom boot object with `kmutil configure-boot`;
- booting m1n1, which then owns EL2 and the hardware;

A failed boot can leave the Mac unable to start macOS. Recovery is a **DFU restore**, which erases the internal storage. DFU restore on Apple Silicon requires a **second Mac** running Apple Configurator and a USB-C cable. There is no local recovery key that makes this risk-free.

This machine is the user's primary M4 iMac. Do not treat it as a disposable test device.

## Hard prerequisites before any hardware step

- [ ] A current Time Machine backup, and a test restore of at least one file.
- [ ] A second Mac with Apple Configurator 2 installed.
- [ ] A USB-C cable that supports data (not charge-only).
- [ ] The exact model identifier and chip confirmed: `Mac16,3`, `T8132`.
- [ ] The machine owner has explicitly approved the risk of a DFU restore.
- [ ] A written recovery sequence, tested as far as possible without changing boot policy.
- [ ] The Mac is plugged into power.
- [ ] FileVault/recovery-key implications understood; a DFU restore erases the disk.

If any box is unchecked, stop. Build and test in QEMU or on a spare supported Mac instead.

## What not to run yet

Do not run these on the M4 until the checklist is complete and the owner has approved:

```bash
# Do not run yet
bputil ...
kmutil configure-boot ...
```

Do not disable SIP, lower boot security, or install m1n1 "just to see if it boots."

## Recovery outline

The exact commands depend on the macOS version and the boot policy in use. The general recovery path is:

1. Disconnect the Mac from power.
2. Put the M4 into DFU mode (hold the power button while connecting the USB-C cable to the second Mac; the exact timing is model-specific).
3. Open Apple Configurator 2 on the second Mac.
4. Restore the M4.
5. Reinstall macOS and restore from the Time Machine backup.

Because step 4 erases the machine, the backup is mandatory.

## Safer development alternatives

- Build and static-check the firmware on the host.
- Use QEMU for generic AArch64/UEFI tests (not Apple hardware emulation).
- If a spare Apple Silicon Mac is available, use it for m1n1 bring-up first.
- Use the m1n1 proxy over USB/UART for inspection before installing a custom boot object.
- Make one change at a time, and keep a known-good boot path.

## Reporting

Record the exact m1n1 build hash, patch set, boot policy, and serial log for every hardware experiment. A boot that "worked once" without a log is not reproducible.
