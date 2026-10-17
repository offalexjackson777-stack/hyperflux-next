# Privacy And Support Bundles

When you ask for help, a support bundle shows the project what HyperFlux sees
on your computer. This page says what a bundle contains, what it leaves out, and
how to check it before you share it.

## Preview A Bundle

```sh
hyperfluxctl support-bundle --preview
```

The preview lists what kinds of information the bundle would contain and what
it leaves out. It uploads nothing, does not query your devices, and changes
nothing.

## Save A Bundle

```sh
hyperfluxctl support-bundle --output hyperflux-support.json
```

You should see `Support bundle written: hyperflux-support.json`. Only you can
read the file. Nothing is uploaded. You decide whether to attach it to a report.
If a file with that name already exists, the command stops without changing it:
delete or rename the old file first, or choose another name.

## What A Bundle Contains

- whether the packages are installed and the driver is active;
- the state of the HyperFlux service and the receivers it has found, in short
  form;
- which devices HyperFlux knows how to control, and what it can do with each;
- short, structured records of recent HyperFlux events and their results;
- a statement of whether the bundle queried devices, wrote to them, or used the
  network.

## What A Bundle Leaves Out

- hardware serial numbers and anything that identifies your computer;
- private file paths and terminal history;
- raw HID or USB data;
- captures, memory dumps, and free-form system journal text;
- answers to active queries sent to your devices.

A bundle is only a report. It does not give anyone access to your devices. See
the [security policy](../../SECURITY.md) for reporting security problems
privately.

## Hardware Reports

`hyperfluxctl qualification` makes a smaller report about your receiver and
devices, for device support requests and bug reports about a device. [Hardware](hardware.md#share-a-hardware-report) shows how to share
it. It only reads what the HyperFlux service already knows. It sends nothing to
your devices, writes nothing to them, and uploads nothing.

A hardware report contains:

- whether HyperFlux is ready, the HyperFlux version, and the version of the
  protocol that apps use to talk to the HyperFlux service;
- the installed driver and service versions;
- each receiver and device by model name and USB ID (such as `1532:00cd`),
  numbered `receiver-1`, `device-1`, and so on;
- for each device: keyboard or mouse, whether it is active or asleep, its
  battery level, how far it is tested, and the HyperFlux profile it uses;
- the checks a maintainer would still need to run for each device;
- short codes for problems the service has found, such as `HFX-SERVICE-002`;
- a statement that no device was queried and that firmware was not collected.

A hardware report leaves out:

- hardware serial numbers and the receiver and device names HyperFlux uses
  inside your computer, so two reports cannot be matched to one computer;
- firmware versions, because reading them means sending a query to the device;
- private file paths, user names, and the computer's name;
- raw HID or USB data and system journal text;
- the wording of service problems, which can name local details. Only their
  codes and how serious they are remain.

If you share a report to confirm that a device works, the project keeps only a
short public summary of it next to your request: the device model and USB ID,
the HyperFlux, driver, and service versions, the date, the problem codes, and
what you ticked in the form. The rest of the report and anything else you wrote
in the request are not copied. A report that holds anything looking private,
such as a file path, a user or computer name, a network address, or a serial
number, is turned down rather than cleaned up, and you are asked for a fresh
one. The maintainer tool says where in the report it found such text, but never
repeats the text itself.
