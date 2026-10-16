# Basilisk V3 Pro 35K Passive Charge State

## Result

`OBSERVED`

On 2026-10-17 watched sessions read the charge field of the passive pointer
status report for the wireless Basilisk V3 Pro 35K, paired PID `0x00cd`,
through the HyperFlux V2 receiver. The kernel already forwards this field as
the raw charge observation and the byte before it as the raw contact
observation. The charge field followed the charging mat and the USB cable and
nothing else.

## Mapping

| Charge value | Byte 3 of the same report | Condition seen | Published state |
| --- | --- | --- | --- |
| `1` | any value | on the charging mat, or on a USB cable | charging, or full when the battery reads 100 percent |
| `0` | `0` | lifted off the charging mat | discharging |
| `0` | any other value | not observed | unknown |
| any other value | any value | not observed | unknown |

Byte 3 read `0` off the mat and `1` on the mat. On a USB cable it read `6`.
Byte 3 is therefore not only a mat contact, and only its proven off-the-mat
value `0` lets a `0` charge value count as discharging.

## USB Cable

With the cable plugged in at a battery level of about 80 percent, the first
status report still read charge `0` with byte 3 `0`. The next reports read
charge `1` with byte 3 `6`, which is published as charging. The unplug was not
observed.

## Charge Limit

The user had a vendor charge limit of about 80 percent set. While the mouse
held at that limit on the mat, an independent desktop tool that asks the mouse
whether it is charging showed charging. This is device behaviour the user
observed, not a reading of this charge field at the limit. It means the mouse
itself reports charging while it holds at its limit, and HyperFlux publishes
what the mouse reports rather than guessing a limit.

## Scope

This observation qualifies the exact PID `0x00cd` profile and its passive
pointer report. It qualifies no other PID and no other report. A later
capture showed that the keyboard status report carries power-source and charge
bytes in the same positions, but no keyboard profile qualifies them yet, so the
keyboard charge state stays unknown. No raw capture, serial number, or host
path was retained.

## Limits

- The value while the mouse sits on the mat at 100 percent was not observed.
  The full state is inferred from charging plus a 100 percent battery level.
- A `0` charge value with a nonzero byte 3 was not observed and is published
  as unknown.
- The value after the USB cable is unplugged was not observed.
- For about two seconds after the cable is plugged in, the first report still
  reads discharging.
- A charging value does not identify the cable or mat that supplies power.
- A stale, missing, or unpaired child reports an unknown charge state.
