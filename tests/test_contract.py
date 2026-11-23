"""The parcel contract's checks, run over every normalised sample."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.dragonfly.const import ParcelStatus
from custom_components.dragonfly.parcels import (
    is_pickup,
    normalize_parcel,
    planned_for_status,
    planned_window,
)

from .payloads import active_sample, delivered_sample

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "contents",
    "status",
    "raw_status",
    "delivered",
    "delivered_at",
    "planned_from",
    "planned_to",
    "pickup",
    "pickup_point",
    "url",
    "weight",
    "dimensions",
    "history",
    "raw",
]

_AMS = ZoneInfo("Europe/Amsterdam")


def _with_eta(**eta) -> dict:
    sample = active_sample()
    sample["public_eta"] = eta
    return sample


SAMPLES = {
    "active": active_sample,
    "delivered": delivered_sample,
    "moment_local": lambda: _with_eta(**{"from": "2099-04-29T13:00:00+02:00"}),
    "moment_utc": lambda: _with_eta(**{"from": "2099-04-29T13:00:00Z"}),
    "whole_day": lambda: _with_eta(**{"from": "2099-04-29T00:00:00+02:00"}),
    "reversed": lambda: _with_eta(
        **{"from": "2099-04-29T15:00:00Z", "to": "2099-04-29T13:00:00Z"}
    ),
    "bare": lambda: {"tracking_id": "1", "last_status": None},
}


@pytest.fixture(params=SAMPLES)
def parcel(request) -> dict:
    return normalize_parcel(SAMPLES[request.param]())


def test_keys_and_order(parcel):
    assert list(parcel) == CANONICAL_KEYS


def test_delivered_follows_status(parcel):
    assert parcel["delivered"] is (parcel["status"] is ParcelStatus.DELIVERED)


def test_delivered_at_only_when_delivered(parcel):
    if not parcel["delivered"]:
        assert parcel["delivered_at"] is None


def test_planned_window_is_ordered_and_anchored(parcel):
    if parcel["planned_to"] is not None:
        assert parcel["planned_from"] is not None
        assert datetime.fromisoformat(parcel["planned_to"]) > datetime.fromisoformat(
            parcel["planned_from"]
        )


def test_planned_is_empty_when_the_parcel_is_done(parcel):
    if parcel["delivered"]:
        assert parcel["planned_from"] is None
        assert parcel["planned_to"] is None


def test_pickup_is_always_false(parcel):
    assert parcel["pickup"] is False
    assert parcel["pickup_point"] is None


def test_a_moment_ends_at_the_end_of_the_day_in_its_own_offset():
    parcel = normalize_parcel(SAMPLES["moment_local"]())
    assert parcel["planned_to"] == "2099-04-29T23:59:59+02:00"


def test_a_whole_day_estimate_covers_the_day():
    parcel = normalize_parcel(SAMPLES["whole_day"]())
    assert parcel["planned_from"] == "2099-04-29T00:00:00+02:00"
    assert parcel["planned_to"] == "2099-04-29T23:59:59+02:00"


async def test_a_bare_utc_moment_ends_the_day_in_the_users_zone(hass):
    await hass.config.async_set_time_zone("Europe/Amsterdam")
    parcel = normalize_parcel(_with_eta(**{"from": "2099-04-29T23:30:00Z"}))
    assert parcel["planned_from"] == "2099-04-29T23:30:00Z"
    assert parcel["planned_to"] == "2099-04-30T23:59:59+02:00"


def test_reversed_window_ends_are_swapped():
    parcel = normalize_parcel(SAMPLES["reversed"]())
    assert parcel["planned_from"] < parcel["planned_to"]


def test_the_delivered_flag_decides_the_status():
    sample = delivered_sample()
    sample["last_status"]["step"] = None
    parcel = normalize_parcel(sample)
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["delivered"] is True


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (
            "2026-04-29T13:00:00+02:00",
            "2026-04-29T15:00:00+02:00",
            ("2026-04-29T13:00:00+02:00", "2026-04-29T15:00:00+02:00"),
        ),
        (
            "2026-04-29T13:00:00+02:00",
            None,
            ("2026-04-29T13:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            "2026-04-29T13:00:00+02:00",
            "2026-04-29T13:00:00+02:00",
            ("2026-04-29T13:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            "2026-04-29T13:00:00",
            None,
            ("2026-04-29T13:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            "2026-04-29",
            None,
            ("2026-04-29T00:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            date(2026, 4, 29),
            date(2026, 4, 29),
            ("2026-04-29T00:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            "2026-04-29",
            "2026-05-01",
            ("2026-04-29T00:00:00+02:00", "2026-05-01T23:59:59+02:00"),
        ),
        (
            "2026-04-29T15:00:00+02:00",
            "2026-04-29T13:00:00+02:00",
            ("2026-04-29T13:00:00+02:00", "2026-04-29T15:00:00+02:00"),
        ),
        (
            None,
            "2026-04-29T13:00:00+02:00",
            ("2026-04-29T13:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            datetime(2026, 4, 29, 13, 0),
            datetime(2026, 4, 29, 15, 0),
            ("2026-04-29T13:00:00+02:00", "2026-04-29T15:00:00+02:00"),
        ),
        (None, None, (None, None)),
        ("garbage", None, (None, None)),
        (12, 13, (None, None)),
    ],
)
def test_planned_window_shapes(start, end, expected):
    assert planned_window(start, end, zone=_AMS) == expected


@pytest.mark.parametrize(
    "status",
    [ParcelStatus.AT_PICKUP_POINT, ParcelStatus.RETURNING, ParcelStatus.DELIVERED],
)
def test_planned_for_status_empties_the_window(status):
    assert planned_for_status(status, "a", "b") == (None, None)


def test_planned_for_status_keeps_the_window_otherwise():
    for status in (
        ParcelStatus.REGISTERED,
        ParcelStatus.IN_TRANSIT,
        ParcelStatus.OUT_FOR_DELIVERY,
        ParcelStatus.PROBLEM,
        ParcelStatus.UNKNOWN,
    ):
        assert planned_for_status(status, "a", "b") == ("a", "b")


def test_is_pickup_sources():
    assert is_pickup(True, ParcelStatus.IN_TRANSIT) is True
    assert is_pickup(None, ParcelStatus.AT_PICKUP_POINT) is True
    assert is_pickup(None, ParcelStatus.DELIVERED, [None, ParcelStatus.AT_PICKUP_POINT])
    assert is_pickup(False, ParcelStatus.IN_TRANSIT, [ParcelStatus.IN_TRANSIT]) is False
    assert is_pickup(None, ParcelStatus.DELIVERED) is False
