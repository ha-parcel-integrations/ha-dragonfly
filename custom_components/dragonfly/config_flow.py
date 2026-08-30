"""Config flow for the Dragonfly Shipping parcel tracker integration."""

from __future__ import annotations

import logging
import re
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_COUNTRY,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    COUNTRIES,
    DEFAULT_COUNTRY,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    DEFAULT_INCLUDE_HISTORY,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# A Dragonfly Track & Trace code as printed on the shipping confirmation or
# the missed-delivery card. Upper-case alphanumeric; the consumer site strips
# everything else before querying, so we normalise the same way and accept a
# generous length range.
_TRACKING_CODE_RE = re.compile(r"^[A-Z0-9]{6,30}$")

# First-run form: pick which country's Dragonfly backend this hub talks to.
# Selector option values double as hassfest translation keys, which must be
# lowercase — COUNTRIES/CONF_COUNTRY's actual stored value stays upper-case
# everywhere else, so this list is a display-only lowercase mirror.
# async_step_user upper-cases the submitted value right back before using it.
_COUNTRY_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=[code.lower() for code in COUNTRIES],
        translation_key=CONF_COUNTRY,
        mode=selector.SelectSelectorMode.DROPDOWN,
    )
)


def normalize_tracking_code(value: str) -> str:
    """Return the tracking code upper-cased with separators stripped.

    Mirrors the consumer site's sanitiser (uppercase, drop everything that is
    not ``A-Z0-9``), so pasted codes with spaces or dashes still work.
    """
    return re.sub(r"[^A-Z0-9]+", "", (value or "").upper())


def valid_tracking_code(value: str) -> bool:
    """Whether ``value`` looks like a Dragonfly tracking code."""
    return bool(_TRACKING_CODE_RE.match(value))


def _current_parcels(entry: ConfigEntry) -> list[dict[str, str]]:
    """Return a mutable copy of the tracked parcels list."""
    return [dict(item) for item in entry.options.get(CONF_PARCELS, [])]


class DragonflyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the UI-driven configuration flow for the Dragonfly integration."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> DragonflyOptionsFlowHandler:
        """Return the options flow handler."""
        return DragonflyOptionsFlowHandler()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the Dragonfly hub — single instance, one country.

        Dragonfly tracking is keyed on the tracking code alone (no account,
        no postal code), so the only thing setup needs is which country's
        backend to use — NL, AU or CA all run the same cfworker platform on
        their own domain. Parcels are added afterwards via the options flow,
        the ``dragonfly.track_parcel`` service or a dashboard button.
        ``single_config_entry`` in the manifest enforces one hub; the country
        is not editable afterward (tracked parcels are keyed to one backend).
        """
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            country = user_input[CONF_COUNTRY].upper()
            return self.async_create_entry(
                title=f"Dragonfly ({country})",
                data={},
                options={
                    CONF_COUNTRY: country,
                    CONF_PARCELS: [],
                    CONF_DELIVERED_FILTER_TYPE: DEFAULT_DELIVERED_FILTER_TYPE,
                    CONF_DELIVERED_FILTER_AMOUNT: DEFAULT_DELIVERED_FILTER_AMOUNT,
                    CONF_INCLUDE_HISTORY: DEFAULT_INCLUDE_HISTORY,
                },
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_COUNTRY, default=DEFAULT_COUNTRY.lower()
                    ): _COUNTRY_SELECTOR,
                }
            ),
        )


class DragonflyOptionsFlowHandler(OptionsFlow):
    """Manage tracked parcels, history and polling in one sectioned form.

    Mirrors the other suite carriers' section layout (here: ``parcels`` /
    ``delivered`` / ``history`` / ``polling``). Changes apply live via HA's
    options-update listener (which refreshes the coordinator), so new/removed
    per-parcel sensors appear and disappear immediately.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer parcel management separately from integration settings."""
        return self.async_show_menu(
            step_id="init", menu_options=["parcels", "settings"]
        )

    async def async_step_parcels(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle the complete tracked-code list."""
        errors: dict[str, str] = {}
        if user_input is not None:
            codes = list(
                dict.fromkeys(
                    normalize_tracking_code(code)
                    for code in user_input.get("tracking_codes", [])
                    if normalize_tracking_code(code)
                )
            )
            if any(not valid_tracking_code(code) for code in codes):
                errors["base"] = "invalid_tracking_code"
            else:
                return self.async_create_entry(
                    title="",
                    data={
                        **self.config_entry.options,
                        CONF_PARCELS: [{CONF_TRACKING_CODE: code} for code in codes],
                    },
                )

        current_codes = [
            parcel[CONF_TRACKING_CODE] for parcel in _current_parcels(self.config_entry)
        ]
        schema = vol.Schema(
            {
                vol.Optional("tracking_codes"): selector.TextSelector(
                    selector.TextSelectorConfig(multiple=True)
                )
            }
        )
        return self.async_show_form(
            step_id="parcels",
            data_schema=self.add_suggested_values_to_schema(
                schema, {"tracking_codes": current_codes}
            ),
            errors=errors,
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle non-parcel integration settings."""
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    **self.config_entry.options,
                    CONF_DELIVERED_FILTER_TYPE: user_input[CONF_DELIVERED_FILTER_TYPE],
                    CONF_DELIVERED_FILTER_AMOUNT: int(
                        user_input[CONF_DELIVERED_FILTER_AMOUNT]
                    ),
                    CONF_INCLUDE_HISTORY: bool(user_input[CONF_INCLUDE_HISTORY]),
                },
            )

        current = self.config_entry.options
        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DELIVERED_FILTER_TYPE,
                        default=current.get(
                            CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
                        ),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=["days", "parcels"],
                            translation_key=CONF_DELIVERED_FILTER_TYPE,
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    ),
                    vol.Required(
                        CONF_DELIVERED_FILTER_AMOUNT,
                        default=current.get(
                            CONF_DELIVERED_FILTER_AMOUNT,
                            DEFAULT_DELIVERED_FILTER_AMOUNT,
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=1, max=365, step=1, mode=selector.NumberSelectorMode.BOX
                        )
                    ),
                    vol.Required(
                        CONF_INCLUDE_HISTORY,
                        default=current.get(
                            CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY
                        ),
                    ): selector.BooleanSelector(),
                }
            ),
        )
