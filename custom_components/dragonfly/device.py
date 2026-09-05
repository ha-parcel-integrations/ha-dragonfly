"""The device every entity of this integration belongs to.

One place, because sensors, the button and the calendar must all land on the
*same* device entry. It used to be defined three times — once per platform —
with the button and calendar docstrings noting that they mirrored the sensor's
copy, which is exactly the kind of duplication that drifts.
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo

from .const import CONF_COUNTRY, COUNTRIES, DEFAULT_COUNTRY, DOMAIN

ATTRIBUTION = "Data provided by Dragonfly"


def build_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Return the DeviceInfo shared by every entity of the Dragonfly hub."""
    # Device name stays the bare "Dragonfly" — has_entity_name derives the
    # default entity_id from it, and existing installs must not get renamed
    # entities the moment this option ships. The country is still visible via
    # the config entry title (set at setup) and the configuration_url below.
    country = entry.options.get(CONF_COUNTRY, DEFAULT_COUNTRY)
    host = COUNTRIES.get(country, COUNTRIES[DEFAULT_COUNTRY])["host"]
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="Dragonfly",
        manufacturer="Dragonfly Shipping",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url=f"https://{host}",
    )
