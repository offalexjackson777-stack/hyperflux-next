// SPDX-License-Identifier: GPL-2.0-only

#include "plugin_widget.hpp"

#include <QDesktopServices>
#include <QFrame>
#include <QGridLayout>
#include <QHBoxLayout>
#include <QLabel>
#include <QProgressBar>
#include <QScrollArea>
#include <QStyle>
#include <QToolButton>
#include <QUrl>
#include <QVariant>
#include <QVBoxLayout>

#include <utility>

namespace hyperflux::openrgb::native
{
namespace
{

// Semantic colours stay readable on OpenRGB's light and dark themes; everything
// else follows the host palette.
constexpr const char* good_colour = "#2e9d5b";
constexpr const char* warning_colour = "#c98a12";
constexpr const char* bad_colour = "#d0453b";

constexpr const char* page_style = R"(
QFrame#hyperfluxDeviceCard {
    border: 1px solid palette(mid);
    border-radius: 10px;
    background: palette(base);
}
QFrame#hyperfluxAttention {
    border: 1px solid #c98a12;
    border-radius: 10px;
}
QLabel#hyperfluxDeviceConnection {
    border: 1px solid palette(highlight);
    border-radius: 9px;
    padding: 1px 9px;
}
QToolButton#hyperfluxRepository {
    border: 1px solid palette(highlight);
    border-radius: 13px;
    padding: 4px 14px;
    background: palette(highlight);
    color: palette(highlighted-text);
    font-weight: bold;
}
QToolButton#hyperfluxRepository:hover {
    background: palette(button);
    color: palette(button-text);
}
QProgressBar#hyperfluxBatteryBar {
    border: none;
    border-radius: 4px;
    background: palette(midlight);
}
)";

QString text(const std::string& value)
{
    return QString::fromStdString(value);
}

QString pill_style(const char* colour)
{
    return QString("border: 1px solid %1; border-radius: 9px; padding: 1px 9px; color: %1;")
        .arg(colour);
}

const char* tone_colour(PluginHealthTone tone)
{
    switch(tone)
    {
        case PluginHealthTone::Positive: return good_colour;
        case PluginHealthTone::Warning: return warning_colour;
        case PluginHealthTone::Negative: return bad_colour;
        case PluginHealthTone::Neutral: return nullptr;
    }
    return nullptr;
}

QStyle::StandardPixmap health_icon(PluginHealthTone tone)
{
    switch(tone)
    {
        case PluginHealthTone::Positive: return QStyle::SP_DialogApplyButton;
        case PluginHealthTone::Warning: return QStyle::SP_MessageBoxWarning;
        case PluginHealthTone::Negative: return QStyle::SP_MessageBoxCritical;
        case PluginHealthTone::Neutral: return QStyle::SP_MessageBoxInformation;
    }
    return QStyle::SP_MessageBoxInformation;
}

const char* battery_colour(std::uint8_t percent)
{
    if(percent <= 15)
    {
        return bad_colour;
    }
    return percent <= 30 ? warning_colour : good_colour;
}

QLabel* muted(const QString& value, QWidget* parent)
{
    auto* label = new QLabel(value, parent);
    label->setForegroundRole(QPalette::PlaceholderText);
    label->setWordWrap(true);
    return label;
}

QLabel* section(const QString& value, QWidget* parent)
{
    auto* label = new QLabel(value, parent);
    QFont font = label->font();
    font.setBold(true);
    label->setFont(font);
    return label;
}

