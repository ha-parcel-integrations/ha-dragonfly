"""Tests for Dragonfly setup and unload."""
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dragonfly.api import DragonflyApiError
from custom_components.dragonfly.const import (
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DOMAIN,
)


def _sample(code: str = "INTLCMB2C000123456") -> dict:
    return {
        "tracking_id": code,
        "client_code": "ACME",
        "last_status": {
            "step": 3,
            "timestamp": "2026-04-29T08:46:00Z",
            "isDelivered": False,
            "showEta": True,
            "etaType": "time",
            "labels": {"shortLabel": {"nl": "Bij de bezorger"}},
        },
        "public_eta": {"from": "2026-05-01T10:00:00Z", "to": None},
        "status_list": [
            {
                "step": 1,
                "timestamp": "2026-04-28T10:00:00Z",
                "labels": {"shortLabel": {"nl": "Zending ontvangen"}},
            }
        ],
    }


async def test_setup_and_unload(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: "INTLCMB2C000123456"}]},
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.dragonfly.api.DragonflyApiClient.async_get_parcel",
        new=AsyncMock(return_value=_sample()),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED

    # The active parcel produced a per-parcel sensor and the summary sensor.
    incoming = hass.states.get("sensor.dragonfly_incoming_parcels")
    assert incoming is not None
    assert incoming.state == "1"

    # Services registered on setup...
    assert hass.services.has_service(DOMAIN, "track_parcel")

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED

    # ...and removed on unload (single-instance integration).
    assert not hass.services.has_service(DOMAIN, "track_parcel")


async def test_setup_retries_when_first_refresh_fails(hass):
    """When the first data fetch fails, setup retries from the entry itself.

    The first refresh runs in __init__.py before platforms are forwarded, so a
    failure raises ConfigEntryNotReady from the entry setup (SETUP_RETRY) rather
    than — too late — from a forwarded platform.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: "INTLCMB2C000123456"}]},
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.dragonfly.api.DragonflyApiClient.async_get_parcel",
        new=AsyncMock(side_effect=DragonflyApiError("Dragonfly unreachable")),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_per_parcel_sensor_spawn_and_remove(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: "INTLCMB2C000123456"}]},
    )
    entry.add_to_hass(hass)

    mock = AsyncMock(return_value=_sample())
    with patch("custom_components.dragonfly.api.DragonflyApiClient.async_get_parcel", new=mock):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        registry = er.async_get(hass)
        assert registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_INTLCMB2C000123456"
        )

        # The next poll returns a different tracking code: the summary sensor
        # spawns a new per-parcel sensor and removes the stale one.
        mock.return_value = _sample("INTLCMB2C000222222")
        await entry.runtime_data.coordinator.async_request_refresh()
        await hass.async_block_till_done()

        assert registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_INTLCMB2C000222222"
        )
        assert (
            registry.async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}_INTLCMB2C000123456"
            )
            is None
        )


async def test_options_update_applies_live_without_reload(hass):
    """Adding a parcel via options refreshes the coordinator immediately."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: "INTLCMB2C000123456"}]},
    )
    entry.add_to_hass(hass)

    mock = AsyncMock(return_value=_sample())
    with patch("custom_components.dragonfly.api.DragonflyApiClient.async_get_parcel", new=mock):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        mock.side_effect = lambda code: _sample(code)
        hass.config_entries.async_update_entry(
            entry,
            options={
                **entry.options,
                CONF_PARCELS: [
                    {CONF_TRACKING_CODE: "INTLCMB2C000123456"},
                    {CONF_TRACKING_CODE: "INTLCMB2C000222222"},
                ],
            },
        )
        await hass.async_block_till_done()

    incoming = hass.states.get("sensor.dragonfly_incoming_parcels")
    assert incoming.state == "2"
