// SPDX-License-Identifier: GPL-2.0-only

#pragma once

#include "RazerController.h"

#include <array>
#include <cstddef>
#include <cstdint>
#include <mutex>

namespace hyperflux::openrgb::native
{

inline constexpr std::size_t razer_hid_report_bytes = 91;
inline constexpr std::size_t razer_native_report_bytes = 90;

enum class NativeExchangeCertainty
{
    NotSent,
    MayHaveBeenSent,
    MatchedResponse,
};

struct NativeFeatureExchangeResult
{
    NativeExchangeCertainty certainty;
    std::array<std::uint8_t, razer_native_report_bytes> response {};
};

class NativeFeatureExchange
{
public:
    virtual ~NativeFeatureExchange() = default;

    [[nodiscard]] virtual NativeFeatureExchangeResult exchange(
        const std::array<std::uint8_t, razer_native_report_bytes>& request) = 0;
};

/// HIDAPI-compatible facade over one atomic HyperFlux native exchange.
class HyperFluxRazerFeatureTransport final : public RazerFeatureTransport
{
public:
    explicit HyperFluxRazerFeatureTransport(NativeFeatureExchange& exchange) noexcept;

    int GetFeatureReport(unsigned char* report, size_t size) override;
    int SendFeatureReport(const unsigned char* report, size_t size) override;
    int SendArgbFeatureReport(const unsigned char* report, size_t size) override;

private:
    NativeFeatureExchange* exchange_;
    std::array<std::uint8_t, razer_native_report_bytes> pending_response_ {};
    bool response_pending_ = false;
    std::mutex mutex_;
};

} // namespace hyperflux::openrgb::native
