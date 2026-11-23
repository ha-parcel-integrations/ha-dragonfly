"""Pure parcel-mapping helpers for the Dragonfly integration (no I/O).

Keeping the carrier-specific mapping — the step map, the label rendering, the
history builder, the canonical :func:`normalize_parcel`, the sort contract and
the delivered filter — out of ``coordinator.py`` lets it be unit-tested as plain
functions and mirrors the layout every other carrier in the suite uses. The
coordinator only fetches, caches and fires events.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta, tzinfo
import logging
import re
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    COUNTRIES,
    DEFAULT_COUNTRY,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    HISTORY_MAX_EVENTS,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

# Dragonfly ``step`` → canonical ParcelStatus. The step drives the progress
# bar on the consumer site (1..4, left to right); a *negative* step is the
# site's exception state (red from that step onward), which we surface as
# ``problem``. The same value appears on the top-level ``last_status`` and on
# each ``status_list`` entry, so one map drives both.
_STEP_MAP: dict[int, ParcelStatus] = {
    1: ParcelStatus.REGISTERED,  # Zending aangemeld / ontvangen
    2: ParcelStatus.IN_TRANSIT,  # In het sorteercentrum / onderweg
    3: ParcelStatus.OUT_FOR_DELIVERY,  # Bij de bezorger
    4: ParcelStatus.DELIVERED,  # Afgeleverd
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
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
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
            return datetime.fromtimestamp(value / 1000, tz=UTC).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return str(value)


def status_label(
    status: dict | None, key: str = "shortLabel", *, country: str = DEFAULT_COUNTRY
) -> str | None:
    """Return a human-readable label from a Dragonfly status object.

    The API embeds its texts per language: ``labels[key][lang]`` on newer
    payloads, ``[key][lang]`` directly on older ones — exactly the fallback
    chain the consumer site uses. Which language leads follows the hub's
    country (``COUNTRIES[country]["label_languages"]``) — Dutch for NL,
    English-only for AU, English then French for CA. ``[link ...]`` markup is
    stripped to its inner text and ``{token}`` placeholders are filled from
    the status' ``package_location.address`` when available.
    """
    if not isinstance(status, dict):
        return None
    label_languages = COUNTRIES.get(country, COUNTRIES[DEFAULT_COUNTRY])[
        "label_languages"
    ]
    for source in (status.get("labels"), status):
        if not isinstance(source, dict):
            continue
        per_lang = source.get(key)
        if not isinstance(per_lang, dict):
            continue
        for lang in label_languages:
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
    status_list: list | None,
    *,
    max_events: int = HISTORY_MAX_EVENTS,
    country: str = DEFAULT_COUNTRY,
) -> list[dict]:
    """Build the canonical ``history`` list from Dragonfly's ``status_list``.

    Each entry is ``{timestamp, status, raw_status}`` — identical across all
    suite carriers. ``raw_status`` is the API's own short label, in the hub's
    country language. Sorted oldest → newest and capped to the most recent
    ``max_events``. Comes free with the tracking call (no extra request).
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
            "raw_status": status_label(status, country=country),
        }
        dt = _parse_iso(timestamp)
        if dt is None:
            unparseable.append(entry)
        else:
            parseable.append((dt, entry))
    parseable.sort(key=lambda item: item[0])
    ordered = [entry for _, entry in parseable] + unparseable
    return ordered[-max_events:]


def _tracking_url(
    tracking_code: str | None, *, country: str = DEFAULT_COUNTRY
) -> str | None:
    """Construct the consumer tracking deep-link for a parcel, for the hub's country."""
    if not tracking_code:
        return None
    template = COUNTRIES.get(country, COUNTRIES[DEFAULT_COUNTRY])["tracking_url"]
    return template.format(tracking_code=tracking_code)


