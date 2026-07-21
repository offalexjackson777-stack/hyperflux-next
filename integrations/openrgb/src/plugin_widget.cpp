// SPDX-License-Identifier: GPL-2.0-only

#include "plugin_widget.hpp"

#include <QAbstractItemView>
#include <QFrame>
#include <QHeaderView>
#include <QLabel>
#include <QStyle>
#include <QTableWidget>
#include <QTableWidgetItem>
#include <QToolButton>
#include <QVBoxLayout>

#include <array>
#include <utility>

namespace hyperflux::openrgb::native
{
namespace
{

QFrame* separator(QWidget* parent)
{
    auto* line = new QFrame(parent);
    line->setFrameShape(QFrame::HLine);
    line->setFrameShadow(QFrame::Sunken);
    return line;
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

QTableWidgetItem* item(const std::string& value)
{
    auto* result = new QTableWidgetItem(QString::fromStdString(value));
    result->setToolTip(result->text());
    return result;
}

} // namespace

PluginInformationWidget::PluginInformationWidget(
    ModelProvider provider,
    QWidget* parent)
    : QWidget(parent),
      provider_(std::move(provider)),
      health_icon_(new QLabel(this)),
      health_title_(new QLabel(this)),
      health_summary_(new QLabel(this)),
      empty_state_(new QLabel(this)),
      devices_(new QTableWidget(this)),
      lighting_transport_(new QLabel(this)),
      effects_authority_(new QLabel(this)),
      build_identity_(new QLabel(this))
{
    setObjectName("hyperfluxNextInformation");
    auto* page = new QVBoxLayout(this);
    page->setContentsMargins(18, 18, 18, 18);
    page->setSpacing(12);

    auto* heading = new QHBoxLayout();
    auto* titles = new QVBoxLayout();
    titles->setSpacing(2);
    auto* title = new QLabel("HyperFlux Next", this);
    QFont title_font = title->font();
    title_font.setBold(true);
    title_font.setPointSize(title_font.pointSize() + 3);
    title->setFont(title_font);
    titles->addWidget(title);
    titles->addWidget(new QLabel("OpenRGB integration", this));
    heading->addLayout(titles, 1);
    auto* refresh_button = new QToolButton(this);
    refresh_button->setObjectName("hyperfluxRefresh");
    refresh_button->setIcon(style()->standardIcon(QStyle::SP_BrowserReload));
    refresh_button->setToolTip("Refresh status");
    connect(refresh_button, &QToolButton::clicked, this, [this] { refresh(); });
    heading->addWidget(refresh_button, 0, Qt::AlignTop);
    page->addLayout(heading);

    auto* health = new QHBoxLayout();
    health_icon_->setObjectName("hyperfluxHealthIcon");
    health_icon_->setFixedSize(24, 24);
    health->addWidget(health_icon_, 0, Qt::AlignTop);
    auto* health_text = new QVBoxLayout();
    health_text->setSpacing(2);
    health_title_->setObjectName("hyperfluxHealthTitle");
    QFont health_font = health_title_->font();
    health_font.setBold(true);
    health_title_->setFont(health_font);
    health_summary_->setObjectName("hyperfluxHealthSummary");
    health_summary_->setWordWrap(true);
    health_text->addWidget(health_title_);
    health_text->addWidget(health_summary_);
    health->addLayout(health_text, 1);
    page->addLayout(health);
    page->addWidget(separator(this));

    auto* inventory_title = new QLabel("Paired inventory", this);
    QFont section_font = inventory_title->font();
    section_font.setBold(true);
    inventory_title->setFont(section_font);
    page->addWidget(inventory_title);
    empty_state_->setObjectName("hyperfluxControllerEmptyState");
    empty_state_->setWordWrap(true);
    page->addWidget(empty_state_);

    devices_->setObjectName("hyperfluxInventoryTable");
    devices_->setColumnCount(7);
    devices_->setHorizontalHeaderLabels(
        {"Device", "Type", "Pairing", "Current state", "Battery", "Support", "OpenRGB"});
    devices_->verticalHeader()->setVisible(false);
    devices_->setAlternatingRowColors(true);
    devices_->setSelectionMode(QAbstractItemView::NoSelection);
    devices_->setEditTriggers(QAbstractItemView::NoEditTriggers);
    devices_->setFocusPolicy(Qt::NoFocus);
    devices_->setMinimumHeight(110);
    devices_->setMaximumHeight(260);
    devices_->horizontalHeader()->setSectionResizeMode(QHeaderView::ResizeToContents);
    devices_->horizontalHeader()->setSectionResizeMode(0, QHeaderView::Stretch);
    devices_->horizontalHeader()->setSectionResizeMode(6, QHeaderView::Stretch);
    page->addWidget(devices_);
    page->addWidget(separator(this));

    auto* lighting_title = new QLabel("Lighting compatibility", this);
    lighting_title->setFont(section_font);
    page->addWidget(lighting_title);
    lighting_transport_->setObjectName("hyperfluxLightingTransport");
    lighting_transport_->setWordWrap(true);
    effects_authority_->setObjectName("hyperfluxEffectsAuthority");
    effects_authority_->setWordWrap(true);
    page->addWidget(lighting_transport_);
    page->addWidget(effects_authority_);
    page->addStretch(1);

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
    health_icon_->setPixmap(style()->standardIcon(health_icon(model.tone)).pixmap(20, 20));
    health_title_->setText(QString::fromStdString(model.headline));
    health_summary_->setText(QString::fromStdString(model.summary));
    health_summary_->setToolTip(
        model.technical_detail.has_value()
            ? QString::fromStdString(*model.technical_detail)
            : QString());

    const bool empty = model.devices.empty();
    empty_state_->setVisible(empty);
    empty_state_->setText(empty ? "No paired device is reported by the bridge." : "");
    devices_->setVisible(!empty);
    devices_->setRowCount(static_cast<int>(model.devices.size()));
    for(std::size_t row_index = 0; row_index < model.devices.size(); ++row_index)
    {
        const auto& device = model.devices[row_index];
        const std::array<std::string, 7> values = {
            device.device,
            device.type,
            device.pairing,
            device.availability,
            device.battery,
            device.support,
            device.openrgb,
        };
        for(std::size_t column = 0; column < values.size(); ++column)
        {
            devices_->setItem(
                static_cast<int>(row_index),
                static_cast<int>(column),
                item(values[column]));
        }
    }
    devices_->resizeRowsToContents();
    lighting_transport_->setText(QString::fromStdString(model.lighting_transport));
    effects_authority_->setText(QString::fromStdString(model.effects_authority));
    build_identity_->setText(QString::fromStdString(model.build_identity));
}

} // namespace hyperflux::openrgb::native
