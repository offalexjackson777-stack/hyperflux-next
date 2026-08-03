class ExampleKeyboard:
    """Class for the Razer Example Keyboard (Wired)"""

    USB_VID = 0x1532
    USB_PID = 0x0200
    HAS_MATRIX = True
    MATRIX_DIMS = [6, 22]
    METHODS = ["get_device_type_keyboard", "set_custom_effect", "set_key_row"]
