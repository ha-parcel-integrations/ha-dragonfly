"""Pure parcel-mapping helpers for the Dragonfly integration (no I/O).

Keeping the carrier-specific mapping — the step map, the label rendering, the
history builder, the canonical :func:`normalize_parcel`, the sort contract and
the delivered filter — out of ``coordinator.py`` lets it be unit-tested as plain
functions and mirrors the layout every other carrier in the suite uses. The
coordinator only fetches, caches and fires events.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    HISTORY_MAX_EVENTS,
    LABEL_LANGUAGES,
    TRACKING_URL,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

# Dragonfly ``step`` → canonical ParcelStatus. The step drives the progress
# bar on the consumer site (1..4, left to right); a *negative* step is the
# site's exception state (red from that step onward), which we surface as
# ``problem``. The same value appears on the top-level ``last_status`` and on
# each ``status_list`` entry, so one map drives both.
_STEP_MAP: dict[int, ParcelStatus] = {
    1: ParcelStatus.REGISTERED,        # Zending aangemeld / ontvangen
    2: ParcelStatus.IN_TRANSIT,        # In het sorteercentrum / onderweg
    3: ParcelStatus.OUT_FOR_DELIVERY,  # Bij de bezorger
    4: ParcelStatus.DELIVERED,         # Afgeleverd
}

# Points at the pre-filled issue template rather than a blank form, so a
# user following this link from their log lands somewhere that already
# asks the right questions.
_NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-dragonfly/issues/new"
    "?template=unrecognised_status.yml"
)

# ``[link <url>]text[/link]`` markup inside API labels — keep the inner text.
_LABEL_LINK_RE = re.compile(r"\[link .+?\](.+?)\[/link\]", re.IGNORECASE)
# ``{token}`` placeholders inside API labels, filled from the status'
# ``package_location.address`` when present.
_LABEL_TOKEN_RE = re.compile(r"\{([^{}]+)\}")

# Steps we have already warned about, so each unmapped one is logged only
# once per HA session.
_unmapped_steps_logged: set[int] = set()


def _warn_unmapped_step(step: int) -> None:
    """Log an unmapped Dragonfly step once, with a copy-paste issue link."""
    if step in _unmapped_steps_logged:
        return
    _unmapped_steps_logged.add(step)
    _LOGGER.warning(
        "Unrecognised Dragonfly step — help us map it. Open an issue and "
        "paste this line: %s\n  step=%s → reported as 'unknown'",
        _NEW_ISSUE_URL,
        step,
    )


def map_parcel_status(step: int | None) -> ParcelStatus:
    """Map a Dragonfly ``step`` to a canonical :class:`ParcelStatus`.

    ``None`` (a not-yet-scanned parcel) reports ``unknown`` silently; a
    negative step is the site's exception state and reports ``problem``; an
    unmapped non-negative step reports ``unknown`` with a one-shot warning.
    """
    if step is None:
        return ParcelStatus.UNKNOWN
    if step < 0:
        return ParcelStatus.PROBLEM
    mapped = _STEP_MAP.get(step)
    if mapped is not None:
        return mapped
    _warn_unmapped_step(step)
    return ParcelStatus.UNKNOWN


def map_event_status(step: int | None) -> ParcelStatus | None:
    """Map a history entry's ``step`` to a canonical status, or ``None``.

    Unmapped non-negative steps keep ``status: null`` on the history entry
    and warn once (reusing the parcel-step one-shot set).
    """
    if step is None:
        return None
    if step < 0:
        return ParcelStatus.PROBLEM
    mapped = _STEP_MAP.get(step)
    if mapped is not None:
        return mapped
    _warn_unmapped_step(step)
    return None


def _parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime, or ``None`` on failure.

    Naive values are treated as UTC so a list always sorts without crashing
    on a mixed set.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _to_iso_timestamp(value) -> str | None:
    """Return an ISO 8601 string for an API timestamp field.

    Dragonfly stamps statuses in **epoch milliseconds** (verified on a live
    parcel: ``last_status.timestamp = 1784203767167``) while the ETA fields
    are ISO strings — normalise both to the ISO strings the canonical parcel
    contract expects. Unparseable numbers become ``None``; strings pass
    through untouched (``_parse_iso`` guards their consumers).
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return str(value)


def status_label(status: dict | None, key: str = "shortLabel") -> str | None:
    """Return a human-readable label from a Dragonfly status object.

    The API embeds its texts per language: ``labels[key][lang]`` on newer
    payloads, ``[key][lang]`` directly on older ones — exactly the fallback
    chain the consumer site uses. Dutch is preferred (NL-only carrier),
    English is the fallback. ``[link ...]`` markup is stripped to its inner
    text and ``{token}`` placeholders are filled from the status'
    ``package_location.address`` when available.
    """
    if not isinstance(status, dict):
        return None
    for source in (status.get("labels"), status):
        if not isinstance(source, dict):
            continue
        per_lang = source.get(key)
        if not isinstance(per_lang, dict):
            continue
        for lang in LABEL_LANGUAGES:
            label = per_lang.get(lang)
            if label:
                return _format_label(str(label), status)
    return None


def _format_label(label: str, status: dict) -> str:
    """Strip link markup and fill address tokens in an API label."""
    label = _LABEL_LINK_RE.sub(r"\1", label)
    address = (status.get("package_location") or {}).get("address")
    if isinstance(address, dict):
        label = _LABEL_TOKEN_RE.sub(
            lambda match: str(address.get(match.group(1), match.group(0))), label
        )
    return label


