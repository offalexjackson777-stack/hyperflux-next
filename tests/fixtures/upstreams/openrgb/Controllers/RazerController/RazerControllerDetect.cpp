REGISTER_HID_DETECTOR_IPU(
    "Razer Example Mouse (Wireless)",
    DetectRazerControllers,
    RAZER_VID,
    RAZER_EXAMPLE_MOUSE_WIRELESS_PID,
    0x00,
    0x01,
    0x02
);

REGISTER_HID_DETECTOR_IPU(
    "Razer Example Keyboard (Wired)",
    DetectRazerControllers,
    RAZER_VID,
    RAZER_EXAMPLE_KEYBOARD_WIRED_PID,
    0x02,
    0x01,
    0x02
);
