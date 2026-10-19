// SPDX-License-Identifier: GPL-2.0-only

#include "svg_icon.hpp"

#include <hyperflux/openrgb/embedded_icons.hpp>

#include <QByteArray>
#include <QImage>
#include <QPainter>
#include <QPainterPath>
#include <QPen>
#include <QString>
#include <QTransform>
#include <QXmlStreamReader>

#include <algorithm>
#include <charconv>
#include <cmath>
#include <numbers>
#include <optional>
#include <string>
#include <system_error>
#include <vector>

namespace hyperflux::openrgb::native
{
namespace
{

struct Paint
{
    bool none = false;
    bool current = false;
    QColor colour;
};

struct Style
{
    Paint fill {false, false, QColor(Qt::black)};
    Paint stroke {true, false, QColor()};
    double stroke_width = 1.0;
    Qt::PenCapStyle cap = Qt::FlatCap;
    Qt::PenJoinStyle join = Qt::MiterJoin;
    QTransform transform;
};

struct Shape
{
    QPainterPath path;
    Style style;
};

bool is_separator(char c)
{
    return c == ' ' || c == ',' || c == '\t' || c == '\n' || c == '\r' || c == '\f';
}

bool is_number_start(char c)
{
    return (c >= '0' && c <= '9') || c == '-' || c == '+' || c == '.';
}

/// Reads SVG numbers and flags from an attribute. Locale-independent: the
/// host application may have set a locale whose decimal mark is a comma.
class Scanner
{
public:
    explicit Scanner(std::string text) : text_(std::move(text)) {}

    void skip()
    {
        while(index_ < text_.size() && is_separator(text_[index_]))
        {
            ++index_;
        }
    }

    [[nodiscard]] bool done()
    {
        skip();
        return index_ >= text_.size();
    }

    [[nodiscard]] bool at_number()
    {
        skip();
        return index_ < text_.size() && is_number_start(text_[index_]);
    }

