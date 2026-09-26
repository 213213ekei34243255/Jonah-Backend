"""GET /, GET /health, GET /metrics."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from app import __version__
from app.api.deps import guard
from app.metrics import render_metrics

router = APIRouter(tags=["service"])


@router.get("/", summary="Service information")
async def root(request: Request) -> dict:
    return {"name": request.app.state.settings.app_name, "status": "ok", "version": __version__, "docs": "/docs"}


@router.get("/health", summary="Liveness check", description="Always cheap: does not call any provider. Used by Render / Docker health checks.")
async def health() -> dict:
    return {"status": "ok"}  # the same answer as the original Jonah proxy


@router.get("/metrics", summary="Prometheus metrics", include_in_schema=True, dependencies=[Depends(guard)])
async def metrics() -> Response:
    body, content_type = render_metrics()
    return Response(content=body, media_type=content_type)
