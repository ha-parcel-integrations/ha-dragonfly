"""Tests for the Dragonfly API client."""
import json
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.dragonfly.api import DragonflyApiClient, DragonflyApiError


def _session_returning(status: int, body: object = None) -> MagicMock:
    response = AsyncMock()
    response.status = status
    if isinstance(body, str):
        response.json = AsyncMock(side_effect=json.JSONDecodeError("x", body, 0))
    else:
        response.json = AsyncMock(return_value=body)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=ctx)
    return session


def _success_envelope(result: dict) -> dict:
    return {"success": True, "data": {"result": result}}


def _not_found_envelope() -> dict:
    return {
        "success": False,
        "data": {"status": 404, "statusText": "Not Found", "result": None, "code": "not_found"},
    }


async def test_get_parcel_returns_result_on_success():
    session = _session_returning(
        200, _success_envelope({"tracking_id": "INTLCM123", "last_status": {"step": 4}})
    )
    client = DragonflyApiClient(session)
    parcel = await client.async_get_parcel("INTLCM123")
    assert parcel["tracking_id"] == "INTLCM123"
    # the tracking code ends up in the URL
    url = session.get.call_args[0][0]
    assert "cfworker/v3/tracking/INTLCM123/" in url


async def test_get_parcel_returns_none_on_not_found():
    client = DragonflyApiClient(_session_returning(200, _not_found_envelope()))
    assert await client.async_get_parcel("UNKNOWN123") is None


async def test_get_parcel_returns_none_on_hollow_success():
    """A success envelope without a result dict is treated as unknown."""
    client = DragonflyApiClient(
        _session_returning(200, {"success": True, "data": {"result": None}})
    )
    assert await client.async_get_parcel("INTLCM123") is None


async def test_get_parcel_raises_on_error_status():
    client = DragonflyApiClient(_session_returning(500, {}))
    with pytest.raises(DragonflyApiError):
        await client.async_get_parcel("INTLCM123")


async def test_get_parcel_raises_on_unparseable_body():
    client = DragonflyApiClient(_session_returning(200, "not json"))
    with pytest.raises(DragonflyApiError):
        await client.async_get_parcel("INTLCM123")


async def test_get_parcel_raises_on_non_object_body():
    client = DragonflyApiClient(_session_returning(200, ["not", "a", "dict"]))
    with pytest.raises(DragonflyApiError):
        await client.async_get_parcel("INTLCM123")


async def test_get_parcel_raises_on_unknown_error_envelope():
    client = DragonflyApiClient(
        _session_returning(200, {"success": False, "data": {"code": "rate_limited"}})
    )
    with pytest.raises(DragonflyApiError) as err:
        await client.async_get_parcel("INTLCM123")
    assert "rate_limited" in str(err.value)


async def test_get_parcel_raises_on_error_envelope_without_data():
    client = DragonflyApiClient(_session_returning(200, {"success": False}))
    with pytest.raises(DragonflyApiError):
        await client.async_get_parcel("INTLCM123")


async def test_get_parcel_propagates_network_error():
    session = MagicMock()
    session.get = MagicMock(side_effect=aiohttp.ClientError("boom"))
    client = DragonflyApiClient(session)
    with pytest.raises(aiohttp.ClientError):
        await client.async_get_parcel("INTLCM123")
