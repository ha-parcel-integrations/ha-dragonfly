"""Constants for the Dragonfly Shipping parcel tracker integration."""
from enum import StrEnum

from homeassistant.const import Platform

DOMAIN = "dragonfly"


class ParcelStatus(StrEnum):
    """Carrier-agnostic parcel status.

    Mirrors the enum the other suite integrations (DHL, DPD, PostNL, GLS)
    publish on the ``status`` field of each normalised parcel, so
    cross-carrier automations and the aggregator can target
    ``status: out_for_delivery`` regardless of carrier. Listed in roughly
    the order a parcel moves through.
    """

    REGISTERED = "registered"               # Sender announced the parcel; not handed over yet
    IN_TRANSIT = "in_transit"               # In the carrier's network
    OUT_FOR_DELIVERY = "out_for_delivery"   # On a delivery vehicle today
    AT_PICKUP_POINT = "at_pickup_point"     # Ready to collect at a pickup location
    DELIVERED = "delivered"                 # Handed over
    RETURNING = "returning"                 # Failed delivery, going back to sender
    PROBLEM = "problem"                     # Carrier reports an exception/issue
    UNKNOWN = "unknown"                     # Raw status we have not mapped yet


PLATFORMS = [Platform.BUTTON, Platform.CALENDAR, Platform.SENSOR]

# Public Dragonfly Shipping tracking endpoint (no auth) — the same Cloudflare
# worker the consumer site (dragonflyshipping.nl, an Intelcom brand) calls.
# Keyed on the tracking code alone: no postal code, no account. Always
# answers HTTP 200 with a JSON envelope; ``success`` + ``data.code``
# distinguish a hit from an unknown code (``"not_found"``).
TRACKING_API_URL = "https://dragonflyshipping.nl/cfworker/v3/tracking/{tracking_code}/"

# Consumer tracking deep-link, used to populate the parcel's ``url`` field.
TRACKING_URL = "https://dragonflyshipping.nl/nl/volg-je-pakket/?tracking-id={tracking_code}"

# Label language used for the human-readable ``raw_status`` texts. The API
# embeds its labels per language (``nl`` / ``en``); Dutch first matches the
# NL-only consumer site, English is the fallback.
LABEL_LANGUAGES = ("nl", "en")

# Tracked parcels live in the config entry options as a list of
# ``{tracking_code}`` dicts — Dragonfly has no account/feed, the user enters
# the codes themselves. Kept as dicts so future per-parcel fields slot in
# without an options migration.
CONF_PARCELS = "parcels"
CONF_TRACKING_CODE = "tracking_code"

# Delivered-parcels retention: keep delivered parcels visible for the last N
# days, or keep only the N most recent — mirrors the other suite carriers.
CONF_DELIVERED_FILTER_TYPE = "delivered_filter_type"
CONF_DELIVERED_FILTER_AMOUNT = "delivered_filter_amount"
DEFAULT_DELIVERED_FILTER_TYPE = "days"
DEFAULT_DELIVERED_FILTER_AMOUNT = 7

# Refresh interval (minutes) controls how often the coordinator polls
# Dragonfly. Default 30 min keeps the load on the public endpoint gentle; the
# minimum is 15 min for the same reason. Kept identical to the other suite
# carriers.
CONF_REFRESH_INTERVAL = "refresh_interval"
REFRESH_INTERVAL_OPTIONS = (15, 30, 60, 120, 240)
DEFAULT_REFRESH_INTERVAL = 30

# Per-parcel status history is opt-in and off by default, kept identical to
# the other suite carriers. Dragonfly returns the timeline (``status_list``)
# in the same call, so no extra request is involved either way.
CONF_INCLUDE_HISTORY = "include_history"
DEFAULT_INCLUDE_HISTORY = False

# Cap each parcel's history to the most recent N events so the attribute
# stays well under HA's ~16 KB state-attribute limit.
HISTORY_MAX_EVENTS = 20