QFrame* device_card(const PluginDeviceRow& device, QWidget* parent)
{
    auto* card = new QFrame(parent);
    card->setObjectName("hyperfluxDeviceCard");
    card->setProperty("stableId", QVariant(text(device.stable_id)));
    auto* layout = new QVBoxLayout(card);
    layout->setContentsMargins(16, 14, 16, 14);
    layout->setSpacing(8);

    auto* heading = new QHBoxLayout();
    auto* names = new QVBoxLayout();
    names->setSpacing(1);
    auto* name = new QLabel(text(device.device), card);
    name->setObjectName("hyperfluxDeviceName");
    QFont name_font = name->font();
    name_font.setBold(true);
    name_font.setPointSize(name_font.pointSize() + 1);
    name->setFont(name_font);
    name->setWordWrap(true);
    names->addWidget(name);
    auto* state = muted(text(device.type + " \xC2\xB7 " + device.availability), card);
    state->setObjectName("hyperfluxDeviceState");
    names->addWidget(state);
    heading->addLayout(names, 1);
    if(!device.connection.empty())
    {
        auto* connection = new QLabel(text(device.connection), card);
        connection->setObjectName("hyperfluxDeviceConnection");
        heading->addWidget(connection, 0, Qt::AlignTop);
    }
    layout->addLayout(heading);

    auto* reading = new QHBoxLayout();
    reading->setSpacing(10);
    auto* percent = new QLabel(
        device.battery_percent.has_value()
            ? QString::number(*device.battery_percent) + "%"
            : QString::fromUtf8("\xE2\x80\x94"),
        card);
    percent->setObjectName("hyperfluxBatteryPercent");
    QFont percent_font = percent->font();
    percent_font.setBold(true);
    percent_font.setPointSize(percent_font.pointSize() + 9);
    percent->setFont(percent_font);
    reading->addWidget(percent, 0, Qt::AlignBottom);
    if(!device.charge.empty())
    {
        auto* charge = new QLabel(text(device.charge), card);
        charge->setObjectName("hyperfluxBatteryCharge");
        charge->setStyleSheet(
            device.charge == "On battery"
                ? QString("border: 1px solid palette(mid); border-radius: 9px; padding: 1px 9px;")
                : pill_style(good_colour));
        reading->addWidget(charge, 0, Qt::AlignVCenter);
    }
    reading->addStretch(1);
    auto* note = muted(text(device.battery_note), card);
    note->setObjectName("hyperfluxBatteryNote");
    note->setWordWrap(false);
    reading->addWidget(note, 0, Qt::AlignVCenter);
    layout->addLayout(reading);

    auto* bar = new QProgressBar(card);
    bar->setObjectName("hyperfluxBatteryBar");
    bar->setRange(0, 100);
    bar->setValue(device.battery_percent.value_or(0));
    bar->setTextVisible(false);
    bar->setFixedHeight(8);
    if(device.battery_percent.has_value())
    {
        bar->setStyleSheet(
            QString("QProgressBar#hyperfluxBatteryBar::chunk { border-radius: 4px; background: %1; }")
                .arg(battery_colour(*device.battery_percent)));
    }
    layout->addWidget(bar);

    auto* control = muted(text(device.openrgb + " \xC2\xB7 " + device.support), card);
    control->setObjectName("hyperfluxDeviceControl");
    layout->addWidget(control);
    return card;
}

} // namespace

