// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include <hyperflux/openrgb/plugin_coordinator.hpp>

#include <QObject>

#include <atomic>

namespace hyperflux::openrgb::native
{

class QtApplicationDispatcher final : public QObject, public ApplicationDispatcher
{
public:
    explicit QtApplicationDispatcher(QObject* parent = nullptr) noexcept;
    ~QtApplicationDispatcher() override;

    [[nodiscard]] bool post(std::function<void()> task) noexcept override;
    void stop() noexcept;

private:
    std::atomic_bool accepting_ {true};
};

} // namespace hyperflux::openrgb::native
