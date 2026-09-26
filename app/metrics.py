"""Prometheus-compatible metrics, served at GET /metrics. Metric names follow the spec:
search_requests_total, search_request_duration_seconds, provider_requests_total, provider_errors_total (+ provider latency)."""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

SEARCH_REQUESTS = Counter("search_requests", "Search requests handled.", ["mode", "outcome"])  # exposed as search_requests_total
SEARCH_DURATION = Histogram(
    "search_request_duration_seconds", "End-to-end duration of a search request.", ["mode"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30),
)  # fmt: skip
PROVIDER_REQUESTS = Counter("provider_requests", "Calls made to a search provider.", ["provider", "outcome"])  # provider_requests_total
PROVIDER_ERRORS = Counter("provider_errors", "Failed provider calls, by error code.", ["provider", "error"])  # provider_errors_total
PROVIDER_DURATION = Histogram(
    "provider_request_duration_seconds", "Duration of a provider call.", ["provider"], buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10)
)  # fmt: skip


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
