# Basilisk V3 Pro 35K Passive Charge State

## Result

`OBSERVED`

On 2026-10-17 a watched session read the charge field of the passive pointer
status report for the wireless Basilisk V3 Pro 35K, paired PID `0x00cd`,
through the HyperFlux V2 receiver. The kernel already forwards this field as
the raw charge observation. The field changed with the charging pad and with
nothing else.

## Mapping

| Raw value | Physical condition | Published state |
| --- | --- | --- |
| `0` | mouse lifted off the charging mat | discharging |
| `1` | mouse resting on the charging mat | charging, or full when the battery reads 100 percent |
| any other value | not observed | unknown |

The contact field of the same report (`0` off the mat, `1` on the mat) gives
the charge value its context. A `0` charge value is published as discharging
only when that contact field says the mouse is off the mat. A `0` charge value
while the mouse is on the mat was not observed and is published as unknown.

## Scope

This observation qualifies the exact PID `0x00cd` profile and its passive
pointer report. It qualifies no other PID and no other report. The keyboard
status report has no charge field, so the keyboard charge state stays unknown.
No raw capture, serial number, or host path was retained.

## Limits

- The value while the mouse sits on the mat at 100 percent was not observed.
  The full state is inferred from charging plus a 100 percent battery level.
- The mouse had a vendor charge limit of about 80 percent set. The value while
  the mouse sits on the mat and the limit stops charging was not observed, so
  a `0` charge value on the mat is published as unknown.
- The value while the mouse charges over a USB cable was not observed.
- A charging value does not identify the cable or mat that supplies power.
- A stale, missing, or unpaired child reports an unknown charge state.
