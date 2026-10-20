// SPDX-License-Identifier: GPL-2.0-only

#include "plugin_widget.hpp"
#include "svg_icon.hpp"

#include <QApplication>
#include <QFrame>
#include <QImage>
#include <QLabel>
#include <QPainter>
#include <QPushButton>
#include <QTabBar>
#include <QTabWidget>
#include <QTimer>
#include <QUrl>
#include <QVBoxLayout>

#include <array>
#include <cstdlib>
#include <iostream>
#include <string_view>

namespace
{

int failure(int line)
{
    std::cerr << "openrgb-plugin-widget-contract failed at line " << line << '\n';
    return EXIT_FAILURE;
}

QString label_text(const QWidget* parent, const char* name)
{
    const auto* label = parent->findChild<QLabel*>(name);
    return label == nullptr ? QString("<missing>") : label->text();
}

QStringList chip_words(const QWidget* parent)
{
    QStringList words;
    for(const auto* chip : parent->findChildren<QFrame*>("hyperfluxChip"))
    {
        words << chip->property("word").toString();
    }
    return words;
}

QImage draw(std::string_view svg, int size)
{
    QImage image(size, size, QImage::Format_ARGB32_Premultiplied);
    image.fill(Qt::transparent);
    QPainter painter(&image);
    painter.setRenderHint(QPainter::Antialiasing);
    if(!hyperflux::openrgb::native::paint_svg(painter, svg, QRectF(0, 0, size, size), Qt::red))
    {
        return {};
    }
    painter.end();
    return image;
}

bool red(const QImage& image, int x, int y)
{
    const QColor colour = image.pixelColor(x, y);
    return colour.alpha() > 200 && colour.red() > 200 && colour.green() < 60;
}

bool clear(const QImage& image, int x, int y)
{
    return image.pixelColor(x, y).alpha() < 20;
}

int renderer_contract()
{
    using namespace hyperflux::openrgb::native;
    // Fills land where the view box says, scaled to the target.
    const QImage half = draw(R"svg(<svg viewBox="0 0 10 10"><rect x="0" y="0" width="5" height="10" fill="currentColor"/></svg>)svg", 20);
    if(half.isNull() || !red(half, 2, 10) || !clear(half, 17, 10))
    {
        return failure(__LINE__);
    }
    // Arcs: the upper half of a disc.
    const QImage arc = draw(R"svg(<svg viewBox="0 0 10 10"><path d="M0 5 A5 5 0 0 1 10 5 Z" fill="#f00"/></svg>)svg", 20);
    if(arc.isNull() || !red(arc, 10, 3) || !clear(arc, 10, 17) || !clear(arc, 1, 1))
    {
        return failure(__LINE__);
    }
    // Relative commands, implicit line-tos after a move, and transforms.
    const QImage relative = draw(
        R"svg(<svg viewBox="0 0 10 10"><g transform="translate(5 0)"><path d="m0 0 5 0 0 10 h-5 z" fill="#ff0000"/></g></svg>)svg",
        20);
    if(relative.isNull() || !red(relative, 15, 10) || !clear(relative, 4, 10))
    {
        return failure(__LINE__);
    }
    // Lucide strokes: the 24-unit grid, width 2, round caps.
    const QImage line = draw(
        R"svg(<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="2" y1="12" x2="22" y2="12"/></svg>)svg",
        24);
    if(line.isNull() || !red(line, 12, 12) || !clear(line, 12, 4) || !clear(line, 12, 20))
    {
        return failure(__LINE__);
    }
    // Anything it does not understand draws nothing rather than something wrong.
    if(!draw("<html/>", 16).isNull() || !draw(R"svg(<svg viewBox="0 0 10 10" style="fill:red"/>)svg", 16).isNull())
    {
        return failure(__LINE__);
    }
    // Every icon the page uses is built in and draws something.
    constexpr std::array<std::string_view, 21> names {
        "battery", "battery-full", "battery-low", "battery-medium", "cable", "circle-check",
        "circle-pause", "clock", "external-link", "github", "hyperflux-mark",
        "hyperflux-symbolic", "keyboard", "lightbulb", "moon", "mouse", "plug-zap", "refresh-cw",
        "triangle-alert", "wifi", "wifi-off",
    };
    for(const auto name : names)
    {
        const QImage image = draw(embedded_icon(name), 32);
        bool painted = false;
        for(int y = 0; y < image.height() && !painted; ++y)
        {
            for(int x = 0; x < image.width() && !painted; ++x)
            {
                painted = image.pixelColor(x, y).alpha() > 0;
            }
        }
        if(image.isNull() || !painted)
        {
            std::cerr << "icon " << name << " did not draw\n";
            return failure(__LINE__);
        }
    }
    if(!embedded_icon("no-such-icon").empty() || !icon_pixmap("no-such-icon", 16, 1.0, Qt::red).isNull()
       || icon_pixmap("mouse", 16, 2.0, Qt::red).size() != QSize(32, 32)
       || icon_pixmap("mouse", 16, 2.0, Qt::red).devicePixelRatio() != 2.0)
    {
        return failure(__LINE__);
    }
    return EXIT_SUCCESS;
}

} // namespace

int main(int argc, char** argv)
{
    QApplication application(argc, argv);
    using namespace hyperflux::openrgb::native;

    if(renderer_contract() != EXIT_SUCCESS)
    {
        return EXIT_FAILURE;
    }

    PluginInformationViewModel model;
    model.tone = PluginHealthTone::Positive;
    model.headline = "Ready";
    PluginDeviceRow keyboard {"receiver-1/keyboard-1/profile", "Test Keyboard TKL", "Keyboard"};
    keyboard.connection = "Wired";
    keyboard.note = "OpenRGB controls it over the cable until it is unplugged.";
    model.devices.push_back(keyboard);
    PluginDeviceRow mouse {"receiver-1/mouse-1/profile", "Test Mouse", "Mouse"};
    mouse.connection = "Wireless";
    mouse.battery_percent = 76;
    mouse.charge = "Charging";
    model.devices.push_back(mouse);

    // OpenRGB's Information list: a container page in a tab widget, with a
    // label named "icon" in the tab's left button.
    QTabWidget tabs;
    auto* container = new QWidget();
    auto* container_layout = new QVBoxLayout(container);
    auto* widget = new PluginInformationWidget([&model] { return model; });
    container_layout->addWidget(widget);
    const int index = tabs.addTab(container, QString());
    auto* tab_label = new QWidget();
    auto* glyph = new QLabel("⚙", tab_label);
    glyph->setObjectName("icon");
    tabs.tabBar()->setTabButton(index, QTabBar::LeftSide, tab_label);
    QApplication::processEvents();
#if QT_VERSION >= QT_VERSION_CHECK(6, 0, 0)
    const bool swapped = !glyph->pixmap().isNull();
#else
    const bool swapped = !glyph->pixmap(Qt::ReturnByValue).isNull();
#endif
    if(!swapped)
    {
        return failure(__LINE__);
    }

    // The GitHub button opens the project page, the same address OpenRGB lists for the plugin.
    const QString github_url = QString::fromUtf8(repository_url.data(), static_cast<int>(repository_url.size()));
    if(github_url != "https://github.com/offalexjackson777-stack/hyperflux-next")
    {
        return failure(__LINE__);
    }

    auto* health = widget->findChild<QFrame*>("hyperfluxHealth");
    auto* cards = widget->findChild<QWidget*>("hyperfluxDeviceCards");
    auto* github = widget->findChild<QPushButton*>("hyperfluxGitHub");
    const auto card_frames = widget->findChildren<QFrame*>("hyperfluxDeviceCard");
    const auto timers = widget->findChildren<QTimer*>();
    if(health == nullptr || health->property("word").toString() != "Ready" || cards == nullptr
       || card_frames.size() != 2 || widget->findChild<QFrame*>("hyperfluxAttention") != nullptr
       || widget->findChild<QLabel*>("hyperfluxEmptyState") != nullptr
       || widget->findChild<QWidget*>("hyperfluxLighting") == nullptr
       || github == nullptr || github->accessibleName() != "GitHub"
       || github->toolTip() != github_url || timers.size() != 1
       || timers.front()->interval() != PluginInformationWidget::refresh_interval_ms
       || !timers.front()->isActive())
    {
        return failure(__LINE__);
    }
    const QFrame* mouse_card = nullptr;
    const QFrame* keyboard_card = nullptr;
    for(const auto* card : card_frames)
    {
        const auto stable_id = card->property("stableId").toString();
        mouse_card = stable_id == "receiver-1/mouse-1/profile" ? card : mouse_card;
        keyboard_card = stable_id == "receiver-1/keyboard-1/profile" ? card : keyboard_card;
    }
    if(mouse_card == nullptr || keyboard_card == nullptr
       || label_text(mouse_card, "hyperfluxDeviceName") != "Test Mouse"
       || label_text(mouse_card, "hyperfluxBatteryPercent") != "76%"
       || chip_words(mouse_card) != QStringList({"Wireless", "Charging"})
       || mouse_card->findChild<QLabel*>("hyperfluxDeviceNote") != nullptr
       || label_text(keyboard_card, "hyperfluxDeviceName") != "Test Keyboard TKL"
       || label_text(keyboard_card, "hyperfluxBatteryPercent") != "Battery level not reported yet"
       || chip_words(keyboard_card) != QStringList({"Wired"})
       || label_text(keyboard_card, "hyperfluxDeviceNote")
              != "OpenRGB controls it over the cable until it is unplugged.")
    {
        return failure(__LINE__);
    }
    // The type icon names the type for screen readers.
    bool named_type = false;
    for(const auto* icon : mouse_card->findChildren<QWidget*>("hyperfluxIcon"))
    {
        named_type = named_type || icon->accessibleName() == "Mouse";
    }
    if(!named_type)
    {
        return failure(__LINE__);
    }

    model.devices[1].stale = "No update for 14 minutes";
    model.devices[1].presence = "Asleep";
    model.devices[1].charge.clear();
    model.tone = PluginHealthTone::Negative;
    model.headline = "Needs attention";
    model.technical_detail = "injected socket detail";
    model.attention = PluginAttention {
        PluginHealthTone::Negative,
        "The HyperFlux bridge is not answering, so these readings are from 14 minutes ago. "
        "Run hyperfluxctl doctor --explain in a terminal to see the fix.",
        "hyperfluxctl doctor --explain",
    };
    widget->refresh();
    health = widget->findChild<QFrame*>("hyperfluxHealth");
    auto* attention_text = widget->findChild<QLabel*>("hyperfluxAttentionText");
    const QFrame* stale_card = nullptr;
    for(const auto* card : widget->findChildren<QFrame*>("hyperfluxDeviceCard"))
    {
        stale_card = card->property("stableId").toString() == "receiver-1/mouse-1/profile" ? card : stale_card;
    }
    if(health == nullptr || health->property("word").toString() != "Needs attention"
       || health->toolTip() != "injected socket detail" || attention_text == nullptr
       || !attention_text->text().contains("<span style=\"font-family:")
       || !attention_text->text().contains("hyperfluxctl doctor --explain</span>")
       || widget->findChildren<QFrame*>("hyperfluxDeviceCard").size() != 2 || stale_card == nullptr
       || chip_words(stale_card) != QStringList({"Wireless", "Asleep", "No update for 14 minutes"}))
    {
        return failure(__LINE__);
    }

    model = PluginInformationViewModel {};
    model.tone = PluginHealthTone::Positive;
    model.headline = "Ready";
    model.empty_state = "No devices found on a supported receiver";
    widget->refresh();
    if(label_text(widget, "hyperfluxEmptyState") != "No devices found on a supported receiver"
       || !widget->findChildren<QFrame*>("hyperfluxDeviceCard").isEmpty()
       || widget->findChild<QWidget*>("hyperfluxLighting") != nullptr
       || widget->findChild<QFrame*>("hyperfluxAttention") != nullptr)
    {
        return failure(__LINE__);
    }

    // A theme change rebuilds the page in the new colours once Qt has finished with the old one.
    QPalette light = widget->palette();
    light.setColor(QPalette::Window, Qt::white);
    light.setColor(QPalette::WindowText, Qt::black);
    widget->setPalette(light);
    QApplication::processEvents();
    if(label_text(widget, "hyperfluxEmptyState") != "No devices found on a supported receiver")
    {
        return failure(__LINE__);
    }
    // Light themes get a neutral tile with the cyan glyph, dark ones the slate mark.
    const auto header_icons = [widget]
    {
        QStringList names;
        for(const auto* icon : widget->findChildren<QWidget*>("hyperfluxIcon"))
        {
            if(icon->accessibleName() == "HyperFlux Next")
            {
                names << icon->property("icon").toString();
            }
        }
        return names;
    };
    if(widget->findChild<QFrame*>("hyperfluxMarkTile") == nullptr
       || header_icons() != QStringList({"hyperflux-symbolic"}))
    {
        return failure(__LINE__);
    }
    QPalette dark = widget->palette();
    dark.setColor(QPalette::Window, QColor(0x1E, 0x1E, 0x1E));
    dark.setColor(QPalette::WindowText, Qt::white);
    widget->setPalette(dark);
    QApplication::processEvents();
    if(widget->findChild<QFrame*>("hyperfluxMarkTile") != nullptr
       || header_icons() != QStringList({"hyperflux-mark"}))
    {
        return failure(__LINE__);
    }
    return EXIT_SUCCESS;
}
