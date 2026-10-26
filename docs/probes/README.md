# Probe logs

`docs/m4-platform.md` and `docs/bringup-plan.md` both call for small probes: one
question, one command, one result. This directory is where their output goes, so
the next hardware session starts from a log instead of a memory.

**There are no probe logs yet: nothing in this repository has run on the M4.**

## Rules

- One file per probe: `YYYY-MM-DD-<topic>.md` (for example
  `2026-10-27-m1n1-vgic-topology.md`).
- Record the build first: repository commit, artifact SHA-256, patch set, and boot
  policy. A boot that "worked once" without a log is not reproducible.
- Paste the raw output. A failing probe is a result; do not summarise it away.
- Redact before committing: serial number, hardware UUID, provisioning UDID,
  ECID, MAC addresses, Apple ID, and hostnames. No script in this repository needs
  them — `sysctl -n hw.model` and `ioreg ... -n device-tree` identify the machine
  well enough.
- Hardware probes are gated by `docs/safety.md`. Do not run them on the only Mac
  until the checklist is complete and the owner has approved the DFU-restore risk.

## Template

````md
# <topic> — <date>

## Question

What this probe is meant to answer.

## Setup

Commit and artifact hash, patch set, boot policy, cables/attachments, machine.

## Command

The exact command(s), copied from the shell.

## Result

The raw output, or the failure with the point where it stopped.

## Interpretation

What the result proves, and what it does not.

## Next

The follow-up probe or fix this enables.
````
