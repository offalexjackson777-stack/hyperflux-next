# SPDX-License-Identifier: GPL-2.0-only

isEmpty(HFX_OPENRGB_SOURCE_DIR) {
    error("HFX_OPENRGB_SOURCE_DIR is required")
}

include($$HFX_OPENRGB_SOURCE_DIR/OpenRGB.pro)

SOURCES -= startup/main_FreeBSD_Linux_MacOS.cpp
CONFIG -= lrelease embed_translations
TRANSLATIONS =

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

HFX_REBASED_INCLUDEPATH =
for(path, INCLUDEPATH) {
    HFX_REBASED_INCLUDEPATH += $$absolute_path($$path, $$HFX_OPENRGB_SOURCE_DIR)
}
INCLUDEPATH = $$HFX_REBASED_INCLUDEPATH $$HFX_OPENRGB_SOURCE_DIR
SOURCES += $$PWD/cpp/openrgb_plugin_manager_sdk_harness.cpp

TARGET = openrgb-plugin-manager-sdk-harness
