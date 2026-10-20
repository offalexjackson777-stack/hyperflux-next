// SPDX-License-Identifier: GPL-2.0-only

// Writes the reference images openrgb-svg-icon-reference-contract compares
// the plugin's own SVG renderer against. Qt SVG draws them, so the plugin
// does not need to link it. Run it after changing an icon:
//   openrgb-svg-icon-reference-writer tests/cpp/fixtures/openrgb-icons

#include "svg_icon_reference.hpp"

#include <hyperflux/openrgb/embedded_icons.hpp>

#include <QGuiApplication>
#include <QImage>
#include <QPainter>
#include <QSvgRenderer>

#include <iostream>

int main(int argc, char** argv)
{
    QGuiApplication application(argc, argv);
    if(argc != 2)
    {
        std::cerr << "usage: openrgb-svg-icon-reference-writer DIRECTORY\n";
        return 2;
    }
    using hyperflux::openrgb::native::generated::embedded_icons;
    for(const int size : hyperflux::openrgb::native::reference_sizes)
    {
        QImage sheet = hyperflux::openrgb::native::blank_sheet(size);
        QPainter painter(&sheet);
        painter.setRenderHint(QPainter::Antialiasing);
        int column = 0;
        for(const auto& icon : embedded_icons)
        {
            QSvgRenderer renderer(QByteArray(icon.svg.data(), static_cast<int>(icon.svg.size())));
            if(!renderer.isValid())
            {
                std::cerr << "Qt SVG cannot read " << icon.name << '\n';
                return 1;
            }
            renderer.render(&painter, QRectF(column * size, 0, size, size));
            ++column;
        }
        painter.end();
        const QString path = QString::fromLocal8Bit(argv[1]) + QString("/reference-%1.png").arg(size);
        if(!sheet.save(path))
        {
            std::cerr << "cannot write " << path.toStdString() << '\n';
            return 1;
        }
    }
    return 0;
}
