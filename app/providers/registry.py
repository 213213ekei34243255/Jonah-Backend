"""Builds the provider objects. Adding a provider = write the class, add it to PROVIDER_CLASSES (README: "Adding a provider")."""

from __future__ import annotations

import httpx

from app.config import Settings
from app.providers.base import SearchProvider
from app.providers.bing import BingProvider
from app.providers.brave import BraveProvider
from app.providers.google import GoogleProvider
from app.providers.searxng import SearxngProvider
from app.providers.wikipedia import WikipediaProvider

PROVIDER_CLASSES: list[type[SearchProvider]] = [SearxngProvider, BraveProvider, BingProvider, GoogleProvider, WikipediaProvider]


def build_providers(settings: Settings, client: httpx.AsyncClient) -> list[SearchProvider]:
    """Every known provider (configured or not); the manager only uses the configured ones, /providers reports them all."""
    return [cls(settings, client) for cls in PROVIDER_CLASSES]
