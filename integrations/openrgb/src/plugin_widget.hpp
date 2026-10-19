// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include <hyperflux/openrgb/plugin_view_model.hpp>

#include <QPointer>
#include <QWidget>

#include <functional>

class QEvent;
class QLabel;
class QString;
class QTimer;
class QUrl;
class QVBoxLayout;

namespace hyperflux::openrgb::native
{

/// Where Help goes: the guide at `installed_path` when it is readable, the
/// online guide otherwise.
QUrl help_target(const QString& installed_path);

/// The HyperFlux Next page in OpenRGB's Information list.
///
/// Colours come from the host palette plus Tailwind's semantic tones, and
/// every widget carries its own style sheet, so an OpenRGB skin can change
/// the window around the page but cannot break the page itself.
class PluginInformationWidget final : public QWidget
{
public:
    using ModelProvider = std::function<PluginInformationViewModel()>;

    explicit PluginInformationWidget(
        ModelProvider provider,
        QWidget* parent = nullptr);

    /// Asks the provider for the current state and redraws when it changed.
    void refresh();

    /// How often the page re-reads state so "No update for N minutes" stays true.
    static constexpr int refresh_interval_ms = 30'000;

protected:
    void changeEvent(QEvent* event) override;

private:
    void render(const PluginInformationViewModel& model);
    void install_list_icon();
    void paint_list_icon();

    ModelProvider provider_;
    PluginInformationViewModel rendered_;
    bool has_rendered_ = false;
    bool rendering_ = false;
    bool restyle_pending_ = false;
    QVBoxLayout* page_;
    QWidget* content_ = nullptr;
    QTimer* timer_;
    QPointer<QLabel> list_icon_;
};

} // namespace hyperflux::openrgb::native
