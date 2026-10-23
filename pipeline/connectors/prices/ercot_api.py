"""The ERCOT Public API (api.ercot.com), used only when its three settings are present.

Owner, 2026-10-22: the API account is registered by a person ("Accounts registered by 'bots' or
other automated methods are not permitted", docs/29 §2.1); until `ERCOT_API_SUBSCRIPTION_KEY`,
`ERCOT_API_USERNAME` and `ERCOT_API_PASSWORD` are set, nothing here runs and the connector works
from the public MIS downloads and display pages (`ercot.py`). With them, the backfill uses the
API for a year whose annual workbook it may not download again (the three-downloads rule).

The token flow and the two endpoints are the ones gridstatus uses (`gridstatus/ercot_api/
ercot_api.py:35-38, 150-154, 272-309`, main 5c48466): an Azure B2C resource-owner password grant
returns an `id_token`, sent as a bearer token beside the subscription key. Responses are
`{"fields": [{"name": ...}], "data": [[...]], "_meta": {"totalPages": n, "currentPage": k}}`;
the rows go through the same vendored parser as the annual workbooks. Limits: "up to 30 requests
per minute" (docs/29 §2.1); the session's per-host rate for api.ercot.com is 0.4 a second.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any

import pandas as pd

from pipeline.connectors.base import ConnectorError, ParseError
from pipeline.connectors.http import PoliteSession
from pipeline.connectors.prices.base import FetchRequest
from pipeline.vendor.gridstatus.prices import (
    DAY_AHEAD_HOURLY,
    REAL_TIME_15_MIN,
    PriceLayoutError,
    ercot_finalize_spp,
    ercot_parse_doc,
)

TOKEN_URL = "https://ercotb2c.b2clogin.com/ercotb2c.onmicrosoft.com/B2C_1_PUBAPI-ROPC-FLOW/oauth2/v2.0/token"  # noqa: S105 - a URL
CLIENT_ID = "fec253ea-0d06-4272-a5e6-b478baeecd70"
PUBLIC_BASE_URL = "https://api.ercot.com/api/public-reports"
HOST = "api.ercot.com"
#: market -> (endpoint, the date fields it filters on)
ENDPOINTS: dict[str, tuple[str, str]] = {
    "rt": ("/np6-905-cd/spp_node_zone_hub", "deliveryDate"),
    "da": ("/np4-190-cd/dam_stlmnt_pnt_prices", "deliveryDate"),
}
PAGE_SIZE = 50_000


def get_token(http: PoliteSession, creds: Mapping[str, str]) -> str:
    """The `id_token` for the account in `creds` (never logged, never stored)."""
    payload = {
        "grant_type": "password",
        "username": creds["ERCOT_API_USERNAME"],
        "password": creds["ERCOT_API_PASSWORD"],
        "response_type": "id_token",
        "scope": f"openid {CLIENT_ID} offline_access",
        "client_id": CLIENT_ID,
    }
    response = http.post(TOKEN_URL, data=payload, timeout=60)
    try:
        token = response.json().get("id_token")
    except ValueError:
        token = None
    if not token:
        raise ConnectorError(f"ERCOT API token request answered HTTP {response.status_code} without a token")
    return str(token)


def auth_headers(token: str, creds: Mapping[str, str]) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Ocp-Apim-Subscription-Key": creds["ERCOT_API_SUBSCRIPTION_KEY"],
    }


def request_for(market: str, start: dt.date, end: dt.date, point: str, page: int = 1) -> FetchRequest:
    """One page of one settlement point's prices for the delivery dates `start`..`end`."""
    endpoint, field = ENDPOINTS[market]
    params: dict[str, Any] = {
        f"{field}From": start.isoformat(),
        f"{field}To": end.isoformat(),
        "settlementPoint": point,
        "size": PAGE_SIZE,
        "page": page,
    }
    return FetchRequest(
        key=f"api-{market}-{point}-{start:%Y%m%d}-{end:%Y%m%d}-p{page}",
        url=f"{PUBLIC_BASE_URL}{endpoint}",
        ext="json",
        params=params,
        meta={
            "kind": "api",
            "market": market,
            "point": point,
            "page": page,
            "start": start.isoformat(),
            "end": end.isoformat(),
        },
    )


def total_pages(body: Mapping[str, Any]) -> int:
    meta = body.get("_meta") or {}
    try:
        return int(meta.get("totalPages") or 1)
    except (TypeError, ValueError):
        return 1


def parse_response(body: Mapping[str, Any], market: str) -> pd.DataFrame:
    """An API page as the vendored parser's frame (`Time`, `Interval Start`, ..., `SPP`)."""
    try:
        columns = [f["name"] for f in body["fields"]]
        data = body["data"]
    except (KeyError, TypeError) as exc:
        raise ParseError(f"ERCOT API response without fields/data: {exc!r}") from exc
    # gridstatus/ercot_api/ercot_api.py:2453-2457: capitalise the first letter, strip whitespace.
    columns = [col[:1].upper() + col[1:] for col in columns]
    df = pd.DataFrame(data=data, columns=columns)
    df = df.apply(lambda x: x.str.strip() if x.dtype == "object" else x)
    if df.empty:
        return df
    try:
        df = ercot_parse_doc(df)
        return ercot_finalize_spp(df, market=REAL_TIME_15_MIN if market == "rt" else DAY_AHEAD_HOURLY)
    except (PriceLayoutError, KeyError) as exc:
        raise ParseError(f"ERCOT API rows unreadable: {exc!r}") from exc
