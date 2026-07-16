"""Coordinator for the Dragonfly Shipping parcel tracker integration."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import DragonflyApiClient, DragonflyApiError
from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    CONF_REFRESH_INTERVAL,
    CONF_TRACKING_CODE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    DEFAULT_INCLUDE_HISTORY,
    DEFAULT_REFRESH_INTERVAL,
    DOMAIN,
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

_NEW_ISSUE_URL = "https://github.com/ha-parcel-integrations/ha-dragonfly/issues/new"

# ``[link <url>]text[/link]`` markup inside API labels — keep the inner text.
_LABEL_LINK_RE = re.compile(r"\[link .+?\](.+?)\[/link\]", re.IGNORECASE)
# ``{token}`` placeholders inside API labels, filled from the status'
# ``package_location.address`` when present.
_LABEL_TOKEN_RE = re.compile(r"\{([^{}]+)\}")

# Steps we have already warned about, so each unmapped one is logged only
# once per HA session.
_unmapped_steps_logged: set[int] = set()


def _refresh_interval(entry: ConfigEntry) -> timedelta:
    """Return the configured refresh interval as a ``timedelta``."""
    minutes = int(entry.options.get(CONF_REFRESH_INTERVAL, DEFAULT_REFRESH_INTERVAL))
    return timedelta(minutes=minutes)


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


class DragonflyCoordinator(DataUpdateCoordinator[list[dict]]):
    """Coordinator that polls each tracked Dragonfly parcel on a fixed schedule.

    Dragonfly has no account/feed, so the tracked parcels are the tracking
    codes the user entered (stored in the entry options). Each is fetched
    individually and merged into one list; ``coordinator.data`` is the active
    (not-yet-delivered) parcels, ``self.delivered`` the rest.
    """

    def __init__(
        self, hass: HomeAssistant, client: DragonflyApiClient, entry: ConfigEntry
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=_refresh_interval(entry),
        )
        self._client = client
        self.delivered: list[dict] = []
        # tracking_code -> last successful raw payload, so a transient fetch
        # failure or a not-found blip keeps the parcel visible instead of
        # dropping its sensor. Lives for the integration's lifetime (resets
        # on restart).
        self._raw_cache: dict[str, dict] = {}
        # barcode -> last seen ParcelStatus / (planned_from, planned_to).
        # ``None`` on the first refresh so events are suppressed for parcels
        # that already existed when the integration started.
        self._known_state: dict[str, ParcelStatus] | None = None
        self._known_delivery_times: (
            dict[str, tuple[str | None, str | None]] | None
        ) = None
        # Cached device id, attached to every fired event so device-trigger
        # automations can filter to this Dragonfly device.
        self._cached_device_id: str | None = None
        # Timestamp of the last successful poll (diagnostic sensor).
        self.last_success_time: datetime | None = None

    def _device_id(self) -> str | None:
        """Resolve (and cache) this entry's device id for event payloads."""
        if self._cached_device_id is not None:
            return self._cached_device_id
        registry = dr.async_get(self.hass)
        device = next(
            iter(dr.async_entries_for_config_entry(registry, self.config_entry.entry_id)),
            None,
        )
        if device is not None:
            self._cached_device_id = device.id
        return self._cached_device_id

    def _tracked(self) -> list[str]:
        """Return the configured tracking codes."""
        return [
            item[CONF_TRACKING_CODE]
            for item in self.config_entry.options.get(CONF_PARCELS, [])
            if item.get(CONF_TRACKING_CODE)
        ]

    @property
    def _include_history(self) -> bool:
        """Whether the opt-in per-parcel history option is enabled."""
        return bool(
            self.config_entry.options.get(
                CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY
            )
        )

    def _apply_delivered_filter(self, parcels: list[dict]) -> list[dict]:
        """Trim the delivered list per the configured retention option.

        ``parcels`` is already sorted newest-first. ``days`` keeps deliveries
        from the last N days (an unparseable ``delivered_at`` is kept); the
        ``parcels`` type keeps the N most recent. The parcels stay *tracked*
        either way — this only controls what the delivered sensor shows.
        """
        options = self.config_entry.options
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

    async def _async_update_data(self) -> list[dict]:
        codes = self._tracked()

        # Drop cache entries for parcels that were untracked, so the cache
        # stays bounded to what the user still follows.
        tracked_codes = set(codes)
        self._raw_cache = {
            k: v for k, v in self._raw_cache.items() if k in tracked_codes
        }

        results = await asyncio.gather(
            *(self._client.async_get_parcel(code) for code in codes),
            return_exceptions=True,
        )

        raws: list[dict] = []
        errors = 0
        for code, result in zip(codes, results):
            if isinstance(result, BaseException):
                if not isinstance(result, (DragonflyApiError, aiohttp.ClientError)):
                    raise result
                errors += 1
                _LOGGER.warning("Dragonfly fetch failed for %s: %s", code, result)
                cached = self._raw_cache.get(code)
                if cached is not None:
                    raws.append(cached)
                continue

            if result is None:
                # not_found — unknown or not yet scanned. Keep prior data if
                # we have it, otherwise show a pending placeholder so the
                # user still sees the tracked parcel.
                raws.append(
                    self._raw_cache.get(code)
                    or {"tracking_id": code, "last_status": None}
                )
                continue

            # The response's own tracking_id can be missing on edge payloads;
            # fall back to the code we asked for so the sensor keeps its key.
            result.setdefault("tracking_id", code)
            self._raw_cache[code] = result
            raws.append(result)

        if codes and errors == len(codes) and not raws:
            raise UpdateFailed("Dragonfly unreachable for all tracked parcels")

        include_history = self._include_history
        normalized = [
            normalize_parcel(raw, include_history=include_history) for raw in raws
        ]
        active = [p for p in normalized if not p["delivered"]]
        delivered = [p for p in normalized if p["delivered"]]

        self.delivered = self._apply_delivered_filter(
            sort_parcels_by_ts(delivered, "delivered_at", descending=True)
        )
        normalized_active = sort_parcels_by_ts(active, "planned_from")

        # Incoming = active + delivered, combined so the transition to
        # delivered is visible in one set (mirrors the other suite carriers).
        incoming = normalized_active + self.delivered
        self._fire_change_events(incoming)
        self._known_state = {
            p["barcode"]: p["status"] for p in incoming if p.get("barcode")
        }
        self._known_delivery_times = {
            p["barcode"]: (p.get("planned_from"), p.get("planned_to"))
            for p in incoming
            if p.get("barcode")
        }

        # Only stamp the diagnostic timestamp when at least one fetch actually
        # succeeded (or nothing is tracked) — a poll that was served entirely
        # from cache must not present itself as a successful update.
        if not codes or errors < len(codes):
            self.last_success_time = datetime.now(timezone.utc)
        return normalized_active

    def _fire_change_events(self, parcels: list[dict]) -> None:
        """Fire registered / status-changed / delivered / delivery-time events.

        Silent on the very first refresh — we cannot know which parcels are
        genuinely new vs. already present before HA started. Mirrors the other
        suite carriers, including the ``device_id`` on every payload and the
        ``value → null`` ETA transitions staying intentionally silent. The
        parcels span active + delivered, so the terminal hop is visible: a
        change **to** ``DELIVERED`` fires only ``dragonfly_parcel_delivered``
        (never also ``_status_changed``), a barcode first seen
        already-delivered fires nothing, and ``registered`` only fires for
        not-yet-delivered new barcodes.
        """
        if self._known_state is None:
            return

        known_times = self._known_delivery_times or {}
        device_id = self._device_id()

        for parcel in parcels:
            barcode = parcel.get("barcode")
            if not barcode:
                continue
            new_status = parcel["status"]
            if barcode not in self._known_state:
                if new_status != ParcelStatus.DELIVERED:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_parcel_registered",
                        {**parcel, "device_id": device_id},
                    )
                continue

            if self._known_state[barcode] != new_status:
                if new_status == ParcelStatus.DELIVERED:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_parcel_delivered",
                        {**parcel, "device_id": device_id},
                    )
                else:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_parcel_status_changed",
                        {
                            **parcel,
                            "device_id": device_id,
                            "old_status": self._known_state[barcode],
                            "new_status": new_status,
                        },
                    )

            old_from, old_to = known_times.get(barcode, (None, None))
            new_from = parcel.get("planned_from")
            new_to = parcel.get("planned_to")
            from_changed = new_from is not None and new_from != old_from
            to_changed = new_to is not None and new_to != old_to
            if from_changed or to_changed:
                self.hass.bus.async_fire(
                    f"{DOMAIN}_parcel_delivery_time_changed",
                    {
                        **parcel,
                        "device_id": device_id,
                        "old_planned_from": old_from,
                        "new_planned_from": new_from,
                        "old_planned_to": old_to,
                        "new_planned_to": new_to,
                    },
                )
