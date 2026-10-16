# DeathStalker V2 Pro Tenkeyless Passive Charge State

## Result

`OBSERVED`

On 2026-10-17 watched sessions read the keyboard status report of the wireless
DeathStalker V2 Pro Tenkeyless, paired PID `0x0296`, through the HyperFlux V2
receiver, while a USB cable was plugged in and later unplugged. The report
uses the same layout as the mouse status report: battery level, then a
power-source byte, then a charge byte. The kernel already forwards the battery
level as the raw battery observation and the next two bytes together as the
raw route word, power-source byte high and charge byte low, at the next
sequence number. No kernel or UAPI change is needed.

## Mapping

| Route word | Source byte | Charge byte | Condition seen | Published state |
| --- | --- | --- | --- | --- |
| `0x0101` | `1` | `1` | on the USB cable | charging, or full when the battery reads 100 percent |
| `0x0001` | `0` | `1` | about 2 seconds after plug-in, and for about 4 seconds after unplug | charging, or full when the battery reads 100 percent |
| `0x0000` | `0` | `0` | off the cable | discharging |
| any other word | any | any | not observed | unknown |

Only a route word that directly follows the battery level of the same status
report counts. The keyboard identity report also carries a route value, and it
never sets the charge state.

## USB Cable

On plug-in the charge byte set first and the source byte about 2 seconds
later. On unplug the source byte cleared first and the charge byte about
4 seconds later. While the cable was plugged in the keyboard did not appear as
its own USB device, so the cable supplied power only. The battery level rose
from about 88 percent while charging.

## Scope

This observation qualifies the exact PID `0x0296` profile and its keyboard
status report. It qualifies no other PID and no other report. No raw capture,
serial number, or host path was retained.

## Limits

- The route word at a full battery on the cable was not observed. It may read
  `0x0100`, which is published as unknown. Full is shown only if the keyboard
  keeps reporting a charging word at a 100 percent battery level.
- The route word while a vendor charge limit holds the battery was not
  observed. A charging word there is published as charging, and any other word
  as unknown.
- For about 4 seconds after the cable is unplugged the keyboard still reports
  charging, and HyperFlux publishes what the keyboard reports.
- A charging word does not identify the cable that supplies power.
- A stale, missing, or unpaired keyboard reports an unknown charge state.
