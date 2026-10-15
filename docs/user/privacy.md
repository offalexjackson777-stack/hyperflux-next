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
