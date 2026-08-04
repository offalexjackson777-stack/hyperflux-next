def endpoint(interface, name, out_sig):
    return lambda function: function


@endpoint("razer.device.misc", "getSupportedPollRates", out_sig="aq")
def get_supported_poll_rates(self):
    """Return the polling rates declared by the device class."""
    self.logger.debug("DBus call get_supported_poll_rates")
    return self.POLL_RATES
