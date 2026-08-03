// SPDX-License-Identifier: GPL-2.0-only

#include <hyperflux/openrgb/razer_feature_transport.hpp>

#include <array>
#include <cstddef>
#include <cstdint>

namespace
{

class FakeExchange final : public hyperflux::openrgb::native::NativeFeatureExchange
{
public:
    hyperflux::openrgb::native::NativeFeatureExchangeResult exchange(
        const std::array<std::uint8_t,
            hyperflux::openrgb::native::razer_native_report_bytes>& request) override
    {
        ++calls;
        observed = request;
        return next;
    }

    std::size_t calls = 0;
    std::array<std::uint8_t,
        hyperflux::openrgb::native::razer_native_report_bytes> observed {};
    hyperflux::openrgb::native::NativeFeatureExchangeResult next {
        hyperflux::openrgb::native::NativeExchangeCertainty::MatchedResponse,
        {},
    };
};

} // namespace

int main()
{
    using namespace hyperflux::openrgb::native;
    FakeExchange exchange;
    for(std::size_t index = 0; index < exchange.next.response.size(); ++index)
    {
        exchange.next.response[index] = static_cast<std::uint8_t>(index);
    }
    HyperFluxRazerFeatureTransport transport(exchange);
    std::array<unsigned char, razer_hid_report_bytes> request {};
    request[0] = 0x07;
    for(std::size_t index = 1; index < request.size(); ++index)
    {
        request[index] = static_cast<unsigned char>(index + 2);
    }
    if(transport.SendFeatureReport(request.data(), request.size())
       != static_cast<int>(request.size()))
    {
        return 1;
    }
    if(exchange.calls != 1 || exchange.observed.front() != request[1]
       || exchange.observed.back() != request.back())
    {
        return 2;
    }
    exchange.next.response.fill(0xaa);
    if(transport.SendFeatureReport(request.data(), request.size())
           != static_cast<int>(request.size())
       || exchange.calls != 2)
    {
        return 3;
    }

    std::array<unsigned char, razer_hid_report_bytes> response {};
    response[0] = 0x08;
    if(transport.GetFeatureReport(response.data(), response.size())
           != static_cast<int>(response.size())
       || response[0] != 0x08 || response[1] != 0xaa || response.back() != 0xaa)
    {
        return 4;
    }
    if(transport.GetFeatureReport(response.data(), response.size()) != -1)
    {
        return 5;
    }

    exchange.next.certainty = NativeExchangeCertainty::NotSent;
    if(transport.SendFeatureReport(request.data(), request.size()) != -1)
    {
        return 6;
    }
    exchange.next.certainty = NativeExchangeCertainty::MayHaveBeenSent;
    if(transport.SendFeatureReport(request.data(), request.size()) != -1)
    {
        return 7;
    }
    exchange.next.certainty = NativeExchangeCertainty::MatchedResponse;
    if(transport.SendFeatureReport(request.data(), request.size() - 1) != -1
       || transport.SendFeatureReport(nullptr, request.size()) != -1
       || transport.GetFeatureReport(nullptr, response.size()) != -1
       || transport.SendArgbFeatureReport(request.data(), request.size()) != -1)
    {
        return 8;
    }
    return 0;
}
