// SPDX-License-Identifier: GPL-2.0-only

#include "plugin_widget.hpp"

#include "svg_icon.hpp"

#include <QApplication>
#include <QDesktopServices>
#include <QEvent>
#include <QFileInfo>
#include <QFontDatabase>
#include <QFrame>
#include <QGridLayout>
#include <QHBoxLayout>
#include <QLabel>
#include <QPainter>
#include <QPushButton>
#include <QScrollArea>
#include <QStackedWidget>
#include <QTabBar>
#include <QTabWidget>
#include <QTimer>
#include <QUrl>
#include <QVariant>
#include <QVBoxLayout>

#include <algorithm>
#include <cmath>
#include <utility>

namespace hyperflux::openrgb::native
{
namespace
{

// ------------------------------------------------------------------ colour
// The page takes its neutrals from the host palette and its meanings from
// Tailwind: the 400 shades on dark windows and the 700 shades on light ones,
// each at least 4.4:1 on the window and 5:1 on a card.

struct Tones
{
    QColor good;
    QColor warn;
    QColor bad;
    QColor wireless;
};

bool is_dark(const QPalette& palette)
{
    return palette.color(QPalette::Window).lightness() < 128;
}

Tones tones(const QPalette& palette)
{
    if(is_dark(palette))
    {
        return {QColor(0x4A, 0xDE, 0x80), QColor(0xFB, 0x92, 0x3C), QColor(0xF8, 0x71, 0x71),
                QColor(0x60, 0xA5, 0xFA)};
    }
    return {QColor(0x15, 0x80, 0x3D), QColor(0xC2, 0x41, 0x0C), QColor(0xB9, 0x1C, 0x1C),
            QColor(0x1D, 0x4E, 0xD8)};
}

QColor mix(const QColor& from, const QColor& to, double amount)
{
    return QColor::fromRgbF(
        static_cast<float>(from.redF() * (1.0 - amount) + to.redF() * amount),
        static_cast<float>(from.greenF() * (1.0 - amount) + to.greenF() * amount),
        static_cast<float>(from.blueF() * (1.0 - amount) + to.blueF() * amount));
}

QString css(const QColor& colour)
{
    return colour.name(QColor::HexRgb);
}

/// WCAG 2 contrast ratio.
double contrast(const QColor& first, const QColor& second)
{
    const auto luminance = [](const QColor& colour)
    {
        const auto channel = [](double value)
        {
            return value <= 0.03928 ? value / 12.92 : std::pow((value + 0.055) / 1.055, 2.4);
        };
        return 0.2126 * channel(colour.redF()) + 0.7152 * channel(colour.greenF())
            + 0.0722 * channel(colour.blueF());
    };
    const double a = luminance(first);
    const double b = luminance(second);
    return (std::max(a, b) + 0.05) / (std::min(a, b) + 0.05);
}

/// Every colour the page draws with, computed once per render from the palette.
struct Colours
{
    Tones tone;
    QColor ink;
    QColor window;
    QColor card;
    QColor muted;
    QColor border;
    QColor wired;
    /// The HyperFlux cyan: Tailwind cyan-400 on dark windows, cyan-700 on light ones.
    QColor brand;
    double tint;
    bool dark;
};

Colours colours(const QPalette& palette)
{
    Colours result {};
    result.tone = tones(palette);
    result.ink = palette.color(QPalette::WindowText);
    result.window = palette.color(QPalette::Window);
    // A translucent view colour (Kvantum) is flattened onto the window.
    const QColor base = palette.color(QPalette::Base);
    result.card = base.alpha() < 255
        ? mix(result.window, QColor(base.red(), base.green(), base.blue()), base.alphaF())
        : base;
    // Text mixed 38 % toward the window, pulled back until it reaches 4.5:1 on any scheme.
    result.muted = result.ink;
    for(double amount = 0.38; amount > 0.0; amount -= 0.02)
    {
        const QColor candidate = mix(result.ink, result.window, amount);
        if(contrast(candidate, result.window) >= 4.5)
        {
            result.muted = candidate;
            break;
        }
    }
    result.border = mix(result.card, result.ink, 0.12);
    result.wired = mix(result.ink, result.card, 0.45);
    result.dark = is_dark(palette);
    result.brand = result.dark ? QColor(0x22, 0xD3, 0xEE) : QColor(0x0E, 0x74, 0x90);
    result.tint = result.dark ? 0.15 : 0.12;
    return result;
}

QColor tone_colour(const Colours& colours, PluginHealthTone tone)
{
    switch(tone)
    {
        case PluginHealthTone::Positive: return colours.tone.good;
        case PluginHealthTone::Warning: return colours.tone.warn;
        case PluginHealthTone::Negative: return colours.tone.bad;
        case PluginHealthTone::Neutral: return colours.wired;
    }
    return colours.wired;
}

// ------------------------------------------------------------------ shape and type
// One radius (KDE Kirigami's 5 px) and four gaps on an 18 px grid.
constexpr int small_gap = 4;
constexpr int medium_gap = 6;
constexpr int large_gap = 8;
constexpr int grid_unit = 18;
constexpr int radius = 5;

/// Skins set "* { font-family; font-size }", which beats setFont(); a
/// widget's own sheet beats the skin, so each label pins its font there.
void pin_font(QWidget* widget, const QString& extra)
{
    const QFont font = widget->font();
    const QString size = font.pixelSize() > 0
        ? QString("%1px").arg(font.pixelSize())
        : QString("%1pt").arg(font.pointSizeF());
    widget->setStyleSheet(
        QString("font-family: \"%1\"; font-size: %2; font-weight: %3; background: transparent; border: none; %4")
            .arg(font.family(), size, font.bold() ? QString("bold") : QString("normal"), extra));
}

/// Scales a font whether its size is in points or pixels.
void scale(QFont& font, double factor, double add_points)
{
    if(font.pixelSize() > 0)
    {
        font.setPixelSize(std::max(1, static_cast<int>(std::lround(font.pixelSize() * factor + add_points * 4.0 / 3.0))));
    }
    else
    {
        font.setPointSizeF(std::max(1.0, font.pointSizeF() * factor + add_points));
    }
}

enum class Text
{
    Body,
    Muted,
    Name,
    Title,
    Percent,
};

QLabel* text(const QString& value, const Colours& colours, Text kind = Text::Body, QColor colour = QColor())
{
    auto* label = new QLabel(value);
    label->setTextFormat(Qt::PlainText);
    QFont font = QApplication::font();
    switch(kind)
    {
        case Text::Body:
        case Text::Muted: break;
        case Text::Name: scale(font, 1.0, 2.0); font.setBold(true); break;
        case Text::Title: scale(font, 1.0, 5.0); font.setBold(true); break;
        case Text::Percent: scale(font, 1.6, 0.0); font.setBold(true); break;
    }
    label->setFont(font);
    if(!colour.isValid())
    {
        colour = kind == Text::Muted ? colours.muted : colours.ink;
    }
    pin_font(label, QString("color: %1;").arg(css(colour)));
    return label;
}

// ------------------------------------------------------------------ building blocks

/// A Lucide icon drawn as vectors at the screen's own pixel density.
class Icon final : public QWidget
{
public:
    Icon(std::string_view name, int size, QColor colour, const QString& accessible_name = QString())
        : name_(name), colour_(std::move(colour))
    {
        setFixedSize(size, size);
        setAttribute(Qt::WA_TransparentForMouseEvents);
        setObjectName("hyperfluxIcon");
        setProperty("icon", QString::fromUtf8(name.data(), static_cast<int>(name.size())));
        if(!accessible_name.isEmpty())
        {
            setAccessibleName(accessible_name);
        }
    }

protected:
    void paintEvent(QPaintEvent* /*event*/) override
    {
        QPainter painter(this);
        paint_svg(painter, embedded_icon(name_), QRectF(rect()), colour_);
    }

private:
    std::string_view name_;
    QColor colour_;
};

QHBoxLayout* row_layout(int spacing)
{
    auto* layout = new QHBoxLayout();
    layout->setContentsMargins(0, 0, 0, 0);
    layout->setSpacing(spacing);
    return layout;
}

QFrame* frame(const char* name, const QColor& background, const QColor& border)
{
    auto* result = new QFrame();
    result->setObjectName(name);
    result->setStyleSheet(QString("QFrame#%1 { background: %2; border: 1px solid %3; border-radius: %4px; }")
                              .arg(name, css(background), css(border))
                              .arg(radius));
    return result;
}

/// 22 px tall: a tint of the meaning colour, the word in text colour, the icon in the meaning colour.
QFrame* chip(
    const QString& word,
    std::string_view icon,
    const QColor& tone,
    const Colours& colours,
    const char* name = "hyperfluxChip")
{
    const QColor fill = mix(colours.card, tone, colours.tint);
    auto* result = frame(name, fill, fill);
    result->setFixedHeight(22);
    result->setSizePolicy(QSizePolicy::Fixed, QSizePolicy::Fixed);
    result->setProperty("word", word);
    auto* layout = row_layout(small_gap);
    layout->setContentsMargins(large_gap, 0, large_gap, 0);
    result->setLayout(layout);
    layout->addWidget(new Icon(icon, 16, tone), 0, Qt::AlignVCenter);
    layout->addWidget(text(word, colours), 0, Qt::AlignVCenter);
    return result;
}

QString from(std::string_view value)
{
    return QString::fromUtf8(value.data(), static_cast<int>(value.size()));
}

QPushButton* help_button(const Colours& colours)
{
    auto* button = new QPushButton();
    button->setObjectName("hyperfluxHelp");
    button->setAccessibleName("Help");
    auto* layout = row_layout(medium_gap);
    layout->setContentsMargins(large_gap + 2, small_gap, large_gap + 2, small_gap);
    button->setLayout(layout);
    layout->addWidget(new Icon("circle-help", 16, colours.ink), 0, Qt::AlignVCenter);
    auto* word = text("Help", colours);
    word->setAttribute(Qt::WA_TransparentForMouseEvents);
    layout->addWidget(word, 0, Qt::AlignVCenter);
    // The trailing arrow says the guide opens outside OpenRGB.
    layout->addWidget(new Icon("external-link", 16, colours.muted), 0, Qt::AlignVCenter);
    const QSize size = layout->sizeHint().expandedTo(QSize(0, 30));
    button->setFixedSize(size);
    // Its own sheet with a pinned size: a skin's "QPushButton { min-width }" cannot resize or restyle it.
    button->setStyleSheet(
        QString("QPushButton#hyperfluxHelp { background: %1; border: 1px solid %2; border-radius: %3px; padding: 0;"
                " min-width: %4px; max-width: %4px; min-height: %5px; max-height: %5px; }"
                "QPushButton#hyperfluxHelp:hover { background: %6; }"
                "QPushButton#hyperfluxHelp:pressed { background: %2; }")
            .arg(css(mix(colours.card, colours.ink, 0.06)), css(mix(colours.card, colours.ink, 0.18)))
            .arg(radius)
            .arg(size.width() - 2)
            .arg(size.height() - 2)
            .arg(css(mix(colours.card, colours.ink, 0.11))));
    button->setCursor(Qt::PointingHandCursor);
    const QUrl target = help_target(from(installed_help_path));
    button->setToolTip(target.isLocalFile() ? target.toLocalFile() : target.toString());
    QObject::connect(button, &QPushButton::clicked, button, [target] {
        // A desktop with no handler for Markdown files still gets the guide, from the web.
        if(!QDesktopServices::openUrl(target) && target.isLocalFile())
        {
            QDesktopServices::openUrl(QUrl(from(online_help_url)));
        }
    });
    return button;
}

/// Lucide draws 0-3 bars; each bar stands for about a third, so the picture agrees with the number.
std::string_view battery_icon(int percent)
{
    if(percent <= 10)
    {
        return "battery";
    }
    if(percent <= 40)
    {
        return "battery-low";
    }
    return percent <= 75 ? "battery-medium" : "battery-full";
}

QColor battery_tone(int percent, const Colours& colours)
{
    if(percent <= 10)
    {
        return colours.tone.bad;
    }
    return percent <= 20 ? colours.tone.warn : colours.tone.good;
}

std::string_view type_icon(const std::string& type)
{
    if(type == "Mouse")
    {
        return "mouse";
    }
    if(type == "Keyboard")
    {
        return "keyboard";
    }
    return "hyperflux-symbolic";
}

QString from(const std::string& value)
{
    return QString::fromStdString(value);
}

QFrame* device_card(const PluginDeviceRow& device, const Colours& colours)
{
    auto* card = frame("hyperfluxDeviceCard", colours.card, colours.border);
    card->setProperty("stableId", QVariant(from(device.stable_id)));
    auto* layout = new QVBoxLayout(card);
    layout->setContentsMargins(grid_unit, grid_unit - 2, grid_unit, grid_unit - 2);
    layout->setSpacing(large_gap);

    // Who: the type icon (it names the type for screen readers), the name, and how it is connected.
    auto* head = row_layout(large_gap + 2);
    const QColor tile_colour = mix(colours.card, colours.ink, 0.06);
    auto* tile = frame("hyperfluxDeviceTile", tile_colour, tile_colour);
    tile->setFixedSize(40, 40);
    auto* tile_layout = row_layout(0);
    tile->setLayout(tile_layout);
    tile_layout->addWidget(new Icon(type_icon(device.type), 22, colours.ink, from(device.type)), 0, Qt::AlignCenter);
    head->addWidget(tile, 0, Qt::AlignVCenter);
    auto* name = text(from(device.device), colours, Text::Name);
    name->setObjectName("hyperfluxDeviceName");
    name->setWordWrap(true);
    head->addWidget(name, 1, Qt::AlignVCenter);
    if(device.connection == "Wired")
    {
        head->addWidget(chip("Wired", "cable", colours.wired, colours), 0, Qt::AlignVCenter);
    }
    else if(device.connection == "Wireless")
    {
        head->addWidget(chip("Wireless", "wifi", colours.tone.wireless, colours), 0, Qt::AlignVCenter);
    }
    layout->addLayout(head);
    layout->addSpacing(small_gap);

    // How much: the one number people come for, with an icon that agrees with it.
    auto* reading = row_layout(large_gap);
    if(device.battery_percent.has_value())
    {
        const int percent = *device.battery_percent;
        reading->addWidget(new Icon(battery_icon(percent), 22, battery_tone(percent, colours)), 0, Qt::AlignVCenter);
        auto* number = text(QString("%1%").arg(percent), colours, Text::Percent);
        number->setObjectName("hyperfluxBatteryPercent");
        reading->addWidget(number, 0, Qt::AlignVCenter);
    }
    else
    {
        auto* missing = text("Battery level not reported yet", colours, Text::Muted);
        missing->setObjectName("hyperfluxBatteryPercent");
        reading->addWidget(missing, 0, Qt::AlignVCenter);
    }
    reading->addSpacing(small_gap);
    if(!device.charge.empty())
    {
        reading->addWidget(chip(from(device.charge), "plug-zap", colours.tone.good, colours), 0, Qt::AlignVCenter);
    }
    if(device.presence == "Asleep")
    {
        reading->addWidget(chip("Asleep", "moon", colours.wired, colours), 0, Qt::AlignVCenter);
    }
    else if(!device.presence.empty())
    {
        reading->addWidget(chip(from(device.presence), "wifi-off", colours.tone.warn, colours), 0, Qt::AlignVCenter);
    }
    if(!device.stale.empty())
    {
        reading->addWidget(chip(from(device.stale), "clock", colours.tone.warn, colours), 0, Qt::AlignVCenter);
    }
    reading->addStretch(1);
    layout->addLayout(reading);
    if(!device.note.empty())
    {
        auto* note = text(from(device.note), colours, Text::Muted);
        note->setObjectName("hyperfluxDeviceNote");
        note->setWordWrap(true);
        layout->addWidget(note);
    }
    layout->addStretch(1); // a taller neighbour must not float this card's content
    return card;
}

QFrame* attention_banner(const PluginAttention& attention, const Colours& colours)
{
    const QColor tone = tone_colour(colours, attention.tone);
    auto* banner = frame("hyperfluxAttention", mix(colours.window, tone, colours.tint), mix(colours.window, tone, 0.45));
    auto* layout = row_layout(large_gap + 2);
    layout->setContentsMargins(grid_unit - 4, large_gap + 2, grid_unit - 4, large_gap + 2);
    banner->setLayout(layout);
    layout->addWidget(new Icon("triangle-alert", 16, tone), 0, Qt::AlignTop);
    auto* message = text(QString(), colours);
    message->setObjectName("hyperfluxAttentionText");
    QString html = from(attention.message).toHtmlEscaped();
    if(!attention.command.empty())
    {
        // The command is something to type, so it is shown as code and can be selected.
        const QString command = from(attention.command).toHtmlEscaped();
        const QString family = QFontDatabase::systemFont(QFontDatabase::FixedFont).family().toHtmlEscaped();
        html.replace(command, QString("<span style=\"font-family: '%1';\">%2</span>").arg(family, command));
    }
    message->setTextFormat(Qt::RichText);
    message->setText(html);
    message->setWordWrap(true);
    message->setTextInteractionFlags(Qt::TextSelectableByMouse);
    layout->addWidget(message, 1);
    return banner;
}

QWidget* build_page(const PluginInformationViewModel& model, const Colours& colours)
{
    auto* root = new QWidget();
    root->setObjectName("hyperfluxPage");
    root->setAttribute(Qt::WA_StyledBackground, false);
    auto* page = new QVBoxLayout(root);
    page->setContentsMargins(grid_unit, grid_unit, grid_unit, grid_unit);
    page->setSpacing(grid_unit);

    // Header: the mark and name identify the page, one chip gives the overall state, one button gets help.
    auto* head = row_layout(large_gap + 4);
    if(colours.dark)
    {
        head->addWidget(new Icon("hyperflux-mark", 44, QColor(), "HyperFlux Next"), 0, Qt::AlignVCenter);
    }
    else
    {
        // The mark's slate tile would be the only dark block on a light page,
        // so light themes get the device tiles' neutral with the glyph in cyan.
        const QColor tile_colour = mix(colours.window, colours.ink, 0.06);
        auto* tile = frame("hyperfluxMarkTile", tile_colour, tile_colour);
        tile->setFixedSize(44, 44);
        auto* tile_layout = row_layout(0);
        tile->setLayout(tile_layout);
        tile_layout->addWidget(new Icon("hyperflux-symbolic", 26, colours.brand, "HyperFlux Next"), 0, Qt::AlignCenter);
        head->addWidget(tile, 0, Qt::AlignVCenter);
    }
    head->addWidget(text("HyperFlux Next", colours, Text::Title), 0, Qt::AlignVCenter);
    head->addSpacing(medium_gap);
    std::string_view state_icon = "circle-pause";
    switch(model.tone)
    {
        case PluginHealthTone::Positive: state_icon = "circle-check"; break;
        case PluginHealthTone::Warning: state_icon = "refresh-cw"; break;
        case PluginHealthTone::Negative: state_icon = "triangle-alert"; break;
        case PluginHealthTone::Neutral: break;
    }
    auto* state = chip(from(model.headline), state_icon, tone_colour(colours, model.tone), colours, "hyperfluxHealth");
    if(model.technical_detail.has_value())
    {
        state->setToolTip(from(*model.technical_detail));
    }
    head->addWidget(state, 0, Qt::AlignVCenter);
    head->addStretch(1);
    head->addWidget(help_button(colours), 0, Qt::AlignVCenter);
    page->addLayout(head);

    // One place says the readings are old, instead of a label on every card.
    if(model.attention.has_value())
    {
        page->addWidget(attention_banner(*model.attention, colours));
    }

    if(model.empty_state.has_value())
    {
        auto* empty = text(from(*model.empty_state), colours, Text::Muted);
        empty->setObjectName("hyperfluxEmptyState");
        page->addWidget(empty);
    }
    else if(!model.devices.empty())
    {
        auto* cards = new QWidget();
        cards->setObjectName("hyperfluxDeviceCards");
        auto* grid = new QGridLayout(cards);
        grid->setContentsMargins(0, 0, 0, 0);
        grid->setHorizontalSpacing(grid_unit);
        grid->setVerticalSpacing(grid_unit);
        grid->setColumnStretch(0, 1);
        grid->setColumnStretch(1, 1);
        for(std::size_t index = 0; index < model.devices.size(); ++index)
        {
            grid->addWidget(
                device_card(model.devices[index], colours),
                static_cast<int>(index / 2),
                static_cast<int>(index % 2));
        }
        page->addWidget(cards);

        // The one lighting fact this page adds: where lighting is changed.
        auto* lighting = new QWidget();
        lighting->setObjectName("hyperfluxLighting");
        auto* line = row_layout(small_gap);
        lighting->setLayout(line);
        line->addWidget(new Icon("lightbulb", 16, colours.muted), 0, Qt::AlignVCenter);
        line->addWidget(
            text("Colours and effects for these devices are set in the Devices tab.", colours, Text::Muted),
            0,
            Qt::AlignVCenter);
        line->addStretch(1);
        page->addWidget(lighting);
    }
    page->addStretch(1);
    return root;
}

} // namespace

QUrl help_target(const QString& installed_path)
{
    if(QFileInfo(installed_path).isReadable())
    {
        return QUrl::fromLocalFile(installed_path);
    }
    return QUrl(from(online_help_url));
}

PluginInformationWidget::PluginInformationWidget(
    ModelProvider provider,
    QWidget* parent)
    : QWidget(parent),
      provider_(std::move(provider)),
      page_(nullptr),
      timer_(new QTimer(this))
{
    setObjectName("hyperfluxNextInformation");
    auto* outer = new QVBoxLayout(this);
    outer->setContentsMargins(0, 0, 0, 0);
    auto* scroll = new QScrollArea(this);
    scroll->setObjectName("hyperfluxScroll");
    scroll->setFrameShape(QFrame::NoFrame);
    scroll->setWidgetResizable(true);
    scroll->setStyleSheet("QScrollArea#hyperfluxScroll { background: transparent; border: none; }");
    scroll->viewport()->setAutoFillBackground(false);
    outer->addWidget(scroll);
    auto* holder = new QWidget(scroll);
    holder->setObjectName("hyperfluxPageHolder");
    holder->setAutoFillBackground(false);
    scroll->setWidget(holder);
    page_ = new QVBoxLayout(holder);
    page_->setContentsMargins(0, 0, 0, 0);

    timer_->setInterval(refresh_interval_ms);
    connect(timer_, &QTimer::timeout, this, [this] { refresh(); });
    timer_->start();

    refresh();
    // OpenRGB adds the page to its Information list after creating it.
    QTimer::singleShot(0, this, [this] { install_list_icon(); });
}

void PluginInformationWidget::refresh()
{
    if(!provider_)
    {
        return;
    }
    auto model = provider_();
    if(!has_rendered_ || model != rendered_)
    {
        render(model);
        rendered_ = std::move(model);
        has_rendered_ = true;
    }
}

void PluginInformationWidget::changeEvent(QEvent* event)
{
    QWidget::changeEvent(event);
    if(rendering_ || !has_rendered_)
    {
        return;
    }
    if((event->type() == QEvent::PaletteChange || event->type() == QEvent::ApplicationFontChange)
       && !restyle_pending_)
    {
        // Qt is still walking the old children; rebuild once it has finished.
        restyle_pending_ = true;
        QMetaObject::invokeMethod(
            this,
            [this]
            {
                restyle_pending_ = false;
                render(rendered_);
                paint_list_icon();
            },
            Qt::QueuedConnection);
    }
}

void PluginInformationWidget::render(const PluginInformationViewModel& model)
{
    rendering_ = true;
    auto* next = build_page(model, colours(palette()));
    if(content_ != nullptr)
    {
        page_->removeWidget(content_);
        delete content_;
    }
    content_ = next;
    page_->addWidget(content_);
    rendering_ = false;
}

void PluginInformationWidget::install_list_icon()
{
    // OpenRGB shows a generic puzzle-piece glyph for every plugin in the list.
    // The page swaps in its own mark on that label and changes nothing else;
    // if OpenRGB's layout ever differs, the glyph simply stays.
    QWidget* child = this;
    for(QWidget* ancestor = parentWidget(); ancestor != nullptr; ancestor = ancestor->parentWidget())
    {
        auto* stack = qobject_cast<QStackedWidget*>(ancestor);
        auto* tabs = stack == nullptr ? nullptr : qobject_cast<QTabWidget*>(stack->parentWidget());
        if(tabs != nullptr)
        {
            const int index = tabs->indexOf(child);
            QWidget* button = index < 0 ? nullptr : tabs->tabBar()->tabButton(index, QTabBar::LeftSide);
            list_icon_ = button == nullptr ? nullptr : button->findChild<QLabel*>("icon");
            paint_list_icon();
            return;
        }
        child = ancestor;
    }
}

void PluginInformationWidget::paint_list_icon()
{
    if(list_icon_.isNull())
    {
        return;
    }
    // Symbolic, in the list's own text colour, like the glyphs around it.
    const QColor colour = list_icon_->palette().color(QPalette::WindowText);
    const QPixmap pixmap = icon_pixmap("hyperflux-symbolic", 22, list_icon_->devicePixelRatioF(), colour);
    if(!pixmap.isNull())
    {
        list_icon_->setPixmap(pixmap);
    }
}

} // namespace hyperflux::openrgb::native
