// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/razer_feature_transport.hpp>

#include <algorithm>
#include <iterator>

namespace hyperflux::openrgb::native
{

HyperFluxRazerFeatureTransport::HyperFluxRazerFeatureTransport(
    NativeFeatureExchange& exchange) noexcept
    : exchange_(&exchange)
{
}

int HyperFluxRazerFeatureTransport::GetFeatureReport(unsigned char* report, size_t size)
{
    std::lock_guard lock(mutex_);
    if(report == nullptr || size != razer_hid_report_bytes || !response_pending_)
    {
        return -1;
    }
    std::copy(
        pending_response_.begin(),
        pending_response_.end(),
        std::next(report));
    pending_response_.fill(0);
    response_pending_ = false;
    return static_cast<int>(size);
}

int HyperFluxRazerFeatureTransport::SendFeatureReport(
    const unsigned char* report, size_t size)
{
    std::lock_guard lock(mutex_);
    if(report == nullptr || size != razer_hid_report_bytes)
    {
        return -1;
    }
    pending_response_.fill(0);
    response_pending_ = false;
    std::array<std::uint8_t, razer_native_report_bytes> request {};
    std::copy(std::next(report), std::next(report, size), request.begin());
    const auto outcome = exchange_->exchange(request);
    if(outcome.certainty != NativeExchangeCertainty::MatchedResponse)
    {
        return -1;
    }
    // OpenRGB setters do not read their acknowledgement. A later atomic send
    // therefore replaces that unconsumed response; getters consume the result
    // of their immediately preceding send exactly once.
    pending_response_ = outcome.response;
    response_pending_ = true;
    return static_cast<int>(size);
}

int HyperFluxRazerFeatureTransport::SendArgbFeatureReport(
    const unsigned char*, size_t)
{
    return -1;
}

} // namespace hyperflux::openrgb::native
