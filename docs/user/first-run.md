# First Run

HyperFlux Next separates installation from activation so package ownership and
live device authority are easy to reason about.

## Check The System

Run `hyperfluxctl doctor` after the reviewed activation step. Ready means the
required installed components, bridge boundary, and any installed Full
integration are healthy. A non-Ready result identifies the first missing
boundary and does not grant partial device authority.

## Open Applications Sequentially

Stock OpenRazer, Polychromatic, and OpenRGB remain independent clients. During
first validation, close one application before testing another so lease release
and handoff remain attributable.

## Keep Restoration Disabled

The 2.0 candidate ships restoration disabled. Do not enable it merely to make a
device appear; discovery and route readiness are separate from lighting replay.