def build_history(
    status_list: list | None, *, max_events: int = HISTORY_MAX_EVENTS
) -> list[dict]:
    """Build the canonical ``history`` list from Dragonfly's ``status_list``.

    Each entry is ``{timestamp, status, raw_status}`` — identical across all
    suite carriers. ``raw_status`` is the API's own (Dutch) short label.
    Sorted oldest → newest and capped to the most recent ``max_events``.
    Comes free with the tracking call (no extra request).
    """
    parseable: list[tuple[datetime, dict]] = []
    unparseable: list[dict] = []
    for status in status_list or []:
        if not isinstance(status, dict):
            continue
        timestamp = _to_iso_timestamp(status.get("timestamp"))
        if not timestamp:
            continue
        entry = {
            "timestamp": timestamp,
            "status": map_event_status(status.get("step")),
            "raw_status": status_label(status),
        }
        dt = _parse_iso(timestamp)
        if dt is None:
            unparseable.append(entry)
        else:
            parseable.append((dt, entry))
    parseable.sort(key=lambda item: item[0])
    ordered = [entry for _, entry in parseable] + unparseable
    return ordered[-max_events:]


def _tracking_url(tracking_code: str | None) -> str | None:
    """Construct the consumer tracking deep-link for a parcel."""
    if not tracking_code:
        return None
    return TRACKING_URL.format(tracking_code=tracking_code)


def normalize_parcel(raw: dict, *, include_history: bool = False) -> dict:
    """Return a carrier-agnostic parcel dict with the original payload under ``raw``.

    The expected delivery window is ``public_eta.from``/``public_eta.to``
    (only surfaced while the parcel is on its way and the site itself would
    show an ETA: ``last_status.showEta`` true and ``etaType`` not ``none``).

    ``history`` is the optional per-parcel status timeline — opt-in, default
    off (``None``), kept identical to the other suite carriers. Dragonfly
    returns the timeline in the same call, so enabling it costs no extra
    request.
    """
    last_status = raw.get("last_status") or {}
    step = last_status.get("step")
    delivered = bool(last_status.get("isDelivered")) or step == 4

    # The delivery window: ``public_eta.from/to`` when the worker fills it,
    # otherwise the top-level ``eta`` / ``buffered_eta`` pair (verified live:
    # an out-for-delivery parcel carried ``public_eta: null`` but a concrete
    # ``eta``). A ``buffered_eta`` equal to the ETA is a point estimate, not
    # a window — collapse it to ``planned_to: None``.
    public_eta = raw.get("public_eta") or {}
    show_eta = bool(last_status.get("showEta")) and last_status.get("etaType") != "none"
    eta_from = _to_iso_timestamp(public_eta.get("from") or raw.get("eta"))
    eta_to = _to_iso_timestamp(public_eta.get("to") or raw.get("buffered_eta"))
    if eta_from and eta_to and _parse_iso(eta_to) == _parse_iso(eta_from):
        eta_to = None
    if not show_eta:
        eta_from = eta_to = None

    # ``last_mile_pickup`` is a driver-comes-to-you task (e.g. a return
    # pickup), not a pickup-point delivery — Dragonfly delivers to the door
    # and has no parcel-shop network, so ``at_pickup_point`` never occurs.
    is_pickup = last_status.get("task_type") == "last_mile_pickup"

    tracking_code = raw.get("tracking_id")

    return {
        "carrier": "Dragonfly",
        "barcode": tracking_code,
        "sender": raw.get("client_code") or None,
        "receiver": None,
        "status": map_parcel_status(step),
        "raw_status": status_label(last_status),
        "delivered": delivered,
        "delivered_at": _to_iso_timestamp(last_status.get("timestamp")) if delivered else None,
        "planned_from": None if delivered else eta_from,
        "planned_to": None if delivered else eta_to,
        "pickup": is_pickup,
        "pickup_point": None,
        "url": _tracking_url(tracking_code),
        "weight": None,
        "dimensions": None,
        "history": build_history(raw.get("status_list")) if include_history else None,
        "raw": raw,
    }


def sort_parcels_by_ts(
    parcels: list[dict], key_field: str, *, descending: bool = False
) -> list[dict]:
    """Return normalized parcels sorted by the ISO timestamp at ``key_field``.

    Parcels whose value is missing or unparseable always sort to the end,
    regardless of ``descending``.
    """
    with_ts: list[tuple[datetime, dict]] = []
    without_ts: list[dict] = []
    for parcel in parcels:
        dt = _parse_iso(parcel.get(key_field))
        if dt is None:
            without_ts.append(parcel)
        else:
            with_ts.append((dt, parcel))
    with_ts.sort(key=lambda item: item[0], reverse=descending)
    return [p for _, p in with_ts] + without_ts


def apply_delivered_filter(parcels: list[dict], entry: ConfigEntry) -> list[dict]:
    """Trim the delivered list per the entry's retention option.

    ``parcels`` is already sorted newest-first. ``days`` keeps deliveries from
    the last N days (an unparseable ``delivered_at`` is kept); the ``parcels``
    type keeps the N most recent. The parcels stay *tracked* either way — this
    only controls what the delivered sensor shows.
    """
    options = entry.options
    filter_type = options.get(
        CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
    )
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(timezone.utc) - timedelta(days=amount)
        return [
            p
            for p in parcels
            if (dt := _parse_iso(p.get("delivered_at"))) is None or dt >= cutoff
        ]
    return parcels[:amount]
