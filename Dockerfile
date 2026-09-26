# Jonah Search: small, non-root production image.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8000 \
    WEB_CONCURRENCY=1

WORKDIR /app

# Dependencies first so this layer is cached between code changes. lxml ships prebuilt wheels: no compiler needed.
COPY requirements.txt .
RUN pip install -r requirements.txt

RUN groupadd --system app && useradd --system --gid app --no-create-home --shell /usr/sbin/nologin app
COPY --chown=app:app app ./app
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', '8000'), timeout=4)" || exit 1

# Render (and most platforms) set PORT. The access log is off because it would print every query string; the app writes its own
# structured request log instead. Uvicorn's proxy-header handling is off: the app decides itself whether to trust X-Forwarded-For
# (TRUST_PROXY_HEADERS), so a direct caller cannot spoof its address.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port \"${PORT}\" --workers \"${WEB_CONCURRENCY}\" --no-proxy-headers --no-access-log --timeout-keep-alive 5"]
