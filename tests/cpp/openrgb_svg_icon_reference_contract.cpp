// SPDX-License-Identifier: GPL-2.0-only

// The plugin draws its icons with its own small SVG renderer. This contract
// holds every icon to what Qt SVG draws for the same file, through reference
// images openrgb-svg-icon-reference-writer made with Qt SVG.

#include "svg_icon.hpp"
#include "svg_icon_reference.hpp"

#include <hyperflux/openrgb/embedded_icons.hpp>

#include <QGuiApplication>
#include <QImage>
#include <QPainter>

#include <algorithm>
#include <cstddef>
#include <cstdlib>
#include <iostream>

namespace
{

using hyperflux::openrgb::native::generated::embedded_icons;

int difference(QRgb left, QRgb right)
{
    return std::max(
        {std::abs(qRed(left) - qRed(right)), std::abs(qGreen(left) - qGreen(right)),
         std::abs(qBlue(left) - qBlue(right)), std::abs(qAlpha(left) - qAlpha(right))});
}

struct Comparison
{
    int inked = 0;
    int off = 0;
};

/// Pixels the reference draws, and pixels where the two drawings differ by
/// more than antialiasing does between Qt releases.
Comparison compare(const QImage& reference, const QImage& ours)
{
    Comparison result;
    for(int y = 0; y < reference.height(); ++y)
    {
        for(int x = 0; x < reference.width(); ++x)
        {
            const QRgb expected = reference.pixel(x, y);
            result.inked += qAlpha(expected) > 0 ? 1 : 0;
            result.off += difference(expected, ours.pixel(x, y)) > 32 ? 1 : 0;
        }
    }
    return result;
}

/// One icon's cell. Converting a cell rather than the whole sheet keeps Qt
/// from splitting the conversion across its thread pool, which the
/// ThreadSanitizer lane would report inside Qt.
QImage cell(const QImage& sheet, int column, int size)
{
    return sheet.copy(column * size, 0, size, size).convertToFormat(QImage::Format_ARGB32_Premultiplied);
}

/// Same drawing, within one percent of its inked pixels.
bool matches(const Comparison& comparison)
{
    return comparison.inked > 0 && comparison.off * 100 <= comparison.inked;
}

} // namespace

int main(int argc, char** argv)
{
    QGuiApplication application(argc, argv);
    int failures = 0;
    for(const int size : hyperflux::openrgb::native::reference_sizes)
    {
        const QImage reference(QString(HFX_OPENRGB_ICON_REFERENCE_DIR "/reference-%1.png").arg(size));
        QImage ours = hyperflux::openrgb::native::blank_sheet(size);
        if(reference.size() != ours.size())
        {
            std::cerr << "reference-" << size << ".png does not match the embedded icons; rerun the writer\n";
            return 1;
        }
        QPainter painter(&ours);
        painter.setRenderHint(QPainter::Antialiasing);
        for(std::size_t column = 0; column < embedded_icons.size(); ++column)
        {
            const QRectF target(static_cast<double>(column) * size, 0, size, size);
            if(!hyperflux::openrgb::native::paint_svg(painter, embedded_icons[column].svg, target, Qt::black))
            {
                std::cerr << embedded_icons[column].name << ": not understood\n";
                ++failures;
            }
        }
        painter.end();
        for(std::size_t column = 0; column < embedded_icons.size(); ++column)
        {
            const int index = static_cast<int>(column);
            const Comparison comparison = compare(cell(reference, index, size), cell(ours, index, size));
            if(!matches(comparison))
            {
                std::cerr << embedded_icons[column].name << " at " << size << " px: " << comparison.off << " of "
                          << comparison.inked << " pixels differ from Qt SVG\n";
                ++failures;
            }
        }
        // The comparison must notice a different icon, or it proves nothing.
        if(matches(compare(cell(reference, 0, size), cell(ours, 1, size))))
        {
            std::cerr << "the comparison cannot tell " << embedded_icons[0].name << " from "
                      << embedded_icons[1].name << '\n';
            ++failures;
        }
    }
    return failures == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}
