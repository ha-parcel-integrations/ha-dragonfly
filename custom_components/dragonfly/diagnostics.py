"""Diagnostics support for the Dragonfly Shipping parcel tracker integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import DragonflyConfigEntry

TO_REDACT = {
    # our own canonical fields
    "tracking_code",
    "barcode",
    "sender",
    "url",
    # Dragonfly payload fields
    "tracking_id",
    "client_code",
    "driver_name",
    "driver",
    "client",
    # package_location nests the delivery address (street/postcode/city) and
    # GPS coordinates — redact the whole block.
    "package_location",
    "address",
    "postal_code",
    "postalCode",
    "city",
    "street",
    "email",
    "name",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: DragonflyConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the Dragonfly config entry."""
    coordinator = entry.runtime_data.coordinator

    return {
        "entry_options": async_redact_data(dict(entry.options), TO_REDACT),
        "counts": {
            "incoming_active": len(coordinator.data or []),
            "delivered": len(coordinator.delivered or []),
        },
        "incoming": async_redact_data(coordinator.data or [], TO_REDACT),
        "delivered": async_redact_data(coordinator.delivered or [], TO_REDACT),
    }
