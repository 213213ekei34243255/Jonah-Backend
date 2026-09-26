"""Configuration. Every setting comes from an environment variable (or a local .env file); nothing is hard-coded.

Only providers whose required settings are present are enabled, so the service starts with any subset of them.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


def secret_value(value: SecretStr | None) -> str:
    """The plain text of an optional secret ('' when unset). Only call this where the value is actually used."""
    return value.get_secret_value().strip() if value else ""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False)

    # ---- general
    app_name: str = "Jonah Search"
    environment: str = "development"  # "production" turns on HSTS and quieter defaults
    log_level: str = "INFO"
    user_agent: str = "JonahSearchBot/1.0 (+https://github.com/your-account/jonah-search)"
    cors_origins: str = ""  # comma-separated origins; empty = no CORS headers at all

    # ---- authentication and rate limiting
    search_api_key: SecretStr | None = None  # one key, or several separated by commas; empty = no authentication
    # The shared secret existing Jonah apps already send as X-Jonah-Key. Accepted exactly like a SEARCH_API_KEY key.
    app_shared_secret: SecretStr | None = None
    rate_limit_per_minute: int = Field(60, ge=0)  # per API key (or per client IP when there is no key); 0 disables
    trust_proxy_headers: bool = False  # honour X-Forwarded-For (set true behind Render / a load balancer)
    trusted_proxy_hops: int = Field(1, ge=1, le=5)  # how many proxies you control append to X-Forwarded-For (Render: 1)

    # ---- providers
    provider_priority: str = "searxng,brave,bing,google,wikipedia"
    searxng_url: str = ""  # one instance, or several separated by commas (tried in order)
    brave_api_key: SecretStr | None = None
    bing_api_key: SecretStr | None = None
    bing_endpoint: str = "https://api.bing.microsoft.com/v7.0/search"
    google_api_key: SecretStr | None = None
    google_cx: str = Field("", validation_alias=AliasChoices("google_cx", "google_cse_id"))  # GOOGLE_CX or GOOGLE_CSE_ID
    enable_wikipedia: bool = True  # keyless, documented API: a last-resort provider so the service works with zero setup
    deep_mode_max_providers: int = Field(4, ge=1, le=10)

    # ---- news (NewsAPI) and image source finding (Google Cloud Vision)
    news_api_key: SecretStr | None = None  # https://newsapi.org
    news_default_country: str = "in"  # /news/headlines without ?country=
    news_cache_ttl_seconds: int = Field(300, ge=0)  # NewsAPI's free plan allows 100 requests a day: repeat requests are served cached
    google_vision_api_key: SecretStr | None = None  # empty = use GOOGLE_API_KEY (enable the Cloud Vision API on that key's project)
    max_image_mb: float = Field(4.0, gt=0, le=10)  # images sent to Cloud Vision (uploaded, or downloaded by this server)

    # ---- shopping (eBay Browse API)
    ebay_client_id: str = ""  # "App ID (Client ID)" of your eBay developer keyset
    ebay_client_secret: SecretStr | None = None  # "Cert ID (Client Secret)" of the same keyset
    ebay_environment: str = "production"  # or "sandbox": must match the keyset (sandbox keys start with SBX-)
    ebay_marketplace_id: str = "EBAY_US"  # default marketplace (eBay has no Indian site)
    ebay_affiliate_campaign_id: str = ""  # eBay Partner Network campaign id: results also carry itemAffiliateWebUrl
    max_request_body_mb: float = Field(8.0, gt=0, le=50)  # largest request body accepted (image uploads are the big ones)

    # ---- fetching and extraction (bounded, so a typo fails at startup instead of misbehaving later)
    request_timeout_seconds: float = Field(10.0, gt=0, le=120)  # per provider call and per page fetch
    fetch_total_timeout_seconds: float = Field(20.0, gt=0, le=300)  # ceiling for fetching ALL pages of one request
    max_page_size_mb: float = Field(5.0, gt=0, le=100)
    max_redirects: int = Field(5, ge=0, le=20)
    max_results: int = Field(20, ge=1, le=100)
    max_content_length: int = Field(50000, ge=100, le=2_000_000)  # characters of extracted text kept per page
    max_extract_html_kb: int = Field(400, ge=16, le=20_000)  # HTML processed per page (cost grows with size; the rest is ignored)
    max_concurrent_fetches: int = Field(5, ge=1, le=50)
    allowed_ports: str = "80,443"  # outbound ports the page fetcher may connect to
    respect_robots: bool = True
    robots_fail_open: bool = False  # if robots.txt cannot be read (5xx / network): False = do not fetch

    # ---- cache
    cache_ttl_seconds: int = Field(300, ge=0)  # search results; 0 disables result caching
    page_cache_ttl_seconds: int = Field(900, ge=0)  # extracted pages; 0 disables page caching
    cache_max_entries: int = Field(1000, ge=1)  # in-memory cache only
    redis_url: str = ""  # empty = in-memory cache and rate limiter

    # ------------------------------------------------------------------ derived values

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() == "production"

    @property
    def api_keys(self) -> list[str]:
        keys = [k.strip() for k in secret_value(self.search_api_key).split(",") if k.strip()]
        shared = secret_value(self.app_shared_secret)
        return keys + ([shared] if shared and shared not in keys else [])

    @property
    def vision_api_key(self) -> str:
        return secret_value(self.google_vision_api_key) or secret_value(self.google_api_key)

    @property
    def priority_list(self) -> list[str]:
        return [p.strip().lower() for p in self.provider_priority.split(",") if p.strip()]

    @property
    def searxng_urls(self) -> list[str]:
        return [u.strip().rstrip("/") for u in self.searxng_url.split(",") if u.strip()]

    @property
    def max_page_size_bytes(self) -> int:
        return int(self.max_page_size_mb * 1024 * 1024)

    @property
    def allowed_port_set(self) -> frozenset[int]:
        ports = set()
        for part in self.allowed_ports.split(","):
            part = part.strip()
            if part.isdigit():
                ports.add(int(part))
        return frozenset(ports or {80, 443})

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def secrets_to_redact(self) -> list[str]:
        """Every configured secret value, so the logger can mask them if one ever reaches a log line."""
        values = [
            secret_value(self.brave_api_key), secret_value(self.bing_api_key), secret_value(self.google_api_key),
            secret_value(self.news_api_key), secret_value(self.google_vision_api_key), secret_value(self.ebay_client_secret), *self.api_keys,
        ]  # fmt: skip
        return [v for v in values if len(v) >= 6]


@lru_cache
def get_settings() -> Settings:
    return Settings()