PluginInformationWidget::PluginInformationWidget(
    ModelProvider provider,
    QWidget* parent)
    : QWidget(parent),
      provider_(std::move(provider)),
      health_icon_(nullptr),
      health_title_(nullptr),
      health_summary_(nullptr),
      attention_(nullptr),
      attention_text_(nullptr),
      empty_state_(nullptr),
      devices_(nullptr),
      device_grid_(nullptr),
      lighting_transport_(nullptr),
      effects_authority_(nullptr),
      build_identity_(nullptr),
      repository_(nullptr)
{
    setObjectName("hyperfluxNextInformation");
    setStyleSheet(page_style);
    auto* outer = new QVBoxLayout(this);
    outer->setContentsMargins(0, 0, 0, 0);
    auto* scroll = new QScrollArea(this);
    scroll->setFrameShape(QFrame::NoFrame);
    scroll->setWidgetResizable(true);
    outer->addWidget(scroll);
    auto* content = new QWidget(scroll);
    scroll->setWidget(content);

    auto* page = new QVBoxLayout(content);
    page->setContentsMargins(20, 18, 20, 18);
    page->setSpacing(14);

    auto* heading = new QHBoxLayout();
    heading->setSpacing(10);
    auto* titles = new QVBoxLayout();
    titles->setSpacing(2);
    auto* title = new QLabel("HyperFlux Next", content);
    QFont title_font = title->font();
    title_font.setBold(true);
    title_font.setPointSize(title_font.pointSize() + 5);
    title->setFont(title_font);
    titles->addWidget(title);
    health_summary_ = muted(QString(), content);
    health_summary_->setObjectName("hyperfluxHealthSummary");
    titles->addWidget(health_summary_);
    heading->addLayout(titles, 1);

    health_icon_ = new QLabel(content);
    health_icon_->setObjectName("hyperfluxHealthIcon");
    health_icon_->setFixedSize(20, 20);
    heading->addWidget(health_icon_, 0, Qt::AlignVCenter);
    health_title_ = new QLabel(content);
    health_title_->setObjectName("hyperfluxHealthTitle");
    QFont health_font = health_title_->font();
    health_font.setBold(true);
    health_title_->setFont(health_font);
    heading->addWidget(health_title_, 0, Qt::AlignVCenter);

    auto* refresh_button = new QToolButton(content);
    refresh_button->setObjectName("hyperfluxRefresh");
    refresh_button->setIcon(style()->standardIcon(QStyle::SP_BrowserReload));
    refresh_button->setToolTip("Refresh status");
    refresh_button->setAutoRaise(true);
    connect(refresh_button, &QToolButton::clicked, this, [this] { refresh(); });
    heading->addWidget(refresh_button, 0, Qt::AlignVCenter);

    repository_ = new QToolButton(content);
    repository_->setObjectName("hyperfluxRepository");
    repository_->setText(QString::fromUtf8("GitHub \xE2\x86\x97"));
    repository_->setToolTip(QString::fromUtf8(repository_url.data(), static_cast<int>(repository_url.size())));
    repository_->setCursor(Qt::PointingHandCursor);
    connect(repository_, &QToolButton::clicked, this, []
    {
        QDesktopServices::openUrl(QUrl(QString::fromUtf8(
            repository_url.data(), static_cast<int>(repository_url.size()))));
    });
    heading->addWidget(repository_, 0, Qt::AlignVCenter);
    page->addLayout(heading);

    attention_ = new QFrame(content);
    attention_->setObjectName("hyperfluxAttention");
    auto* attention_layout = new QHBoxLayout(attention_);
    attention_layout->setContentsMargins(14, 10, 14, 10);
    auto* attention_icon = new QLabel(attention_);
    attention_icon->setPixmap(
        style()->standardIcon(QStyle::SP_MessageBoxWarning).pixmap(22, 22));
    attention_layout->addWidget(attention_icon, 0, Qt::AlignTop);
    attention_text_ = new QLabel(attention_);
    attention_text_->setObjectName("hyperfluxAttentionText");
    attention_text_->setWordWrap(true);
    attention_text_->setTextInteractionFlags(Qt::TextSelectableByMouse);
    attention_layout->addWidget(attention_text_, 1);
    attention_->hide();
    page->addWidget(attention_);

    empty_state_ = muted(QString(), content);
    empty_state_->setObjectName("hyperfluxControllerEmptyState");
    page->addWidget(empty_state_);

    devices_ = new QWidget(content);
    devices_->setObjectName("hyperfluxDeviceCards");
    device_grid_ = new QGridLayout(devices_);
    device_grid_->setContentsMargins(0, 0, 0, 0);
    device_grid_->setHorizontalSpacing(14);
    device_grid_->setVerticalSpacing(14);
    device_grid_->setColumnStretch(0, 1);
    device_grid_->setColumnStretch(1, 1);
    page->addWidget(devices_);

    page->addWidget(section("Lighting", content));
    lighting_transport_ = muted(QString(), content);
    lighting_transport_->setObjectName("hyperfluxLightingTransport");
    effects_authority_ = muted(QString(), content);
    effects_authority_->setObjectName("hyperfluxEffectsAuthority");
    page->addWidget(lighting_transport_);
    page->addWidget(effects_authority_);
    page->addStretch(1);

    build_identity_ = muted(QString(), content);
    build_identity_->setObjectName("hyperfluxBuildIdentity");
    page->addWidget(build_identity_);

    refresh();
}

void PluginInformationWidget::refresh()
{
    if(!provider_)
    {
        return;
    }
    const auto model = provider_();
    if(!has_rendered_ || model != rendered_)
    {
        render(model);
        rendered_ = model;
        has_rendered_ = true;
    }
}

void PluginInformationWidget::render(const PluginInformationViewModel& model)
{
    health_icon_->setPixmap(style()->standardIcon(health_icon(model.tone)).pixmap(18, 18));
    health_title_->setText(text(model.headline));
    const char* colour = tone_colour(model.tone);
    health_title_->setStyleSheet(colour == nullptr ? QString() : QString("color: %1;").arg(colour));
    health_summary_->setText(text(model.summary));
    health_summary_->setToolTip(
        model.technical_detail.has_value() ? text(*model.technical_detail) : QString());

    attention_->setVisible(model.attention.has_value());
    attention_text_->setText(model.attention.has_value() ? text(*model.attention) : QString());

    const bool empty = model.devices.empty();
    empty_state_->setVisible(empty);
    empty_state_->setText(empty ? "No paired device is reported by the bridge." : "");
    devices_->setVisible(!empty);
    render_cards(model);

    lighting_transport_->setText(text(model.lighting_transport));
    effects_authority_->setText(text(model.effects_authority));
    build_identity_->setText(text(model.build_identity));
}

void PluginInformationWidget::render_cards(const PluginInformationViewModel& model)
{
    while(auto* item = device_grid_->takeAt(0))
    {
        delete item->widget();
        delete item;
    }
    for(std::size_t index = 0; index < model.devices.size(); ++index)
    {
        device_grid_->addWidget(
            device_card(model.devices[index], devices_),
            static_cast<int>(index / 2),
            static_cast<int>(index % 2));
    }
}

} // namespace hyperflux::openrgb::native