    [[nodiscard]] std::optional<char> letter()
    {
        skip();
        if(index_ >= text_.size())
        {
            return std::nullopt;
        }
        const char c = text_[index_];
        if((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z'))
        {
            ++index_;
            return c;
        }
        return std::nullopt;
    }

    [[nodiscard]] std::optional<double> number()
    {
        skip();
        std::size_t begin = index_;
        if(begin < text_.size() && text_[begin] == '+')
        {
            ++begin;
        }
        double value = 0.0;
        const auto [end, error] =
            std::from_chars(text_.data() + begin, text_.data() + text_.size(), value);
        if(error != std::errc() || !std::isfinite(value))
        {
            return std::nullopt;
        }
        index_ = static_cast<std::size_t>(end - text_.data());
        return value;
    }

    /// Arc flags may be written without separators ("a1 1 0 01 1 1").
    [[nodiscard]] std::optional<bool> flag()
    {
        skip();
        if(index_ < text_.size() && (text_[index_] == '0' || text_[index_] == '1'))
        {
            return text_[index_++] == '1';
        }
        return std::nullopt;
    }

private:
    std::string text_;
    std::size_t index_ = 0;
};

QPointF reflect(const QPointF& control, const QPointF& around)
{
    return around * 2.0 - control;
}

/// Endpoint arc to cubic Béziers, after SVG 1.1 implementation notes F.6.5 and F.6.6.
void arc_to(
    QPainterPath& path,
    const QPointF& from,
    double rx,
    double ry,
    double rotation_degrees,
    bool large_arc,
    bool sweep,
    const QPointF& to)
{
    if(from == to)
    {
        return;
    }
    rx = std::abs(rx);
    ry = std::abs(ry);
    if(rx == 0.0 || ry == 0.0)
    {
        path.lineTo(to);
        return;
    }
    const double phi = rotation_degrees * std::numbers::pi / 180.0;
    const double cos_phi = std::cos(phi);
    const double sin_phi = std::sin(phi);
    const double half_dx = (from.x() - to.x()) / 2.0;
    const double half_dy = (from.y() - to.y()) / 2.0;
    const double x1 = cos_phi * half_dx + sin_phi * half_dy;
    const double y1 = -sin_phi * half_dx + cos_phi * half_dy;
    const double lambda = (x1 * x1) / (rx * rx) + (y1 * y1) / (ry * ry);
    if(lambda > 1.0)
    {
        rx *= std::sqrt(lambda);
        ry *= std::sqrt(lambda);
    }
    const double numerator = rx * rx * ry * ry - rx * rx * y1 * y1 - ry * ry * x1 * x1;
    const double denominator = rx * rx * y1 * y1 + ry * ry * x1 * x1;
    const double coefficient = (large_arc == sweep ? -1.0 : 1.0)
        * std::sqrt(std::max(0.0, numerator / denominator));
    const double centre_x1 = coefficient * rx * y1 / ry;
    const double centre_y1 = -coefficient * ry * x1 / rx;
    const double cx = cos_phi * centre_x1 - sin_phi * centre_y1 + (from.x() + to.x()) / 2.0;
    const double cy = sin_phi * centre_x1 + cos_phi * centre_y1 + (from.y() + to.y()) / 2.0;
    const auto angle = [](double ux, double uy, double vx, double vy)
    {
        return std::atan2(ux * vy - uy * vx, ux * vx + uy * vy);
    };
    const double start_x = (x1 - centre_x1) / rx;
    const double start_y = (y1 - centre_y1) / ry;
    const double end_x = (-x1 - centre_x1) / rx;
    const double end_y = (-y1 - centre_y1) / ry;
    const double theta = angle(1.0, 0.0, start_x, start_y);
    double sweep_angle = angle(start_x, start_y, end_x, end_y);
    if(!sweep && sweep_angle > 0.0)
    {
        sweep_angle -= 2.0 * std::numbers::pi;
    }
    else if(sweep && sweep_angle < 0.0)
    {
        sweep_angle += 2.0 * std::numbers::pi;
    }
    const int segments =
        std::max(1, static_cast<int>(std::ceil(std::abs(sweep_angle) / (std::numbers::pi / 2.0) - 1e-9)));
    const double step = sweep_angle / segments;
    const double handle = 4.0 / 3.0 * std::tan(step / 4.0);
    const auto point = [&](double a)
    {
        return QPointF(
            cx + rx * std::cos(a) * cos_phi - ry * std::sin(a) * sin_phi,
            cy + rx * std::cos(a) * sin_phi + ry * std::sin(a) * cos_phi);
    };
    const auto tangent = [&](double a)
    {
        return QPointF(
            -rx * std::sin(a) * cos_phi - ry * std::cos(a) * sin_phi,
            -rx * std::sin(a) * sin_phi + ry * std::cos(a) * cos_phi);
    };
    for(int segment = 0; segment < segments; ++segment)
    {
        const double a1 = theta + segment * step;
        const double a2 = a1 + step;
        const QPointF end = segment + 1 == segments ? to : point(a2);
        path.cubicTo(point(a1) + tangent(a1) * handle, point(a2) - tangent(a2) * handle, end);
    }
}

std::optional<QPainterPath> parse_path(const QString& data)
{
    Scanner scanner(data.toStdString());
    QPainterPath path;
    path.setFillRule(Qt::WindingFill);
    QPointF current;
    QPointF subpath_start;
    QPointF last_cubic;
    QPointF last_quad;
    char previous = 0;
    bool need_move = false;
    while(!scanner.done())
    {
        const auto command = scanner.letter();
        if(!command.has_value())
        {
            return std::nullopt;
        }
        const char lower = static_cast<char>(*command | 0x20);
        const bool relative = *command == lower;
        if(lower == 'z')
        {
            path.closeSubpath();
            current = subpath_start;
            need_move = true;
            previous = 'z';
            continue;
        }
        bool first = true;
        do
        {
            const char kind = lower == 'm' && !first ? 'l' : lower;
            const QPointF origin = relative ? current : QPointF();
            if(kind != 'm' && need_move)
            {
                path.moveTo(current);
                need_move = false;
            }
            const auto pair = [&scanner, &origin]() -> std::optional<QPointF>
            {
                const auto x = scanner.number();
                const auto y = x.has_value() ? scanner.number() : std::nullopt;
                if(!y.has_value())
                {
                    return std::nullopt;
                }
                return origin + QPointF(*x, *y);
            };
            switch(kind)
            {
                case 'm':
                {
                    const auto to = pair();
                    if(!to)
                    {
                        return std::nullopt;
                    }
                    path.moveTo(*to);
                    current = subpath_start = *to;
                    need_move = false;
                    break;
                }
                case 'l':
                {
                    const auto to = pair();
                    if(!to)
                    {
                        return std::nullopt;
                    }
                    path.lineTo(*to);
                    current = *to;
                    break;
                }
                case 'h':
                case 'v':
                {
                    const auto value = scanner.number();
                    if(!value)
                    {
                        return std::nullopt;
                    }
                    if(kind == 'h')
                    {
                        current.setX(*value + (relative ? current.x() : 0.0));
                    }
                    else
                    {
                        current.setY(*value + (relative ? current.y() : 0.0));
                    }
                    path.lineTo(current);
                    break;
                }
                case 'c':
                case 's':
                {
                    std::optional<QPointF> first_control;
                    if(kind == 'c')
                    {
                        first_control = pair();
                    }
                    else
                    {
                        first_control = previous == 'c' || previous == 's'
                            ? reflect(last_cubic, current)
                            : current;
                    }
                    const auto second_control = first_control ? pair() : std::nullopt;
                    const auto to = second_control ? pair() : std::nullopt;
                    if(!to)
                    {
                        return std::nullopt;
                    }
                    path.cubicTo(*first_control, *second_control, *to);
                    last_cubic = *second_control;
                    current = *to;
                    break;
                }
                case 'q':
                case 't':
                {
                    std::optional<QPointF> control;
                    if(kind == 'q')
                    {
                        control = pair();
                    }
                    else
                    {
                        control = previous == 'q' || previous == 't'
                            ? reflect(last_quad, current)
                            : current;
                    }
                    const auto to = control ? pair() : std::nullopt;
                    if(!to)
                    {
                        return std::nullopt;
                    }
                    path.quadTo(*control, *to);
                    last_quad = *control;
                    current = *to;
                    break;
                }
                case 'a':
                {
                    const auto rx = scanner.number();
                    const auto ry = rx ? scanner.number() : std::nullopt;
                    const auto rotation = ry ? scanner.number() : std::nullopt;
                    const auto large_arc = rotation ? scanner.flag() : std::nullopt;
                    const auto sweep = large_arc ? scanner.flag() : std::nullopt;
                    const auto to = sweep ? pair() : std::nullopt;
                    if(!to)
                    {
                        return std::nullopt;
                    }
                    arc_to(path, current, *rx, *ry, *rotation, *large_arc, *sweep, *to);
                    current = *to;
                    break;
                }
                default:
                    return std::nullopt;
            }
            previous = kind;
            first = false;
        } while(scanner.at_number());
    }
    return path;
}

std::optional<std::vector<double>> numbers(const QString& text)
{
    Scanner scanner(text.toStdString());
    std::vector<double> result;
    while(!scanner.done())
    {
        const auto value = scanner.number();
        if(!value)
        {
            return std::nullopt;
        }
        result.push_back(*value);
    }
    return result;
}

std::optional<QColor> hex_colour(const QString& text)
{
    if(!text.startsWith(QLatin1Char('#')) || (text.size() != 4 && text.size() != 7))
    {
        return std::nullopt;
    }
    bool ok = false;
    const unsigned value = text.mid(1).toUInt(&ok, 16);
    if(!ok)
    {
        return std::nullopt;
    }
    if(text.size() == 4)
    {
        const auto digit = [value](int shift) { return static_cast<int>((value >> shift) & 0xF) * 17; };
        return QColor(digit(8), digit(4), digit(0));
    }
    return QColor(static_cast<int>(value >> 16) & 0xFF, static_cast<int>(value >> 8) & 0xFF,
                  static_cast<int>(value) & 0xFF);
}

std::optional<Paint> paint(const QString& text)
{
    const QString value = text.trimmed();
    if(value == QLatin1String("none"))
    {
        return Paint {true, false, QColor()};
    }
    if(value == QLatin1String("currentColor"))
    {
        return Paint {false, true, QColor()};
    }
    const auto colour = hex_colour(value);
    if(!colour)
    {
        return std::nullopt;
    }
    return Paint {false, false, *colour};
}

/// "translate(64 60) skewX(-8)": each function applies to points before the one on its left.
std::optional<QTransform> transform(const QString& text)
{
    QTransform result;
    QString rest = text.trimmed();
    while(!rest.isEmpty())
    {
        const auto open = rest.indexOf(QLatin1Char('('));
        const auto close = rest.indexOf(QLatin1Char(')'));
        if(open <= 0 || close < open)
        {
            return std::nullopt;
        }
        const QString name = rest.left(open).trimmed();
        const auto values = numbers(rest.mid(open + 1, close - open - 1));
        if(!values)
        {
            return std::nullopt;
        }
        const auto& v = *values;
        QTransform step;
        if(name == QLatin1String("translate") && (v.size() == 1 || v.size() == 2))
        {
            step.translate(v[0], v.size() == 2 ? v[1] : 0.0);
        }
        else if(name == QLatin1String("scale") && (v.size() == 1 || v.size() == 2))
        {
            step.scale(v[0], v.size() == 2 ? v[1] : v[0]);
        }
        else if(name == QLatin1String("rotate") && (v.size() == 1 || v.size() == 3))
        {
            if(v.size() == 3)
            {
                step.translate(v[1], v[2]);
            }
            step.rotate(v[0]);
            if(v.size() == 3)
            {
                step.translate(-v[1], -v[2]);
            }
        }
        else if(name == QLatin1String("skewX") && v.size() == 1)
        {
            step = QTransform(1.0, 0.0, std::tan(v[0] * std::numbers::pi / 180.0), 1.0, 0.0, 0.0);
        }
        else if(name == QLatin1String("skewY") && v.size() == 1)
        {
            step = QTransform(1.0, std::tan(v[0] * std::numbers::pi / 180.0), 0.0, 1.0, 0.0, 0.0);
        }
        else if(name == QLatin1String("matrix") && v.size() == 6)
        {
            step = QTransform(v[0], v[1], v[2], v[3], v[4], v[5]);
        }
        else
        {
            return std::nullopt;
        }
        result = step * result;
        rest = rest.mid(close + 1).trimmed();
        if(rest.startsWith(QLatin1Char(',')))
        {
            rest = rest.mid(1).trimmed();
        }
    }
    return result;
}

double attribute_number(const QXmlStreamAttributes& attributes, const char* name, bool& ok)
{
    const QString text = attributes.value(QLatin1String(name)).toString();
    if(text.isEmpty())
    {
        return 0.0;
    }
    const auto values = numbers(text);
    if(!values || values->size() != 1)
    {
        ok = false;
        return 0.0;
    }
    return values->front();
}

/// Applies the presentation attributes the icons use; false on a value this renderer cannot draw.
bool apply_style(const QXmlStreamAttributes& attributes, Style& style)
{
    if(attributes.hasAttribute(QLatin1String("style")))
    {
        return false;
    }
    if(attributes.hasAttribute(QLatin1String("fill")))
    {
        const auto value = paint(attributes.value(QLatin1String("fill")).toString());
        if(!value)
        {
            return false;
        }
        style.fill = *value;
    }
    if(attributes.hasAttribute(QLatin1String("stroke")))
    {
        const auto value = paint(attributes.value(QLatin1String("stroke")).toString());
        if(!value)
        {
            return false;
        }
        style.stroke = *value;
    }
    if(attributes.hasAttribute(QLatin1String("stroke-width")))
    {
        bool ok = true;
        style.stroke_width = attribute_number(attributes, "stroke-width", ok);
        if(!ok || style.stroke_width < 0.0)
        {
            return false;
        }
    }
    if(attributes.hasAttribute(QLatin1String("stroke-linecap")))
    {
        const QString value = attributes.value(QLatin1String("stroke-linecap")).toString();
        if(value == QLatin1String("round"))
        {
            style.cap = Qt::RoundCap;
        }
        else if(value == QLatin1String("square"))
        {
            style.cap = Qt::SquareCap;
        }
        else if(value == QLatin1String("butt"))
        {
            style.cap = Qt::FlatCap;
        }
        else
        {
            return false;
        }
    }
    if(attributes.hasAttribute(QLatin1String("stroke-linejoin")))
    {
        const QString value = attributes.value(QLatin1String("stroke-linejoin")).toString();
        if(value == QLatin1String("round"))
        {
            style.join = Qt::RoundJoin;
        }
        else if(value == QLatin1String("bevel"))
        {
            style.join = Qt::BevelJoin;
        }
        else if(value == QLatin1String("miter"))
        {
            style.join = Qt::SvgMiterJoin;
        }
        else
        {
            return false;
        }
    }
    if(attributes.hasAttribute(QLatin1String("transform")))
    {
        const auto value = transform(attributes.value(QLatin1String("transform")).toString());
        if(!value)
        {
            return false;
        }
        style.transform = *value * style.transform;
    }
    return true;
}

std::optional<QPainterPath> shape(const QXmlStreamReader& reader)
{
    const auto attributes = reader.attributes();
    bool ok = true;
    const auto number = [&attributes, &ok](const char* name)
    {
        return attribute_number(attributes, name, ok);
    };
    QPainterPath path;
    path.setFillRule(Qt::WindingFill);
    const auto name = reader.name();
    if(name == QLatin1String("path"))
    {
        return parse_path(attributes.value(QLatin1String("d")).toString());
    }
    if(name == QLatin1String("circle"))
    {
        const double r = number("r");
        path.addEllipse(QPointF(number("cx"), number("cy")), r, r);
    }
    else if(name == QLatin1String("rect"))
    {
        const QRectF box(number("x"), number("y"), number("width"), number("height"));
        double rx = number("rx");
        double ry = number("ry");
        const bool has_rx = attributes.hasAttribute(QLatin1String("rx"));
        const bool has_ry = attributes.hasAttribute(QLatin1String("ry"));
        if(has_rx && !has_ry)
        {
            ry = rx;
        }
        else if(has_ry && !has_rx)
        {
            rx = ry;
        }
        rx = std::min(rx, box.width() / 2.0);
        ry = std::min(ry, box.height() / 2.0);
        if(rx > 0.0 && ry > 0.0)
        {
            path.addRoundedRect(box, rx, ry);
        }
        else
        {
            path.addRect(box);
        }
    }
    else if(name == QLatin1String("line"))
    {
        path.moveTo(number("x1"), number("y1"));
        path.lineTo(number("x2"), number("y2"));
    }
    else if(name == QLatin1String("polyline") || name == QLatin1String("polygon"))
    {
        const auto points = numbers(attributes.value(QLatin1String("points")).toString());
        if(!points || points->size() < 4 || points->size() % 2 != 0)
        {
            return std::nullopt;
        }
        path.moveTo((*points)[0], (*points)[1]);
        for(std::size_t index = 2; index < points->size(); index += 2)
        {
            path.lineTo((*points)[index], (*points)[index + 1]);
        }
        if(name == QLatin1String("polygon"))
        {
            path.closeSubpath();
        }
    }
    else
    {
        return std::nullopt;
    }
    if(!ok)
    {
        return std::nullopt;
    }
    return path;
}

struct Document
{
    QRectF view_box;
    std::vector<Shape> shapes;
};

std::optional<Document> parse(std::string_view svg)
{
    QXmlStreamReader reader(QByteArray(svg.data(), static_cast<int>(svg.size())));
    Document document;
    std::vector<Style> styles;
    while(!reader.atEnd())
    {
        const auto token = reader.readNext();
        if(token == QXmlStreamReader::EndElement)
        {
            if(styles.empty())
            {
                return std::nullopt;
            }
            styles.pop_back();
            continue;
        }
        if(token != QXmlStreamReader::StartElement)
        {
            continue;
        }
        Style style = styles.empty() ? Style {} : styles.back();
        if(!apply_style(reader.attributes(), style))
        {
            return std::nullopt;
        }
        const auto name = reader.name();
        if(styles.empty())
        {
            if(name != QLatin1String("svg"))
            {
                return std::nullopt;
            }
            const auto attributes = reader.attributes();
            const auto box = numbers(attributes.value(QLatin1String("viewBox")).toString());
            if(box && box->size() == 4)
            {
                document.view_box = QRectF((*box)[0], (*box)[1], (*box)[2], (*box)[3]);
            }
            else
            {
                bool ok = true;
                document.view_box = QRectF(
                    0.0,
                    0.0,
                    attribute_number(attributes, "width", ok),
                    attribute_number(attributes, "height", ok));
                if(!ok)
                {
                    return std::nullopt;
                }
            }
            if(document.view_box.width() <= 0.0 || document.view_box.height() <= 0.0)
            {
                return std::nullopt;
            }
        }
        else if(name != QLatin1String("g"))
        {
            auto path = shape(reader);
            if(!path)
            {
                return std::nullopt;
            }
            document.shapes.push_back({std::move(*path), style});
        }
        styles.push_back(style);
    }
    if(reader.hasError() || document.view_box.isEmpty())
    {
        return std::nullopt;
    }
    return document;
}

QColor resolve(const Paint& paint, const QColor& current)
{
    return paint.current ? current : paint.colour;
}

} // namespace

bool paint_svg(QPainter& painter, std::string_view svg, const QRectF& target, const QColor& current)
{
    const auto document = parse(svg);
    if(!document || target.isEmpty())
    {
        return false;
    }
    // preserveAspectRatio="xMidYMid meet", the SVG default.
    const QRectF& box = document->view_box;
    const double scale = std::min(target.width() / box.width(), target.height() / box.height());
    QTransform view;
    view.translate(
        target.x() + (target.width() - box.width() * scale) / 2.0,
        target.y() + (target.height() - box.height() * scale) / 2.0);
    view.scale(scale, scale);
    view.translate(-box.x(), -box.y());

    painter.save();
    const QTransform base = painter.worldTransform();
    painter.setRenderHint(QPainter::Antialiasing, true);
    for(const auto& item : document->shapes)
    {
        painter.setWorldTransform(item.style.transform * view * base);
        if(!item.style.fill.none)
        {
            painter.fillPath(item.path, resolve(item.style.fill, current));
        }
        if(!item.style.stroke.none && item.style.stroke_width > 0.0)
        {
            QPen pen(resolve(item.style.stroke, current), item.style.stroke_width);
            pen.setCapStyle(item.style.cap);
            pen.setJoinStyle(item.style.join);
            pen.setMiterLimit(4.0);
            painter.strokePath(item.path, pen);
        }
    }
    painter.restore();
    return true;
}

std::string_view embedded_icon(std::string_view name)
{
    for(const auto& icon : generated::embedded_icons)
    {
        if(icon.name == name)
        {
            return icon.svg;
        }
    }
    return {};
}

QPixmap icon_pixmap(std::string_view name, int size, qreal device_pixel_ratio, const QColor& current)
{
    const auto svg = embedded_icon(name);
    const int pixels = std::max(1, static_cast<int>(std::lround(size * device_pixel_ratio)));
    if(svg.empty())
    {
        return {};
    }
    QImage image(pixels, pixels, QImage::Format_ARGB32_Premultiplied);
    image.fill(Qt::transparent);
    {
        QPainter painter(&image);
        if(!paint_svg(painter, svg, QRectF(0, 0, pixels, pixels), current))
        {
            return {};
        }
    }
    auto pixmap = QPixmap::fromImage(image);
    pixmap.setDevicePixelRatio(device_pixel_ratio);
    return pixmap;
}

} // namespace hyperflux::openrgb::native