def _moment(value: Any, zone: tzinfo) -> tuple[datetime, bool, str] | None:
    """Return ``(aware datetime, date_only, text)`` for a carrier time, or ``None``.

    A naive time or a bare date gets ``zone``; an aware time keeps its offset
    and, when it came in as a string, its own spelling.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        moment = datetime.combine(value, time.min, tzinfo=zone)
        return moment, True, moment.isoformat()
    elif isinstance(value, str) and value:
        try:
            if len(value) == 10:
                moment = datetime.combine(
                    date.fromisoformat(value), time.min, tzinfo=zone
                )
                return moment, True, moment.isoformat()
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo:
            return parsed, False, value
    else:
        return None
    moment = parsed if parsed.tzinfo else parsed.replace(tzinfo=zone)
    return moment, False, moment.isoformat()


def _end_of_day(moment: datetime, zone: tzinfo) -> datetime:
    local = moment.astimezone(zone)
    return datetime.combine(local.date(), time(23, 59, 59), tzinfo=zone)


def planned_window(
    start: Any, end: Any, *, zone: tzinfo
) -> tuple[str | None, str | None]:
    """Return ``(planned_from, planned_to)`` in the canonical window shape.

    ``start`` and ``end`` are ISO strings, dates or datetimes, or ``None``; an
    epoch number must be converted first (see ``to_iso_timestamp``). A window
    keeps both ends. One moment with a time ends at ``23:59:59`` the same local
    day; a date covers ``00:00:00`` to ``23:59:59``; two dates run from the
    first day's start to the last day's end. Equal values are one moment,
    reversed values are swapped, and ``planned_to`` is never set without
    ``planned_from``. ``zone`` is the carrier's own zone: it dates naive times
    and bare dates and decides which day a moment ends on, so a ``Z`` moment
    still ends on the carrier's local day. When nothing is left of that day
    after the moment, there is no ``planned_to``.
    """
    first = _moment(start, zone)
    last = _moment(end, zone)
    if first is None:
        first, last = last, None
    if first is None:
        return None, None

    def closing(moment: tuple[datetime, bool, str]) -> datetime:
        return _end_of_day(moment[0], zone) if moment[1] else moment[0]

    if last is not None and closing(last) < first[0]:
        first, last = last, first
    if last is not None and closing(last) == first[0]:
        last = None

    if last is None:
        end_of_day = _end_of_day(first[0], zone)
        return first[2], end_of_day.isoformat() if end_of_day > first[0] else None
    return first[2], closing(last).isoformat() if last[1] else last[2]


_NO_WINDOW_STATUSES = frozenset(
    {ParcelStatus.AT_PICKUP_POINT, ParcelStatus.RETURNING, ParcelStatus.DELIVERED}
)


def planned_for_status(
    status: ParcelStatus, planned_from: str | None, planned_to: str | None
) -> tuple[str | None, str | None]:
    """Empty the delivery window once the parcel is at its destination or done."""
    if status in _NO_WINDOW_STATUSES:
        return None, None
    return planned_from, planned_to


def is_pickup(
    route_flag: bool | None,
    status: ParcelStatus,
    event_statuses: Iterable[ParcelStatus | None] = (),
) -> bool:
    """Whether the delivery route ends at a pickup point.

    The carrier's own route flag wins; otherwise the parcel having reached
    ``AT_PICKUP_POINT`` at any point counts, so the value survives collection.
    """
    if route_flag:
        return True
    return status is ParcelStatus.AT_PICKUP_POINT or any(
        event is ParcelStatus.AT_PICKUP_POINT for event in event_statuses
    )


def normalize_parcel(
    raw: dict, *, include_history: bool = False, country: str = DEFAULT_COUNTRY
) -> dict:
    """Return a carrier-agnostic parcel dict with the original payload under ``raw``.

    The expected delivery window is ``public_eta.from``/``public_eta.to``,
    falling back to the top-level ``eta``/``buffered_eta`` pair — surfaced
    whenever the API has a value, regardless of ``last_status.showEta``
    (that flag only gates the *consumer site's* display, e.g. a day-level
    estimate while still in transit; HA users want it anyway). The window
    end prefers ``live_buffered_eta`` — the courier's GPS-driven revision —
    over the static ``buffered_eta`` when the API has one; the pair is
    ordered so ``planned_from`` never falls after ``planned_to``.

    ``history`` is the optional per-parcel status timeline — opt-in, default
    off (``None``), kept identical to the other suite carriers. Dragonfly
    returns the timeline in the same call, so enabling it costs no extra
    request.
    """
    last_status = raw.get("last_status") or {}
    step = last_status.get("step")
    status = map_parcel_status(4 if last_status.get("isDelivered") else step)
    delivered = status is ParcelStatus.DELIVERED

    # The delivery window: ``public_eta.from/to`` when the worker fills it,
    # otherwise the top-level ``eta`` / ``buffered_eta`` pair (verified live:
    # an out-for-delivery parcel carried ``public_eta: null`` but a concrete
    # ``eta``). On live payloads ``buffered_eta`` is consistently the exact
    # same instant as ``eta`` (a zero-width "buffer"), so left on its own it
    # never actually produces a window end — ``live_buffered_eta``, the
    # courier's GPS-driven revision of the estimate, is the field that does
    # (live-verified: it diverged from both ``eta`` and ``buffered_eta`` by
    # over an hour on a delivered parcel), so it takes priority when present.
    public_eta = raw.get("public_eta") or {}
    eta_from = _to_iso_timestamp(public_eta.get("from") or raw.get("eta"))
    eta_to = _to_iso_timestamp(
        raw.get("live_buffered_eta") or public_eta.get("to") or raw.get("buffered_eta")
    )
    from_dt = _parse_iso(eta_from)
    # The hub serves several time zones: a moment with its own offset ends its
    # own day, a bare UTC one ends the day in the user's Home Assistant zone.
    zone = dt_util.get_default_time_zone()
    if from_dt is not None and from_dt.utcoffset() and from_dt.tzinfo is not None:
        zone = from_dt.tzinfo
    planned_from, planned_to = planned_window(eta_from, eta_to, zone=zone)
    planned_from, planned_to = planned_for_status(status, planned_from, planned_to)

    tracking_code = raw.get("tracking_id")

    return {
        "carrier": "Dragonfly",
        "barcode": tracking_code,
        "sender": raw.get("client_code") or None,
        "receiver": None,
        "contents": None,
        "status": status,
        "raw_status": status_label(last_status, country=country),
        "delivered": delivered,
        "delivered_at": _to_iso_timestamp(last_status.get("timestamp"))
        if delivered
        else None,
        "planned_from": planned_from,
        "planned_to": planned_to,
        "pickup": False,
        "pickup_point": None,
        "url": _tracking_url(tracking_code, country=country),
        "weight": None,
        "dimensions": None,
        "history": (
            build_history(raw.get("status_list"), country=country)
            if include_history
            else None
        ),
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
    filter_type = options.get(CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE)
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(UTC) - timedelta(days=amount)
        return [
            p
            for p in parcels
            if (dt := _parse_iso(p.get("delivered_at"))) is None or dt >= cutoff
        ]
    return parcels[:amount]
