"""Structured (JSON) logging with secret redaction.

Rules: log events, not payloads. Authorization headers, API keys and provider URLs (which can carry a key in the query string,
e.g. Google's ?key=...) are never logged, and any configured secret that still ends up in a message is masked.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import datetime, timezone

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

# Field names that are never written, whatever their value.
SENSITIVE_KEYS = {"authorization", "api_key", "apikey", "key", "token", "secret", "password", "x-subscription-token", "cookie"}


class JsonFormatter(logging.Formatter):
    def __init__(self, secrets: list[str] | None = None) -> None:
        super().__init__()
        plain = {s for s in (secrets or []) if s}
        escaped = {json.dumps(s, ensure_ascii=False)[1:-1] for s in plain}  # the form a secret takes inside the JSON line (\" \\ ...)
        self._secrets = sorted(plain | escaped, key=len, reverse=True)

    def _mask(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "[redacted]")
        return text

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            for key, value in fields.items():
                if str(key).lower() in SENSITIVE_KEYS:
                    continue
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return self._mask(json.dumps(payload, default=str, ensure_ascii=False))


def configure_logging(level: str = "INFO", secrets: list[str] | None = None) -> None:
    root = logging.getLogger()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(secrets))
    root.handlers[:] = [handler]
    root.setLevel(level.upper() if level.upper() in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"} else "INFO")
    # httpx logs every request URL at INFO. For Google that URL contains the API key, and for every provider it contains the
    # user's query: keep these libraries quiet. (Uvicorn's access log is disabled in the Dockerfile for the same reason.)
    for noisy in ("httpx", "httpcore", "hpack", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields) -> None:
    """One structured event: log_event(log, 'search', query='...', provider='searxng', results=10, duration_ms=431)."""
    logger.log(level, event, extra={"fields": {"event": event, **fields}})
