"""Dragonfly Shipping parcel tracker custom component for Home Assistant."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import DragonflyApiClient
from .const import PLATFORMS
from .coordinator import DragonflyCoordinator, _refresh_interval
from .services import async_setup_services, async_unload_services

_LOGGER = logging.getLogger(__name__)


@dataclass
class DragonflyData:
    """Runtime data attached to the Dragonfly config entry."""

    client: DragonflyApiClient
    coordinator: DragonflyCoordinator


type DragonflyConfigEntry = ConfigEntry[DragonflyData]


async def async_setup_entry(hass: HomeAssistant, entry: DragonflyConfigEntry) -> bool:
    """Set up Dragonfly from a config entry."""
    # No auth: Dragonfly tracking is public, so the HA-managed session is fine.
    client = DragonflyApiClient(async_get_clientsession(hass))
    coordinator = DragonflyCoordinator(hass, client, entry)

    # Fetch initial data here, before forwarding to platforms. Raising
    # ConfigEntryNotReady from a forwarded platform is too late for HA to catch
    # cleanly (it logs a warning and half-sets-up the entry); doing the first
    # refresh here lets a transient failure fail the whole entry so HA retries
    # it with backoff.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = DragonflyData(client=client, coordinator=coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Apply option changes (added/removed parcels, interval, history) live via
    # a coordinator refresh — no reload — so per-parcel sensors appear and
    # disappear immediately. The update listener does NOT reload, so it does
    # not trip the config-entry-listener deprecation.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    async_setup_services(hass)

    return True


async def _async_options_updated(
    hass: HomeAssistant, entry: DragonflyConfigEntry
) -> None:
    """Apply changed options: retune the interval and refresh the coordinator."""
    coordinator = entry.runtime_data.coordinator
    coordinator.update_interval = _refresh_interval(entry)
    await coordinator.async_request_refresh()


async def async_unload_entry(hass: HomeAssistant, entry: DragonflyConfigEntry) -> bool:
    """Unload the Dragonfly config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    # Single-instance integration (single_config_entry), so the services can
    # always go when the entry unloads.
    async_unload_services(hass)
    return True
