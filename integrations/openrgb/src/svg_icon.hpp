// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include <QColor>
#include <QPixmap>
#include <QRectF>

#include <string_view>

class QPainter;

namespace hyperflux::openrgb::native
{

/// Draws a small SVG icon. Supports the subset the plugin's icons use:
/// path, circle, rect, line, polyline, polygon and g, with fill, stroke,
/// stroke-width, stroke-linecap, stroke-linejoin and transform. The colour
/// "currentColor" becomes `current`. Returns false and draws nothing when
/// the document is not understood.
///
/// The plugin draws its own icons instead of linking Qt SVG so it needs only
/// the Qt Core, Gui and Widgets libraries OpenRGB itself loads.
bool paint_svg(QPainter& painter, std::string_view svg, const QRectF& target, const QColor& current);

/// One of the icons built into the plugin, by file name without ".svg".
/// Empty when no such icon exists.
[[nodiscard]] std::string_view embedded_icon(std::string_view name);

/// Renders an embedded icon at `size` logical pixels for a screen with
/// `device_pixel_ratio`. A null pixmap when the icon does not exist.
[[nodiscard]] QPixmap icon_pixmap(
    std::string_view name,
    int size,
    qreal device_pixel_ratio,
    const QColor& current);

} // namespace hyperflux::openrgb::native
