"""Sample Dragonfly API payloads shared by the test modules.

The shape of a ``cfworker/v3/tracking`` result, trimmed to the fields the
integration reads. Keep them here rather than inline in each test module: when
the payload shape turns out to differ from what we assumed, there is then
exactly one place to fix.

Every helper returns a **fresh** dict, so a test may mutate what it gets back
without leaking into the next one.
"""
from __future__ import annotations

ACTIVE_CODE = "INTLCMB2C000999999"
DELIVERED_CODE = "INTLCMB2C000123456"


def status(step: int, ts: str, nl: str, en: str) -> dict:
    """One entry of Dragonfly's ``status_list`` timeline."""
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


def delivered_sample(code: str = DELIVERED_CODE) -> dict:
    """A representative Dragonfly tracking result for a delivered parcel."""
    last = status(4, "2026-04-29T13:12:42Z", "Afgeleverd", "Delivered")
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
            status(4, "2026-04-29T13:12:42Z", "Afgeleverd", "Delivered"),
            status(3, "2026-04-29T08:46:00Z", "Bij de bezorger", "Out for delivery"),
            status(2, "2026-04-28T15:52:17Z", "In het sorteercentrum", "At the sorting facility"),
            status(1, "2026-04-27T23:03:58Z", "Zending ontvangen", "Parcel received"),
        ],
    }


def active_sample(code: str = ACTIVE_CODE) -> dict:
    """An out-for-delivery parcel with an ETA window."""
    sample = delivered_sample(code)
    last = status(3, "2026-04-29T08:46:00Z", "Bij de bezorger", "Out for delivery")
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


def minimal_sample(code: str = DELIVERED_CODE) -> dict:
    """The smallest payload that still drives a full setup.

    Only the keys the coordinator actually dereferences, with a half-open ETA
    window and a single timeline entry — enough for the entity and event paths
    without committing those tests to the full response shape.
    """
    return {
        "tracking_id": code,
        "client_code": "ACME",
        "last_status": {
            "step": 3,
            "timestamp": "2026-04-29T08:46:00Z",
            "isDelivered": False,
            "showEta": True,
            "etaType": "time",
            "labels": {"shortLabel": {"nl": "Bij de bezorger"}},
        },
        "public_eta": {"from": "2026-05-01T10:00:00Z", "to": None},
        "status_list": [
            {
                "step": 1,
                "timestamp": "2026-04-28T10:00:00Z",
                "labels": {"shortLabel": {"nl": "Zending ontvangen"}},
            }
        ],
    }


def minimal_no_eta_sample(code: str = DELIVERED_CODE) -> dict:
    """As :func:`minimal_sample`, but with no ETA and an empty timeline."""
    sample = minimal_sample(code)
    sample["public_eta"] = {"from": None, "to": None}
    sample["status_list"] = []
    return sample
