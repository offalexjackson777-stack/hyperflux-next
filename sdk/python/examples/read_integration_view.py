#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later

from __future__ import annotations

from hyperflux_sdk import (
    Client,
    ClientConfig,
    ClientId,
    ClientName,
    ProtocolFeatureId,
    UnixChannelConfig,
)


def main() -> None:
    client = Client.connect_unix(
        UnixChannelConfig(),
        ClientConfig(
            client_id=ClientId("example.connector.read-only"),
            client_name=ClientName("HyperFlux read-only connector example"),
            required_features=(ProtocolFeatureId("integration-view-projection"),),
        ),
    )
    try:
        view = client.integration_view()
        device_count = sum(len(receiver.devices) for receiver in view.receivers)
        print(f"receivers={len(view.receivers)} devices={device_count}")
    finally:
        client.close()


if __name__ == "__main__":
    main()
