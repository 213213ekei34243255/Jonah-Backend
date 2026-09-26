"""GET /providers: which providers exist, which are enabled, and how healthy each is. Never includes a key."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.api.deps import guard
from app.models.search import ProviderInfo

router = APIRouter(tags=["providers"])


@router.get(
    "/providers",
    response_model=list[ProviderInfo],
    summary="Configured providers and their status",
    description="`status` is `ok`, `cooling_down` (recently failed / out of quota; skipped for a while) or `not_configured`. Keys are never returned.",
    dependencies=[Depends(guard)],
)
async def providers(request: Request) -> list[ProviderInfo]:
    state = request.app.state
    manager = state.manager
    entries = [(p, "search") for p in manager.all_providers] + [(state.news, "news"), (state.vision, "image_source")]
    infos: list[ProviderInfo] = []
    for provider, kind in entries:
        configured = provider.is_configured()
        health = manager.health.get(provider.name)
        cooldown = manager.health.cooldown_remaining(provider.name)
        status = "not_configured" if not configured else "cooling_down" if cooldown > 0 else "ok"
        infos.append(
            ProviderInfo(
                name=provider.name, kind=kind, configured=configured, enabled=configured,
                priority=manager.priority_of(provider.name) if kind == "search" else None, status=status,
                supports_freshness=getattr(provider, "supports_freshness", False), supports_language=getattr(provider, "supports_language", False),
                consecutive_failures=health.consecutive_failures, cooldown_remaining_seconds=int(cooldown), last_error=health.last_error,
                last_latency_ms=int(health.last_latency_ms) if health.last_latency_ms is not None else None, requests=health.requests, failures=health.failures,
            )
        )  # fmt: skip
    return infos
