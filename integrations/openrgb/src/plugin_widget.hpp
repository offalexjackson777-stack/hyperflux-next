// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include <hyperflux/openrgb/plugin_view_model.hpp>

#include <QWidget>

#include <functional>

class QLabel;
class QTableWidget;

namespace hyperflux::openrgb::native
{

class PluginInformationWidget final : public QWidget
{
public:
    using ModelProvider = std::function<PluginInformationViewModel()>;

    explicit PluginInformationWidget(
        ModelProvider provider,
        QWidget* parent = nullptr);

    void refresh();

private:
    void render(const PluginInformationViewModel& model);

    ModelProvider provider_;
    PluginInformationViewModel rendered_;
    bool has_rendered_ = false;
    QLabel* health_icon_;
    QLabel* health_title_;
    QLabel* health_summary_;
    QLabel* empty_state_;
    QTableWidget* devices_;
    QLabel* lighting_transport_;
    QLabel* effects_authority_;
    QLabel* build_identity_;
};

} // namespace hyperflux::openrgb::native
