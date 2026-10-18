# SPDX-License-Identifier: GPL-2.0-only

isEmpty(HFX_OPENRGB_SOURCE_DIR) {
    error("HFX_OPENRGB_SOURCE_DIR is required")
}

include($$HFX_OPENRGB_SOURCE_DIR/OpenRGB.pro)

SOURCES -= startup/main_FreeBSD_Linux_MacOS.cpp
CONFIG -= lrelease embed_translations
TRANSLATIONS =

# The harness runs with no hardware visible, so upstream hardware detectors
# can never find a device. They are most of the build, so leave them out.
# Only the controllers that non-controller upstream code includes stay.
HFX_KEPT_CONTROLLERS = NanoleafController
HFX_KEPT_SOURCES =
for(path, SOURCES) {
    controller = $$section(path, /, 1, 1)
    !contains(path, ^Controllers/.*)|contains(HFX_KEPT_CONTROLLERS, $$controller) {
        HFX_KEPT_SOURCES += $$path
    }
}
SOURCES = $$HFX_KEPT_SOURCES

HFX_KEPT_HEADERS =
for(path, HEADERS) {
    controller = $$section(path, /, 1, 1)
    !contains(path, ^Controllers/.*)|contains(HFX_KEPT_CONTROLLERS, $$controller) {
        HFX_KEPT_HEADERS += $$path
    }
}
HEADERS = $$HFX_KEPT_HEADERS

HFX_REBASED_SOURCES =
for(path, SOURCES) {
    HFX_REBASED_SOURCES += $$absolute_path($$path, $$HFX_OPENRGB_SOURCE_DIR)
}
SOURCES = $$HFX_REBASED_SOURCES

HFX_REBASED_HEADERS =
for(path, HEADERS) {
    HFX_REBASED_HEADERS += $$absolute_path($$path, $$HFX_OPENRGB_SOURCE_DIR)
}
HEADERS = $$HFX_REBASED_HEADERS

HFX_REBASED_FORMS =
for(path, FORMS) {
    HFX_REBASED_FORMS += $$absolute_path($$path, $$HFX_OPENRGB_SOURCE_DIR)
}
FORMS = $$HFX_REBASED_FORMS

HFX_REBASED_RESOURCES =
for(path, RESOURCES) {
    HFX_REBASED_RESOURCES += $$absolute_path($$path, $$HFX_OPENRGB_SOURCE_DIR)
}
RESOURCES = $$HFX_REBASED_RESOURCES

# OpenRGB 1.1 adds a precompiled header, named relative to its source tree.
HFX_REBASED_PRECOMPILED_HEADER =
for(path, PRECOMPILED_HEADER) {
    HFX_REBASED_PRECOMPILED_HEADER += $$absolute_path($$path, $$HFX_OPENRGB_SOURCE_DIR)
}
PRECOMPILED_HEADER = $$HFX_REBASED_PRECOMPILED_HEADER

HFX_REBASED_INCLUDEPATH =
for(path, INCLUDEPATH) {
    HFX_REBASED_INCLUDEPATH += $$absolute_path($$path, $$HFX_OPENRGB_SOURCE_DIR)
}
INCLUDEPATH = $$HFX_REBASED_INCLUDEPATH $$HFX_OPENRGB_SOURCE_DIR
SOURCES += $$PWD/cpp/openrgb_plugin_manager_sdk_harness.cpp

TARGET = openrgb-plugin-manager-sdk-harness
