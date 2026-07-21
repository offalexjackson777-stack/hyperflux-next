# SPDX-License-Identifier: GPL-2.0-or-later

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .contract import IdentityMode, identity_for_mode, reconcile_bounds
from .runtime import OpenRazerRuntime
from .service import ServiceController, initialize_dbus_mainloop, run_loop


def parser() -> argparse.ArgumentParser:
    minimum, default, maximum = reconcile_bounds()
    value = argparse.ArgumentParser(
        prog="hyperflux-openrazer-compat",
        description="Run the optional private HyperFlux OpenRazer compatibility provider.",
    )
    value.add_argument(
        "--identity",
        choices=tuple(mode.value for mode in IdentityMode),
        default=IdentityMode.PRIVATE.value,
        help="private by default; org.razer is accepted only inside the isolated launcher",
    )
    value.add_argument("--socket", type=Path, help="override the local HyperFlux SDK socket")
    value.add_argument(
        "--reconcile-interval-ms",
        type=int,
        default=default,
        metavar=f"{minimum}..{maximum}",
    )
    return value


def main(arguments: list[str] | None = None) -> int:
    options = parser().parse_args(arguments)
    minimum, _, maximum = reconcile_bounds()
    if not minimum <= options.reconcile_interval_ms <= maximum:
        parser().error(
            f"--reconcile-interval-ms must be from {minimum} through {maximum}"
        )
    try:
        identity = identity_for_mode(IdentityMode(options.identity))
        initialize_dbus_mainloop()
        runtime = OpenRazerRuntime.production(options.socket)
        initial = runtime.refresh()
        controller = ServiceController(
            runtime,
            identity,
            options.reconcile_interval_ms,
            initial,
        )
        return run_loop(controller)
    except BaseException as error:
        record = {
            "schema": "hyperflux-openrazer-startup-error-v1",
            "error_type": type(error).__name__,
            "message": str(error),
            "hardware_write_executed": False,
        }
        print(json.dumps(record, sort_keys=True, separators=(",", ":")), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
