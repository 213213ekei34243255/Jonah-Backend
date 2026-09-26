"""eBay product search (eBay Browse API) for Jonah's shopping panels.

eBay needs an OAuth "application access token". It is obtained with EBAY_CLIENT_ID + EBAY_CLIENT_SECRET (client-credentials grant),
kept until a minute before it expires (eBay: 2 hours), and renewed once when eBay rejects it. Clients never see any of it.
eBay's JSON is returned unchanged (itemSummaries[] with title, price, image, itemWebUrl, condition, seller, ...).

eBay keeps a new production keyset switched off until the developer deals with its "Marketplace account deletion" requirement (the
developer portal offers an exemption for apps that store no eBay user data, like this one). The default allowance is about 5,000
Browse API calls a day.
"""

from __future__ import annotations

import asyncio
import base64
import time
from typing import Any, ClassVar

import httpx

from app.config import Settings, secret_value
from app.providers.base import ProviderAuthError, ProviderBadResponse, ProviderError
from app.upstream import request_json

EBAY_HOSTS = {"production": "https://api.ebay.com", "sandbox": "https://api.sandbox.ebay.com"}
EBAY_SCOPE = "https://api.ebay.com/oauth/api_scope"
MARKETPLACES = frozenset({
    "EBAY_US", "EBAY_GB", "EBAY_DE", "EBAY_AU", "EBAY_CA", "EBAY_FR", "EBAY_IT", "EBAY_ES",
    "EBAY_AT", "EBAY_BE", "EBAY_CH", "EBAY_IE", "EBAY_NL", "EBAY_PL", "EBAY_HK", "EBAY_SG", "EBAY_MY", "EBAY_PH",
})  # fmt: skip
SORTS = frozenset({"price", "-price", "newlyListed", "endingSoonest"})


def _sign_in_failure(error: ProviderError) -> ProviderError:
    error.during_sign_in = True  # type: ignore[attr-defined]  (lets the route say "eBay sign-in failed")
    return error


class EbayClient:
    name: ClassVar[str] = "ebay"

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    def is_configured(self) -> bool:
        return bool(self.settings.ebay_client_id.strip()) and bool(secret_value(self.settings.ebay_client_secret))

    def timeout_budget(self) -> float:
        return self.settings.request_timeout_seconds * 2  # a token request, then the search

    @property
    def base(self) -> str:
        return EBAY_HOSTS.get(self.settings.ebay_environment.strip().lower(), EBAY_HOSTS["production"])

    async def _access_token(self, force_new: bool = False) -> str:
        async with self._lock:  # one token request at a time, however many searches arrive together
            if not force_new and self._token and self._expires_at - 60 > time.time():
                return self._token
            pair = f"{self.settings.ebay_client_id.strip()}:{secret_value(self.settings.ebay_client_secret)}"
            headers = {"Content-Type": "application/x-www-form-urlencoded", "Authorization": "Basic " + base64.b64encode(pair.encode()).decode()}
            try:
                data = await request_json(self.client, "POST", f"{self.base}/identity/v1/oauth2/token", headers=headers,
                                          data={"grant_type": "client_credentials", "scope": EBAY_SCOPE})  # fmt: skip
            except ProviderError as err:
                raise _sign_in_failure(err) from None
            token = data.get("access_token") if isinstance(data, dict) else None
            if not isinstance(token, str) or not token:
                raise _sign_in_failure(ProviderBadResponse("token reply was not usable", upstream_status=200))
            try:
                lifetime = int(data.get("expires_in") or 7200)
            except (TypeError, ValueError):
                lifetime = 7200
            self._token, self._expires_at = token, time.time() + lifetime
            return token

    async def get(self, path: str, params: dict[str, Any] | None = None, marketplace: str | None = None) -> Any:
        """GET a Browse API path with the app token; a token eBay no longer accepts (401) is renewed once. Raises ProviderError."""
        for attempt in (1, 2):
            token = await self._access_token(force_new=attempt == 2)
            headers = {
                "Authorization": f"Bearer {token}",
                "X-EBAY-C-MARKETPLACE-ID": marketplace or self.settings.ebay_marketplace_id.strip().upper() or "EBAY_US",
                "Accept": "application/json",
            }
            if self.settings.ebay_affiliate_campaign_id.strip():
                headers["X-EBAY-C-ENDUSERCTX"] = f"affiliateCampaignId={self.settings.ebay_affiliate_campaign_id.strip()}"
            try:
                return await request_json(self.client, "GET", f"{self.base}{path}", params=params, headers=headers)
            except ProviderAuthError as err:
                if err.upstream_status == 401 and attempt == 1:
                    continue
                raise
        raise ProviderAuthError("eBay rejected a fresh token", upstream_status=401)  # not reached: the 2nd attempt returns or raises
