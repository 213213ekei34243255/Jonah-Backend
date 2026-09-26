"""eBay product search, with eBay's JSON returned unchanged.

  GET /shopping/ebay?q=...[&limit=20][&offset=0][&sort=price|-price|newlyListed|endingSoonest][&min_price=][&max_price=]
                         [&condition=new|used][&buying=fixed_price|auction][&category_ids=15724][&marketplace=EBAY_GB]
      Browse API item_summary/search: itemSummaries[] (title, price, image, itemWebUrl, condition, seller, ...), total, limit, offset.
  GET /shopping/ebay/item/{item_id}
      Browse API getItem: one item's full details. item_id is a search result's itemId, e.g. v1|123456789|0.

Jonah Browser's home page (Fashion / Toys panels) calls the first through Jonah's local relay. Failures use the original proxy's
format: HTTP 503 {"error": {"message": "eBay search request failed: upstream HTTP 429 - ...", "upstream_status": 429}}.
"""

from __future__ import annotations

import math
import re
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from app.api.deps import guard
from app.api.routes_compat import bad_request
from app.ebay import MARKETPLACES, SORTS
from app.providers.base import ProviderError
from app.upstream import upstream_error_response

router = APIRouter(tags=["shopping"])

NOT_CONFIGURED = "eBay is not configured on this server (EBAY_CLIENT_ID and EBAY_CLIENT_SECRET)"
_Str = Annotated[str | None, Query()]


def _price(value: str, name: str) -> str:
    try:
        number = float(value)
    except ValueError:
        raise ValueError(f"'{name}' must be a number") from None
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"'{name}' must be a positive number")
    return f"{number:g}"


def _marketplace(value: str | None) -> str | None:
    chosen = (value or "").strip().upper() or None
    if chosen and chosen not in MARKETPLACES:
        raise ValueError(f"Unknown 'marketplace' {chosen!r}")
    return chosen


async def _call(request: Request, what: str, path: str, params: dict | None, marketplace: str | None) -> JSONResponse:
    state = request.app.state
    settings = state.settings
    if not state.ebay.is_configured():
        return upstream_error_response(what, None, settings, reason=NOT_CONFIGURED)
    cooling = state.manager.cooling_down_error(state.ebay.name)
    if cooling is not None:
        return upstream_error_response(what, cooling, settings)
    try:
        data = await state.manager.run_direct(state.ebay, lambda: state.ebay.get(path, params=params, marketplace=marketplace))
    except ProviderError as exc:
        return upstream_error_response(f"{what} (eBay sign-in)" if getattr(exc, "during_sign_in", False) else what, exc, settings)
    return JSONResponse(data)


@router.get("/shopping/ebay", summary="eBay product search (eBay's JSON, unchanged)")
async def ebay_search(
    request: Request,
    q: Annotated[str | None, Query(description="What to look for (required).", examples=["iphone 15"])] = None,
    limit: _Str = None,
    offset: _Str = None,
    sort: Annotated[str | None, Query(description="price, -price, newlyListed or endingSoonest (default: best match).")] = None,
    min_price: _Str = None,
    max_price: _Str = None,
    condition: Annotated[str | None, Query(description="new or used.")] = None,
    buying: Annotated[str | None, Query(description="fixed_price or auction.")] = None,
    category_ids: Annotated[str | None, Query(description="One eBay category number, e.g. 15724.")] = None,
    marketplace: Annotated[str | None, Query(description="EBAY_US (default), EBAY_GB, EBAY_DE, ...")] = None,
    _client: str = Depends(guard),
):
    query = " ".join((q or "").split())
    if not query:
        return bad_request("Missing required query parameter 'q'")
    if len(query) > 350:
        return bad_request("Query parameter 'q' is too long (eBay allows 350 characters)")
    try:
        params: dict = {"q": query, "limit": min(max(int(limit or 20), 1), 200), "offset": min(max(int(offset or 0), 0), 9999)}
    except ValueError:
        return bad_request("'limit' and 'offset' must be whole numbers")
    try:
        chosen_marketplace = _marketplace(marketplace)
        if sort:
            if sort.strip() not in SORTS:
                raise ValueError(f"'sort' must be one of: {', '.join(sorted(SORTS))}")
            params["sort"] = sort.strip()
        if category_ids and category_ids.strip():
            if not re.fullmatch(r"\d{1,10}", category_ids.strip()):
                raise ValueError("'category_ids' must be one eBay category number, e.g. 15724")
            params["category_ids"] = category_ids.strip()
        filters = []
        low = _price(min_price, "min_price") if min_price else ""
        high = _price(max_price, "max_price") if max_price else ""
        if low or high:
            filters.append(f"price:[{low}..{high}]")
        if condition and condition.strip():
            if condition.strip().lower() not in ("new", "used"):
                raise ValueError("'condition' must be 'new' or 'used'")
            filters.append(f"conditions:{{{condition.strip().upper()}}}")
        if buying and buying.strip():
            if buying.strip().lower() not in ("fixed_price", "auction"):
                raise ValueError("'buying' must be 'fixed_price' or 'auction'")
            filters.append(f"buyingOptions:{{{buying.strip().upper()}}}")
        if filters:
            params["filter"] = ",".join(filters)
    except ValueError as exc:
        return bad_request(str(exc))
    return await _call(request, "eBay search request", "/buy/browse/v1/item_summary/search", params, chosen_marketplace)


@router.get("/shopping/ebay/item/{item_id:path}", summary="One eBay item's details (eBay's JSON, unchanged)")
async def ebay_item(request: Request, item_id: str, marketplace: _Str = None, _client: str = Depends(guard)):
    if not re.fullmatch(r"v1\|\d{1,20}\|\d{1,20}", item_id):
        return bad_request("item_id must look like v1|123456789|0 (the itemId from a search result)")
    try:
        chosen_marketplace = _marketplace(marketplace)
    except ValueError as exc:
        return bad_request(str(exc))
    return await _call(request, "eBay item request", f"/buy/browse/v1/item/{item_id.replace('|', '%7C')}", None, chosen_marketplace)
