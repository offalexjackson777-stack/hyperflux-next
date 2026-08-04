# First Run

HyperFlux Next separates package installation from service activation. Keep a
receiver-only desktop connected and use the ordinary boot path.

## Enable The Services

After installing from a published package source or a reviewed local artifact,
enable the bridge and the socket for the current desktop user:

```sh
sudo systemctl enable hyperflux-next-bridge.service \
  "hyperflux-next-client@$(id -u).socket"
sudo reboot
```

The Full package connects its OpenRazer transport through the bridge service;
it does not replace or manage the stock OpenRazer daemon.

## Check The System

After login, run:

```sh
hyperfluxctl status
hyperfluxctl doctor
```

Ready means the installed package boundary and required routes are healthy. If
Full is installed, Doctor also checks its stock-application integration
boundary. A non-Ready result identifies the first missing boundary and does not
grant partial write authority.

## Open Applications Sequentially

Stock OpenRazer, Polychromatic, and OpenRGB remain independent clients. During
first validation, close one application before testing another so lease release
and handoff stay attributable.

Restoration ships disabled. Do not enable it to make a device appear; discovery
and route readiness are independent from lighting replay.
