"""Tests for the Dragonfly config and options flow."""
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dragonfly.config_flow import (
    normalize_tracking_code,
    valid_tracking_code,
)
from custom_components.dragonfly.const import (
    CONF_COUNTRY,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DOMAIN,
)


def test_normalize_tracking_code_strips_and_uppercases():
    assert normalize_tracking_code("intlcm b2c-000123") == "INTLCMB2C000123"
    assert normalize_tracking_code("") == ""
    assert normalize_tracking_code(None) == ""


def test_valid_tracking_code_bounds():
    assert valid_tracking_code("INTLCMB2C000123456")
    assert not valid_tracking_code("ABC")  # too short
    assert not valid_tracking_code("A" * 31)  # too long


async def test_user_flow_shows_country_form(hass):
    """No account, no postcode — the only thing setup asks is the country."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "user"


async def test_user_flow_creates_hub_defaulting_to_nl(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_COUNTRY: "nl"}
    )
    assert result["type"] == "create_entry"
    assert result["title"] == "Dragonfly (NL)"
    assert result["options"][CONF_COUNTRY] == "NL"
    assert result["options"][CONF_PARCELS] == []


async def test_user_flow_creates_hub_for_a_chosen_country(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_COUNTRY: "au"}
    )
    assert result["type"] == "create_entry"
    assert result["title"] == "Dragonfly (AU)"
    assert result["options"][CONF_COUNTRY] == "AU"


async def test_second_hub_rejected(hass):
    MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == "abort"
    # single_config_entry in the manifest aborts before the flow runs.
    assert result["reason"] == "single_instance_allowed"


def _hub(parcels: list[dict]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options={CONF_PARCELS: parcels},
    )


def _init_input(
    *, add="", remove=None, history=False,
    filter_type="days", amount=7,
) -> dict:
    """Build the sectioned options-form submission."""
    parcels: dict = {"add": add}
    if remove is not None:
        parcels["remove"] = remove
    return {
        "parcels": parcels,
        "delivered": {
            CONF_DELIVERED_FILTER_TYPE: filter_type,
            CONF_DELIVERED_FILTER_AMOUNT: amount,
        },
        "history": {CONF_INCLUDE_HISTORY: history},
    }


async def _open_options_step(hass, entry, step_id: str):
    """Start the options flow and select one of its two top-level routes."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "menu"
    assert result["menu_options"] == ["parcels", "settings"]
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step_id}
    )


async def test_options_parcel_list_can_be_cleared(hass):
    """A submitted empty list removes the final manually tracked parcel."""
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_PARCELS: [{CONF_TRACKING_CODE: "EXAMPLE111111"}]})
    entry.add_to_hass(hass)
    result = await _open_options_step(hass, entry, "parcels")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tracking_codes": []}
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_PARCELS] == []


async def test_options_settings_preserve_parcel_list(hass):
    """Saving settings must never replace the manually tracked parcel list."""
    parcels = [{CONF_TRACKING_CODE: "EXAMPLE111111"}]
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_PARCELS: parcels})
    entry.add_to_hass(hass)
    result = await _open_options_step(hass, entry, "settings")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_DELIVERED_FILTER_TYPE: "days", CONF_DELIVERED_FILTER_AMOUNT: 7, CONF_INCLUDE_HISTORY: False}
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_PARCELS] == parcels
