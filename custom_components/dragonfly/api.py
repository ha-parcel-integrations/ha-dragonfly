"""Dragonfly Shipping public tracking API client."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp

from .const import TRACKING_API_URL

_LOGGER = logging.getLogger(__name__)


class DragonflyApiError(Exception):
    """Raised when a Dragonfly API call returns an unexpected response."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"Dragonfly API request failed: {detail}")
        self.detail = detail


class DragonflyApiClient:
    """Client for the public Dragonfly Shipping tracking endpoint.

    No authentication: the endpoint is keyed on the tracking code alone,
    exactly like the Dragonfly consumer site (dragonflyshipping.nl, an
    Intelcom brand). The worker always answers HTTP 200 with a JSON
    envelope::

        {"success": true,  "data": {"result": {...}}}
        {"success": false, "data": {"status": 404, "code": "not_found", ...}}
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialise the client with an aiohttp session."""
        self._session = session

    async def async_get_parcel(self, tracking_code: str) -> dict[str, Any] | None:
        """Fetch one parcel's tracking details.

        Returns the ``result`` dict for a known parcel, or ``None`` when the
        endpoint reports the code as unknown (``data.code == "not_found"`` —
        also what a not-yet-scanned parcel gets). Any other failure envelope
        or non-2xx status raises :class:`DragonflyApiError`; network errors
        propagate as ``aiohttp.ClientError``.
        """
        url = TRACKING_API_URL.format(tracking_code=tracking_code)
        async with self._session.get(url) as response:
            if response.status != 200:
                raise DragonflyApiError(f"HTTP {response.status}")
            try:
                payload = await response.json(content_type=None)
            except ValueError as err:
                raise DragonflyApiError(f"unparseable body ({err})") from err

        if not isinstance(payload, dict):
            raise DragonflyApiError("unexpected body (not a JSON object)")

        data = payload.get("data")
        if payload.get("success"):
            result = (data or {}).get("result")
            if not isinstance(result, dict):
                # A success envelope must carry a result; treat a hollow one
                # as unknown rather than crashing the whole poll.
                _LOGGER.warning(
                    "Dragonfly returned success without a result for %s",
                    tracking_code,
                )
                return None
            return result

        if isinstance(data, dict) and data.get("code") == "not_found":
            return None
        detail = (data or {}).get("code") if isinstance(data, dict) else None
        raise DragonflyApiError(str(detail or "unknown error envelope"))
