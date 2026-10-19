// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include <hyperflux/openrgb/embedded_icons.hpp>

#include <QColor>
#include <QImage>

#include <array>

namespace hyperflux::openrgb::native
{

/// Pixel sizes the page draws icons at: 16 at 1x, 16 at 2x and the 44 px
/// header mark at 2x.
inline constexpr std::array<int, 3> reference_sizes {16, 32, 88};

/// One row of icons, in embedded order, on a transparent sheet. Black stands
/// in for currentColor, as it does in Qt SVG.
inline QImage blank_sheet(int size)
{
    QImage sheet(
        size * static_cast<int>(generated::embedded_icons.size()),
        size,
        QImage::Format_ARGB32_Premultiplied);
    sheet.fill(Qt::transparent);
    return sheet;
}

} // namespace hyperflux::openrgb::native
