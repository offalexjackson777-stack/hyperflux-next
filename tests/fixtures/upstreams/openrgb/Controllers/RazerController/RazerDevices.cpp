static const razer_zone example_mouse_logo_zone =
{
    "Logo",
    ZONE_TYPE_SINGLE,
    1,
    1
};

static const razer_zone example_mouse_strip_zone =
{
    "LED Strip",
    ZONE_TYPE_LINEAR,
    1,
    2
};

static const razer_device example_mouse_wireless_device =
{
    "Razer Example Mouse (Wireless)",
    RAZER_EXAMPLE_MOUSE_WIRELESS_PID,
    DEVICE_TYPE_MOUSE,
    RAZER_MATRIX_TYPE_EXTENDED,
    0x1F,
    1,
    3,
    {
        &example_mouse_logo_zone,
        &example_mouse_strip_zone,
        NULL,
        NULL,
        NULL,
        NULL
    },
    NULL
};

static const razer_zone example_keyboard_zone =
{
    ZONE_EN_KEYBOARD,
    ZONE_TYPE_MATRIX,
    6,
    22
};

static const razer_device example_keyboard_wired_device =
{
    "Razer Example Keyboard (Wired)",
    RAZER_EXAMPLE_KEYBOARD_WIRED_PID,
    DEVICE_TYPE_KEYBOARD,
    RAZER_MATRIX_TYPE_EXTENDED,
    0x1F,
    6,
    22,
    {
        &example_keyboard_zone,
        NULL,
        NULL,
        NULL,
        NULL,
        NULL
    },
    &example_keyboard_layout
};
