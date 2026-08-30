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

# Every optional key the parcel contract defines. CAPABILITIES below must be a
# subset of this — it exists so a typo in CAPABILITIES fails a test instead of
# silently dropping this carrier off a table on the docs site.
KNOWN_CAPABILITIES = frozenset(
    {"weight", "dimensions", "delivery_window", "pickup_point", "url", "history"}
)

# Which optional contract fields this carrier's API actually populates — feeds
# the comparison table on the docs site. Keep in lockstep with
# normalize_parcel() in parcels.py: everything not listed here comes back as a
# literal None there. Dragonfly delivers to the door with no parcel-shop
# network and never exposes weight or dimensions.
CAPABILITIES = frozenset({"delivery_window", "url", "history"})

# Public Dragonfly Shipping tracking endpoint (no auth) — the same Cloudflare
# worker every country's consumer site calls, one worker deployment per
# country's own domain (see COUNTRIES below). Keyed on the tracking code
# alone: no postal code, no account. Always answers HTTP 200 with a JSON
# envelope; ``success`` + ``data.code`` distinguish a hit from an unknown
# code (``"not_found"``).
TRACKING_API_URL_TEMPLATE = "https://{host}/cfworker/v3/tracking/{tracking_code}/"

# The country a hub was set up for, chosen once at setup and not editable
# afterward (tracked parcels are keyed to one backend). Dragonfly Shipping is
# an Intelcom brand; NL is the original consumer site and stays the default
# for entries created before this option existed.
CONF_COUNTRY = "country"
DEFAULT_COUNTRY = "NL"

# code -> {host, tracking_url, label_languages}. Every country runs the exact
# same cfworker platform on its own domain (confirmed by control-test: same
# data-url pattern, byte-identical not_found envelope) — only the host, the
# consumer deep-link and the label language differ.
#
# ``label_languages`` is the preference order for the API's per-language
# status labels (``labels.shortLabel.{lang}``): NL's site is Dutch-first with
# an English fallback; AU's is English-only; CA's is officially bilingual
# (hreflang carries both en-CA and fr-CA, fr-CA as x-default) but no real CA
# payload has been captured yet to confirm whether the backend actually adds
# an ``.fr`` label key, so English leads and French is a same-shape fallback
# rather than a confirmed preference.
#
# AU/CA payload shape (step catalogue, field mapping, timestamp format) is
# assumed identical to NL's, not yet confirmed on a real parcel.
COUNTRIES: dict[str, dict[str, object]] = {
    "NL": {
        "host": "dragonflyshipping.nl",
        "tracking_url": "https://dragonflyshipping.nl/nl/volg-je-pakket/?tracking-id={tracking_code}",
        "label_languages": ("nl", "en"),
    },
    "AU": {
        "host": "dragonflyshipping.com.au",
        "tracking_url": "https://dragonflyshipping.com.au/track-your-package/?tracking-id={tracking_code}",
        "label_languages": ("en",),
    },
    "CA": {
        "host": "intelcom.ca",
        "tracking_url": "https://intelcom.ca/en/track-your-package/?tracking-id={tracking_code}",
        "label_languages": ("en", "fr"),
    },
}

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

# Dynamic, status-driven polling — unconditional, no user-facing interval
# option. See carrier-research/dynamic-polling.md for the full algorithm and
# the reasoning behind it.
#
# Quiet window: no polling between these local hours except the two anchors
# below, for overnight / end-of-day catch-up.
QUIET_WINDOW_START_HOUR = 0
QUIET_WINDOW_END_HOUR = 6

# Cadence while polling is active (minutes). Hot = a tracked, not-yet-delivered
# parcel is out_for_delivery within HOT_LOOKAHEAD_HOURS of its planned_from (or
# has no planned_from at all); mid = anything else still in flight. This is a
# barcode-based coordinator (Section 2.1): when every tracked parcel is
# delivered, or nothing is tracked, polling stops entirely instead of falling
# to the mid tier — see coordinator.py's ``_hottest_tier_minutes``.
HOT_INTERVAL_MINUTES = 15
MID_INTERVAL_MINUTES = 45
HOT_LOOKAHEAD_HOURS = 1

# Small, stable per-install offset added to every computed interval so
# different installs don't all hit an anchor or tier boundary at the same
# second. Deterministic (hash of the config entry id), not random.
STAGGER_MINUTES = 7

# Per-parcel status history is opt-in and off by default, kept identical to
# the other suite carriers. Dragonfly returns the timeline (``status_list``)
# in the same call, so no extra request is involved either way.
CONF_INCLUDE_HISTORY = "include_history"
DEFAULT_INCLUDE_HISTORY = False

# Cap each parcel's history to the most recent N events so the attribute
# stays well under HA's ~16 KB state-attribute limit.
HISTORY_MAX_EVENTS = 20
