"""POST /search and GET /search."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.api.deps import get_service, guard
from app.models.search import Freshness, Mode, SearchRequest, SearchResponse
from app.providers.manager import UnknownProviderError
from app.service import SearchService

router = APIRouter(tags=["search"])

_RESPONSES = {
    200: {"description": "Results (possibly empty). `errors[]` lists any provider that failed along the way."},
    400: {"description": "A specific `provider` was requested that does not exist or is not configured."},
    401: {"description": "API authentication is enabled and the Bearer key is missing or wrong."},
    422: {"description": "Invalid request parameters."},
    429: {"description": "Rate limit exceeded (see the Retry-After header)."},
    503: {"model": SearchResponse, "description": "No provider is configured, or every provider failed. The body has the usual shape with `errors[]` explaining why."},
}


async def _run(request: Request, search: SearchRequest, service: SearchService) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    try:
        response, status = await service.search(search, request_id)
    except UnknownProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(status_code=status, content=response.model_dump(mode="json"))


def _split(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for value in values or []:
        out.extend(part.strip() for part in value.split(",") if part.strip())
    return out


@router.get(
    "/search",
    response_model=SearchResponse,
    responses=_RESPONSES,
    summary="Search the web (GET)",
    description=(
        "Search through the configured providers and return clean, normalised JSON.\n\n"
        "Example: `GET /search?q=latest+AI+news&max_results=10&fetch_content=true`\n\n"
        "`domains` / `exclude_domains` may be repeated or comma-separated."
    ),
)
async def search_get(
    request: Request,
    q: Annotated[str, Query(min_length=1, max_length=500, description="What to search for.", examples=["latest AI news"])],
    max_results: Annotated[int, Query(ge=1, le=100, description="Number of results (capped by the server's MAX_RESULTS).")] = 10,
    fetch_content: Annotated[bool, Query(description="Also download each result page and return its clean text.")] = False,
    mode: Annotated[Mode, Query(description="fast = first healthy provider, deep = several providers in parallel, merged.")] = "fast",
    freshness: Annotated[Freshness, Query(description="hour | day | week | month | year | any")] = "any",
    domains: Annotated[list[str] | None, Query(description="Only these domains (repeat or comma-separate).")] = None,
    exclude_domains: Annotated[list[str] | None, Query(description="Never these domains.")] = None,
    language: Annotated[str | None, Query(description="Preferred language, e.g. en or pt-BR.")] = None,
    provider: Annotated[str | None, Query(description="Force one provider (see /providers).")] = None,
    service: SearchService = Depends(get_service),
    _client: str = Depends(guard),
):
    try:
        search = SearchRequest(
            query=q, max_results=max_results, fetch_content=fetch_content, mode=mode, freshness=freshness, domains=_split(domains),
            exclude_domains=_split(exclude_domains), language=language, provider=provider,
        )  # fmt: skip
    except ValidationError as exc:
        return JSONResponse(status_code=422, content={"detail": [{"loc": ["query", *map(str, e["loc"])], "msg": e["msg"], "type": e["type"]} for e in exc.errors()]})
    return await _run(request, search, service)


@router.post(
    "/search",
    response_model=SearchResponse,
    responses=_RESPONSES,
    summary="Search the web (POST)",
    description="Same as GET /search, with the parameters as JSON. This is the form an AI agent should use.",
)
async def search_post(
    request: Request,
    body: SearchRequest,
    service: SearchService = Depends(get_service),
    _client: str = Depends(guard),
):
    return await _run(request, body, service)
