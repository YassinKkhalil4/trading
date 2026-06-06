from __future__ import annotations

import pytest

from trading_system.app.data.provider_capabilities import ProviderCapabilityError, assert_provider_usage


def test_alpha_vantage_is_not_intraday_source():
    with pytest.raises(ProviderCapabilityError, match="not approved for intraday"):
        assert_provider_usage("alpha_vantage", intraday=True)


def test_sec_edgar_is_enrichment_only_not_intraday():
    assert_provider_usage("sec_edgar", enrichment=True)
    with pytest.raises(ProviderCapabilityError, match="not approved for intraday"):
        assert_provider_usage("sec_edgar", intraday=True)

