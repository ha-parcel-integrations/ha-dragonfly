"""Tests for Dragonfly diagnostics."""
from unittest.mock import MagicMock

from custom_components.dragonfly.diagnostics import async_get_config_entry_diagnostics


async def test_diagnostics_redacts_and_counts(hass):
    entry = MagicMock()
    entry.options = {"parcels": [{"tracking_code": "INTLCM123456"}]}
    entry.runtime_data.coordinator.data = [
        {
            "barcode": "INTLCM123456",
            "sender": "ACME",
            "status": "out_for_delivery",
            "raw": {
                "tracking_id": "INTLCM123456",
                "driver_name": "Piet",
                "package_location": {"address": {"city": "Rotterdam"}},
            },
        }
    ]
    entry.runtime_data.coordinator.delivered = []

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["counts"] == {"incoming_active": 1, "delivered": 0}
    # tracking codes and payload PII are redacted
    assert result["entry_options"]["parcels"][0]["tracking_code"] == "**REDACTED**"
    assert result["incoming"][0]["barcode"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["driver_name"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["package_location"] == "**REDACTED**"
    # non-PII fields survive
    assert result["incoming"][0]["status"] == "out_for_delivery"
