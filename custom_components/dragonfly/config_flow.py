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
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector

from .const import (
    CONF_COUNTRY,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    CONF_REFRESH_INTERVAL,
    CONF_TRACKING_CODE,
    COUNTRIES,
    DEFAULT_COUNTRY,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    DEFAULT_INCLUDE_HISTORY,
    DEFAULT_REFRESH_INTERVAL,
    DOMAIN,
    REFRESH_INTERVAL_OPTIONS,
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


def _interval_selector() -> selector.SelectSelector:
    """Return the refresh-interval dropdown selector (options translated via strings)."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[str(m) for m in REFRESH_INTERVAL_OPTIONS],
            translation_key=CONF_REFRESH_INTERVAL,
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


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
                    CONF_REFRESH_INTERVAL: DEFAULT_REFRESH_INTERVAL,
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
        """Show and handle the single sectioned options form."""
        errors: dict[str, str] = {}
        parcels = _current_parcels(self.config_entry)

        if user_input is not None:
            parcels_section = user_input.get("parcels", {})
            delivered_section = user_input.get("delivered", {})
            history_section = user_input.get("history", {})
            polling_section = user_input.get("polling", {})

            # Remove first, then add — so re-adding a just-removed code works.
            to_remove = set(parcels_section.get("remove", []))
            parcels = [p for p in parcels if p[CONF_TRACKING_CODE] not in to_remove]

            add_code = normalize_tracking_code(parcels_section.get("add") or "")
            if add_code:
                if not valid_tracking_code(add_code):
                    errors["base"] = "invalid_tracking_code"
                elif any(p[CONF_TRACKING_CODE] == add_code for p in parcels):
                    errors["base"] = "already_tracked"
                else:
                    parcels.append({CONF_TRACKING_CODE: add_code})

            if not errors:
                return self.async_create_entry(
                    title="",
                    data={
                        # An options flow's `data` replaces `entry.options`
                        # wholesale rather than merging into it — omitting the
                        # country here would silently reset the hub to NL the
                        # first time a parcel was added (same bug as
                        # ha-parcel-integrations/ha-gls#2).
                        CONF_COUNTRY: self.config_entry.options.get(
                            CONF_COUNTRY, DEFAULT_COUNTRY
                        ),
                        CONF_PARCELS: parcels,
                        CONF_DELIVERED_FILTER_TYPE: delivered_section[
                            CONF_DELIVERED_FILTER_TYPE
                        ],
                        CONF_DELIVERED_FILTER_AMOUNT: int(
                            delivered_section[CONF_DELIVERED_FILTER_AMOUNT]
                        ),
                        CONF_INCLUDE_HISTORY: bool(
                            history_section[CONF_INCLUDE_HISTORY]
                        ),
                        CONF_REFRESH_INTERVAL: int(
                            polling_section[CONF_REFRESH_INTERVAL]
                        ),
                    },
                )

        current = self.config_entry.options

        parcels_fields: dict[Any, Any] = {vol.Optional("add", default=""): str}
        if parcels:
            parcels_fields[vol.Optional("remove", default=[])] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(
                            value=p[CONF_TRACKING_CODE],
                            label=p[CONF_TRACKING_CODE],
                        )
                        for p in parcels
                    ],
                    multiple=True,
                    mode=selector.SelectSelectorMode.LIST,
                )
            )

        schema = vol.Schema(
            {
                vol.Required("parcels"): section(
                    vol.Schema(parcels_fields), {"collapsed": False}
                ),
                vol.Required("delivered"): section(
                    vol.Schema(
                        {
                            vol.Required(
                                CONF_DELIVERED_FILTER_TYPE,
                                default=current.get(
                                    CONF_DELIVERED_FILTER_TYPE,
                                    DEFAULT_DELIVERED_FILTER_TYPE,
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
                        }
                    ),
                    {"collapsed": True},
                ),
                vol.Required("history"): section(
                    vol.Schema(
                        {
                            vol.Required(
                                CONF_INCLUDE_HISTORY,
                                default=current.get(
                                    CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY
                                ),
                            ): selector.BooleanSelector(),
                        }
                    ),
                    {"collapsed": True},
                ),
                vol.Required("polling"): section(
                    vol.Schema(
                        {
                            vol.Required(
                                CONF_REFRESH_INTERVAL,
                                # str(): selector option values are strings, so a
                                # stored int default trips "expected str" on submit.
                                default=str(
                                    current.get(
                                        CONF_REFRESH_INTERVAL, DEFAULT_REFRESH_INTERVAL
                                    )
                                ),
                            ): _interval_selector(),
                        }
                    ),
                    {"collapsed": True},
                ),
            }
        )

        return self.async_show_form(
            step_id="init", data_schema=schema, errors=errors
        )
