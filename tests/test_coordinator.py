"""Tests for the Dragonfly coordinator logic."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dragonfly.api import DragonflyApiError
from custom_components.dragonfly.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DOMAIN,
    ParcelStatus,
)
from custom_components.dragonfly.coordinator import (
    DragonflyCoordinator,
    build_history,
    map_event_status,
    map_parcel_status,
    normalize_parcel,
    sort_parcels_by_ts,
    status_label,
)


def _status(step: int, ts: str, nl: str, en: str) -> dict:
    return {
        "status": f"STATUS_{step}",
        "statusCode": f"CODE_{step}",
        "step": step,
        "timestamp": ts,
        "labels": {
            "shortLabel": {"nl": nl, "en": en},
            "longLabel": {"nl": f"{nl}.", "en": f"{en}."},
        },
    }


def _delivered_sample(code: str = "INTLCMB2C000123456") -> dict:
    """A representative Dragonfly tracking result for a delivered parcel."""
    last = _status(4, "2026-04-29T13:12:42Z", "Afgeleverd", "Delivered")
    last.update({"isDelivered": True, "showEta": False, "etaType": "none",
                 "task_type": "last_mile_delivery"})
    return {
        "tracking_id": code,
        "client_code": "ACME",
        "driver_name": "Piet",
        "is_green_task": False,
        "last_status": last,
        "public_eta": {"from": None, "to": None, "min": 8, "max": 22},
        "status_list": [
            _status(4, "2026-04-29T13:12:42Z", "Afgeleverd", "Delivered"),
            _status(3, "2026-04-29T08:46:00Z", "Bij de bezorger", "Out for delivery"),
            _status(2, "2026-04-28T15:52:17Z", "In het sorteercentrum", "At the sorting facility"),
            _status(1, "2026-04-27T23:03:58Z", "Zending ontvangen", "Parcel received"),
        ],
    }


def _active_sample(code: str = "INTLCMB2C000999999") -> dict:
    """An out-for-delivery parcel with an ETA window."""
    sample = _delivered_sample(code)
    last = _status(3, "2026-04-29T08:46:00Z", "Bij de bezorger", "Out for delivery")
    last.update({"isDelivered": False, "showEta": True, "etaType": "time",
                 "task_type": "last_mile_delivery"})
    sample["last_status"] = last
    sample["public_eta"] = {
        "from": "2026-04-29T13:00:00Z",
        "to": "2026-04-29T15:00:00Z",
        "min": 8,
        "max": 22,
    }
    sample["status_list"] = sample["status_list"][1:]
    return sample


# ---------------------------------------------------------------------------
# map_parcel_status / map_event_status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "step,expected",
    [
        (1, ParcelStatus.REGISTERED),
        (2, ParcelStatus.IN_TRANSIT),
        (3, ParcelStatus.OUT_FOR_DELIVERY),
        (4, ParcelStatus.DELIVERED),
    ],
)
def test_map_parcel_status_known(step, expected):
    assert map_parcel_status(step) == expected


def test_map_parcel_status_none_is_unknown():
    assert map_parcel_status(None) == ParcelStatus.UNKNOWN


def test_map_parcel_status_negative_is_problem():
    """The site paints a negative step red — the exception state."""
    assert map_parcel_status(-3) == ParcelStatus.PROBLEM


def test_map_parcel_status_unmapped_is_unknown():
    assert map_parcel_status(99) == ParcelStatus.UNKNOWN


def test_map_event_status_none_negative_and_unmapped():
    assert map_event_status(None) is None
    assert map_event_status(98) is None
    assert map_event_status(-2) == ParcelStatus.PROBLEM
    assert map_event_status(4) == ParcelStatus.DELIVERED


def test_unmapped_step_warns_only_once():
    # Second call hits the "already logged" early return branch.
    assert map_parcel_status(97) == ParcelStatus.UNKNOWN
    assert map_parcel_status(97) == ParcelStatus.UNKNOWN


# ---------------------------------------------------------------------------
# status_label
# ---------------------------------------------------------------------------


def test_status_label_prefers_dutch():
    status = _status(3, "2026-04-29T08:46:00Z", "Bij de bezorger", "Out for delivery")
    assert status_label(status) == "Bij de bezorger"


def test_status_label_falls_back_to_english():
    status = _status(3, "2026-04-29T08:46:00Z", "", "Out for delivery")
    assert status_label(status) == "Out for delivery"


def test_status_label_legacy_top_level_labels():
    """Older payloads carry the label dicts directly on the status object."""
    status = {"shortLabel": {"nl": "Onderweg"}}
    assert status_label(status) == "Onderweg"


def test_status_label_none_cases():
    assert status_label(None) is None
    assert status_label({}) is None
    assert status_label({"labels": {"shortLabel": {}}}) is None


def test_status_label_strips_link_markup():
    status = {"labels": {"shortLabel": {"nl": "Zie [link https://x.example]de site[/link]"}}}
    assert status_label(status) == "Zie de site"


def test_status_label_fills_address_tokens():
    status = {
        "labels": {"shortLabel": {"nl": "Afgeleverd in {city}"}},
        "package_location": {"address": {"city": "Rotterdam"}},
    }
    assert status_label(status) == "Afgeleverd in Rotterdam"


def test_status_label_keeps_unknown_tokens():
    status = {"labels": {"shortLabel": {"nl": "Bij {pickup_name}"}},
              "package_location": {"address": {"city": "Rotterdam"}}}
    assert status_label(status) == "Bij {pickup_name}"


# ---------------------------------------------------------------------------
# build_history
# ---------------------------------------------------------------------------


def test_build_history_maps_status_list_oldest_to_newest():
    history = build_history(_delivered_sample()["status_list"])
    assert len(history) == 4
    assert history[0]["raw_status"] == "Zending ontvangen"
    assert history[0]["status"] == ParcelStatus.REGISTERED
    assert history[-1]["status"] == ParcelStatus.DELIVERED


def test_build_history_caps_to_max_events():
    status_list = [
        _status(2, f"2026-04-{d:02d}T10:00:00Z", "x", "x") for d in range(1, 26)
    ]
    assert len(build_history(status_list, max_events=20)) == 20


def test_build_history_handles_missing_and_empty():
    assert build_history(None) == []
    assert build_history([{"step": 1}]) == []  # no timestamp -> skipped
    assert build_history(["not-a-dict"]) == []


def test_build_history_keeps_unparseable_timestamp_last():
    status_list = [
        _status(1, "2026-04-24T10:00:00Z", "ok", "ok"),
        _status(2, "not-a-date", "raar", "weird"),
    ]
    history = build_history(status_list)
    assert len(history) == 2
    assert history[-1]["raw_status"] == "raar"


# ---------------------------------------------------------------------------
# normalize_parcel
# ---------------------------------------------------------------------------


def test_normalize_delivered_parcel():
    parcel = normalize_parcel(_delivered_sample())
    assert parcel["carrier"] == "Dragonfly"
    assert parcel["barcode"] == "INTLCMB2C000123456"
    assert parcel["sender"] == "ACME"
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["raw_status"] == "Afgeleverd"
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-04-29T13:12:42Z"
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None
    assert parcel["url"] == (
        "https://dragonflyshipping.nl/nl/volg-je-pakket/"
        "?tracking-id=INTLCMB2C000123456"
    )
    assert parcel["history"] is None  # opt-in, default off


def test_normalize_history_opt_in():
    parcel = normalize_parcel(_delivered_sample(), include_history=True)
    assert len(parcel["history"]) == 4
    assert parcel["history"][0]["status"] == ParcelStatus.REGISTERED


def test_normalize_active_parcel_has_window():
    parcel = normalize_parcel(_active_sample())
    assert parcel["status"] == ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["delivered"] is False
    assert parcel["planned_from"] == "2026-04-29T13:00:00Z"
    assert parcel["planned_to"] == "2026-04-29T15:00:00Z"


def test_normalize_hides_eta_when_site_would(hass):
    """showEta false or etaType none suppresses the window, like the site."""
    sample = _active_sample()
    sample["last_status"]["showEta"] = False
    parcel = normalize_parcel(sample)
    assert parcel["planned_from"] is None

    sample = _active_sample()
    sample["last_status"]["etaType"] = "none"
    parcel = normalize_parcel(sample)
    assert parcel["planned_from"] is None


def test_normalize_pending_placeholder():
    parcel = normalize_parcel({"tracking_id": "INTLCM123456", "last_status": None})
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["delivered"] is False
    assert parcel["raw_status"] is None
    assert parcel["history"] is None


def test_normalize_delivered_via_flag_without_step():
    raw = _delivered_sample()
    raw["last_status"]["step"] = None
    parcel = normalize_parcel(raw)
    assert parcel["delivered"] is True  # last_status.isDelivered


def test_normalize_pickup_task():
    raw = _active_sample()
    raw["last_status"]["task_type"] = "last_mile_pickup"
    parcel = normalize_parcel(raw)
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] is None


def test_normalize_missing_client_code_is_none():
    raw = _active_sample()
    raw["client_code"] = ""
    parcel = normalize_parcel(raw)
    assert parcel["sender"] is None


def test_normalize_real_payload_epoch_ms_and_eta_fallback():
    """Modeled on a live out-for-delivery parcel (July 2026).

    The real worker stamps statuses in epoch **milliseconds** and left
    ``public_eta: null`` while carrying a concrete top-level ``eta`` whose
    ``buffered_eta`` was the exact same instant (a point estimate, not a
    window).
    """
    raw = {
        "version": "v3",
        "tracking_id": "AMZNL000000000000",
        "eta": "2026-07-16T17:50:47.000000+02:00",
        "buffered_eta": "2026-07-16T15:50:47.000Z",
        "public_eta": None,
        "client_code": None,
        "driver_name": None,
        "last_status": {
            "timestamp": 1784203767167,
            "task_type": "last_mile_delivery",
            "status": 300,
            "statusCode": 300,
            "label": "Loaded",
            "step": 3,
            "showEta": True,
            "etaType": "time",
            "isDelivered": False,
            "labels": {
                "shortLabel": {"en": "On our way to you!", "fr": "", "nl": "We zijn onderweg naar je!"},
            },
        },
        "status_list": [
            {"step": 3, "status": 300, "timestamp": 1784203767167,
             "labels": {"shortLabel": {"nl": "We zijn onderweg naar je!"}}},
            {"step": 1, "status": 0, "timestamp": 1784153769791,
             "labels": {"shortLabel": {"nl": "Je pakket is veilig bij Dragonfly"}}},
        ],
    }
    parcel = normalize_parcel(raw, include_history=True)
    assert parcel["status"] == ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["raw_status"] == "We zijn onderweg naar je!"
    assert parcel["sender"] is None
    # public_eta null → top-level eta; buffered_eta == eta → no window end
    assert parcel["planned_from"] == "2026-07-16T17:50:47.000000+02:00"
    assert parcel["planned_to"] is None
    # epoch-ms history timestamps become ISO strings, oldest first
    assert parcel["history"][0]["timestamp"] == "2026-07-15T22:16:09.791000+00:00"
    assert parcel["history"][0]["status"] == ParcelStatus.REGISTERED
    assert parcel["history"][-1]["status"] == ParcelStatus.OUT_FOR_DELIVERY


def test_normalize_delivered_at_converts_epoch_ms():
    raw = _delivered_sample()
    raw["last_status"]["timestamp"] = 1784203767167
    parcel = normalize_parcel(raw)
    assert parcel["delivered_at"] == "2026-07-16T12:09:27.167000+00:00"


def test_normalize_distinct_buffered_eta_becomes_window_end():
    raw = _active_sample()
    raw["public_eta"] = None
    raw["eta"] = "2026-07-16T16:00:00Z"
    raw["buffered_eta"] = "2026-07-16T18:00:00Z"
    parcel = normalize_parcel(raw)
    assert parcel["planned_from"] == "2026-07-16T16:00:00Z"
    assert parcel["planned_to"] == "2026-07-16T18:00:00Z"


# ---------------------------------------------------------------------------
# sort_parcels_by_ts
# ---------------------------------------------------------------------------


def test_sort_parcels_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "planned_from": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "planned_from": None},
        {"barcode": "c", "planned_from": "2026-05-01T10:00:00Z"},
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "planned_from")]
    assert ordered == ["c", "a", "b"]


# ---------------------------------------------------------------------------
# DragonflyCoordinator._async_update_data
# ---------------------------------------------------------------------------


def _entry_with(parcels: list[dict]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        # Keep-most-recent-100 so the delivered-retention filter never trims
        # the (old, fixed-date) sample parcels these tests assert on.
        options={
            CONF_PARCELS: parcels,
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
        },
        unique_id=DOMAIN,
    )


async def test_update_merges_multiple_parcels(hass):
    entry = _entry_with([
        {CONF_TRACKING_CODE: "INTLCMB2C000999999"},
        {CONF_TRACKING_CODE: "INTLCMB2C000123456"},
    ])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.side_effect = lambda code: (
        _active_sample() if code == "INTLCMB2C000999999" else _delivered_sample()
    )
    coordinator = DragonflyCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    assert len(data) == 1  # one active
    assert data[0]["barcode"] == "INTLCMB2C000999999"
    assert len(coordinator.delivered) == 1
    assert coordinator.last_success_time is not None


async def test_update_not_found_shows_pending_placeholder(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: "INTLCM999999"}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = None  # not_found
    coordinator = DragonflyCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    assert len(data) == 1
    assert data[0]["barcode"] == "INTLCM999999"
    assert data[0]["status"] == ParcelStatus.UNKNOWN


async def test_update_keeps_cached_on_error(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: "INTLCMB2C000123456"}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = _delivered_sample()
    coordinator = DragonflyCoordinator(hass, client, entry)
    await coordinator._async_update_data()  # populates cache

    client.async_get_parcel.side_effect = DragonflyApiError("HTTP 500")
    await coordinator._async_update_data()  # error -> cached raw reused
    assert len(coordinator.delivered) == 1


async def test_update_all_fail_raises(hass):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    entry = _entry_with([{CONF_TRACKING_CODE: "INTLCMB2C000123456"}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.side_effect = DragonflyApiError("HTTP 500")
    coordinator = DragonflyCoordinator(hass, client, entry)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_update_skips_items_missing_fields(hass):
    entry = _entry_with([
        {CONF_TRACKING_CODE: ""},  # skipped
        {CONF_TRACKING_CODE: "INTLCMB2C000123456"},
    ])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = _delivered_sample()
    coordinator = DragonflyCoordinator(hass, client, entry)

    await coordinator._async_update_data()
    assert client.async_get_parcel.await_count == 1  # empty item never fetched


async def test_update_backfills_missing_tracking_id(hass):
    """An edge payload without tracking_id keeps the requested code as key."""
    entry = _entry_with([{CONF_TRACKING_CODE: "INTLCM424242"}])
    entry.add_to_hass(hass)
    sample = _active_sample()
    del sample["tracking_id"]
    client = AsyncMock()
    client.async_get_parcel.return_value = sample
    coordinator = DragonflyCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()
    assert data[0]["barcode"] == "INTLCM424242"


async def test_update_event_carries_device_id(hass):
    from homeassistant.helpers import device_registry as dr

    entry = _entry_with([{CONF_TRACKING_CODE: "INTLCMB2C000999999"}])
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
    )
    client = AsyncMock()
    coordinator = DragonflyCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_status_changed", lambda e: events.append(e))

    in_transit = _active_sample("INTLCMB2C000999999")
    in_transit["last_status"]["step"] = 2
    client.async_get_parcel.return_value = in_transit
    await coordinator._async_update_data()
    client.async_get_parcel.return_value = _active_sample("INTLCMB2C000999999")
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert events[0].data["device_id"] == device.id


async def test_update_fires_status_changed_event(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: "INTLCMB2C000999999"}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = DragonflyCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_status_changed", lambda e: events.append(e))

    # First refresh: in_transit (step 2), events suppressed.
    in_transit = _active_sample()
    in_transit["last_status"]["step"] = 2
    client.async_get_parcel.return_value = in_transit
    await coordinator._async_update_data()

    # Second refresh: out_for_delivery (step 3) — still active, status changed.
    client.async_get_parcel.return_value = _active_sample()
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["new_status"] == ParcelStatus.OUT_FOR_DELIVERY


async def test_update_fires_delivered_event_not_status_changed(hass):
    """The hop to delivered fires parcel_delivered — never status_changed."""
    entry = _entry_with([{CONF_TRACKING_CODE: "INTLCMB2C000999999"}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = DragonflyCoordinator(hass, client, entry)

    delivered = []
    changed = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", lambda e: delivered.append(e))
    hass.bus.async_listen(f"{DOMAIN}_parcel_status_changed", lambda e: changed.append(e))

    client.async_get_parcel.return_value = _active_sample("INTLCMB2C000999999")
    await coordinator._async_update_data()
    client.async_get_parcel.return_value = _delivered_sample("INTLCMB2C000999999")
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert changed == []
    assert len(delivered) == 1
    assert delivered[0].data["barcode"] == "INTLCMB2C000999999"
    assert delivered[0].data["status"] == ParcelStatus.DELIVERED


async def test_no_events_for_parcel_first_seen_delivered(hass):
    """A parcel already delivered when first tracked fires neither registered nor delivered."""
    entry = _entry_with([{CONF_TRACKING_CODE: "INTLCMB2C000999999"}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.side_effect = lambda code: (
        _active_sample(code) if code == "INTLCMB2C000999999" else _delivered_sample(code)
    )
    coordinator = DragonflyCoordinator(hass, client, entry)

    fired = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", lambda e: fired.append(e))
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", lambda e: fired.append(e))

    await coordinator._async_update_data()  # first refresh: seeds state

    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_PARCELS: [
                {CONF_TRACKING_CODE: "INTLCMB2C000999999"},
                {CONF_TRACKING_CODE: "INTLCMB2C000123456"},
            ],
        },
    )
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert fired == []


async def test_update_fires_registered_event_for_new_parcel(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: "INTLCMB2C000999999"}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = _active_sample("INTLCMB2C000999999")
    coordinator = DragonflyCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", lambda e: events.append(e))

    await coordinator._async_update_data()  # first refresh: suppressed

    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_PARCELS: [
                {CONF_TRACKING_CODE: "INTLCMB2C000999999"},
                {CONF_TRACKING_CODE: "INTLCMB2C000888888"},
            ],
        },
    )
    client.async_get_parcel.side_effect = lambda code: _active_sample(code)
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["barcode"] == "INTLCMB2C000888888"


async def test_update_fires_delivery_time_changed_event(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: "INTLCMB2C000999999"}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = DragonflyCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_delivery_time_changed", lambda e: events.append(e)
    )

    client.async_get_parcel.return_value = _active_sample()
    await coordinator._async_update_data()  # first refresh: suppressed

    moved = _active_sample()
    moved["public_eta"]["from"] = "2026-04-29T16:00:00Z"
    moved["public_eta"]["to"] = "2026-04-29T18:00:00Z"
    client.async_get_parcel.return_value = moved
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["new_planned_from"] == "2026-04-29T16:00:00Z"


async def test_update_cached_only_poll_does_not_stamp_last_success(hass):
    """A poll served entirely from cache must not look like a success."""
    entry = _entry_with([{CONF_TRACKING_CODE: "INTLCMB2C000123456"}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = _delivered_sample()
    coordinator = DragonflyCoordinator(hass, client, entry)
    await coordinator._async_update_data()
    stamp = coordinator.last_success_time
    assert stamp is not None

    client.async_get_parcel.side_effect = DragonflyApiError("HTTP 500")
    await coordinator._async_update_data()  # served from cache
    assert coordinator.last_success_time == stamp


async def test_delivered_filter_days_and_count(hass):
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    old = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")
    delivered = [
        {"barcode": "RECENT", "delivered_at": recent},
        {"barcode": "OLD", "delivered_at": old},
    ]

    entry = _entry_with([])
    entry.add_to_hass(hass)
    coordinator = DragonflyCoordinator(hass, AsyncMock(), entry)

    # days: 7-day window drops the 30-day-old one.
    hass.config_entries.async_update_entry(
        entry, options={CONF_DELIVERED_FILTER_TYPE: "days", CONF_DELIVERED_FILTER_AMOUNT: 7}
    )
    kept = coordinator._apply_delivered_filter(delivered)
    assert {p["barcode"] for p in kept} == {"RECENT"}

    # parcels: keep only the most recent 1.
    hass.config_entries.async_update_entry(
        entry,
        options={CONF_DELIVERED_FILTER_TYPE: "parcels", CONF_DELIVERED_FILTER_AMOUNT: 1},
    )
    kept = coordinator._apply_delivered_filter(delivered)
    assert kept == delivered[:1]


async def test_update_prunes_cache_for_untracked_parcels(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: "INTLCMB2C000123456"}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = _delivered_sample()
    coordinator = DragonflyCoordinator(hass, client, entry)
    coordinator._raw_cache["GONE"] = {"tracking_id": "GONE", "last_status": None}

    await coordinator._async_update_data()

    assert "GONE" not in coordinator._raw_cache
    assert "INTLCMB2C000123456" in coordinator._raw_cache


async def test_update_fetches_parcels_concurrently(hass):
    """All tracked parcels are fetched via one gather, not one-by-one."""
    import asyncio

    entry = _entry_with([
        {CONF_TRACKING_CODE: "INTLCMB2C000999999"},
        {CONF_TRACKING_CODE: "INTLCMB2C000123456"},
    ])
    entry.add_to_hass(hass)
    in_flight = 0
    peak = 0

    async def _slow_fetch(code):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1
        return _active_sample(code)

    client = AsyncMock()
    client.async_get_parcel.side_effect = _slow_fetch
    coordinator = DragonflyCoordinator(hass, client, entry)

    await coordinator._async_update_data()
    assert peak == 2
