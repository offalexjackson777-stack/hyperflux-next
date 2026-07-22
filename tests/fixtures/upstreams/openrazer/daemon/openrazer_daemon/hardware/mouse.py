class ExampleMouseBase:
    USB_VID = 0x1532
    HAS_MATRIX = True
    MATRIX_DIMS = [1, 3]
    METHODS = ["get_battery", "get_dpi_xy", "set_dpi_xy"]
    DPI_MAX = 30000


class ExampleMouseWireless(ExampleMouseBase):
    """Class for the Razer Example Mouse (Wireless)"""

    USB_PID = 0x00A8
    METHODS = ExampleMouseBase.METHODS + ["is_charging"]
