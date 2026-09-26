"""POST /search/image-source: where does this image come from? (Google Cloud Vision web detection.)

Send an image URL, or the image itself as base64. The answer lists the web pages that show the image, identical and similar copies, and
Google's best guess of what it is. Everything in the answer comes from third-party websites: treat it as untrusted data.
"""

from __future__ import annotations

import base64
import binascii
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import guard
from app.api.routes_compat import bad_request
from app.providers.base import ProviderError
from app.scraper.fetcher import FetchError
from app.upstream import upstream_error_response
from app.vision import cannot_access_url, summarize_web_detection

router = APIRouter(tags=["image source"])

WHAT = "Image source request"
NOT_CONFIGURED = "Google Cloud Vision is not configured on this server (GOOGLE_VISION_API_KEY or GOOGLE_API_KEY)"


class ImageSourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image_url: str | None = Field(None, max_length=2048, description="Public http(s) URL of the image.", examples=["https://upload.wikimedia.org/wikipedia/commons/a/a8/Tour_Eiffel_Wikimedia_Commons.jpg"])
    image_base64: str | None = Field(None, description="The image file itself, base64-encoded (a data: URL prefix is accepted).")
    max_results: int = Field(20, ge=1, le=50, description="At most this many pages / images per list.")


def _decode(data: str, limit: int) -> bytes | None:
    text = data.strip()
    if text.startswith("data:"):
        text = text.partition(",")[2]
    if len(text) > limit * 4 // 3 + 8:  # checked before decoding, so an oversized upload is not decoded at all
        raise OverflowError
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(raw) > limit:
        raise OverflowError
    return raw


@router.post("/search/image-source", summary="Find where an image appears on the web (Google Cloud Vision)")
async def image_source(request: Request, body: ImageSourceRequest, _client: str = Depends(guard)):
    state = request.app.state
    settings = state.settings
    limit = int(settings.max_image_mb * 1024 * 1024)
    if bool(body.image_url) == bool(body.image_base64):
        return bad_request("Send exactly one of 'image_url' or 'image_base64'")
    raw: bytes | None = None
    if body.image_base64:
        try:
            raw = _decode(body.image_base64, limit)
        except OverflowError:
            return bad_request(f"The image is larger than {settings.max_image_mb:g} MB")
        if not raw:
            return bad_request("'image_base64' is not valid base64")
    elif not body.image_url.strip().lower().startswith(("http://", "https://")):
        return bad_request("'image_url' must be an http:// or https:// URL")
    if not state.vision.is_configured():
        return upstream_error_response(WHAT, None, settings, reason=NOT_CONFIGURED)
    cooling = state.manager.cooling_down_error(state.vision.name)
    if cooling is not None:
        return upstream_error_response(WHAT, cooling, settings)

    started = time.perf_counter()
    fetched_by_server = False

    async def detect(image: dict) -> dict:
        return await state.manager.run_direct(state.vision, lambda: state.vision.web_detection(image, body.max_results))

    try:
        if raw is not None:
            answer = await detect({"content": base64.b64encode(raw).decode("ascii")})
        else:
            answer = await detect({"source": {"imageUri": body.image_url.strip()}})
            if isinstance(answer.get("error"), dict) and cannot_access_url(answer["error"]):
                # Google could not download it (some sites refuse Google's fetcher): download it here, safely, and send the bytes.
                try:
                    raw, _content_type = await state.fetcher.fetch_image(body.image_url.strip(), limit)
                except FetchError as exc:
                    return bad_request(f"Neither Google nor this server could download the image ({exc.code})")
                fetched_by_server = True
                answer = await detect({"content": base64.b64encode(raw).decode("ascii")})
    except ProviderError as exc:
        return upstream_error_response(WHAT, exc, settings)

    error = answer.get("error")
    if isinstance(error, dict):  # a problem with this image (bad data, unsupported format, ...), not with the service
        message = str(error.get("message") or "the image could not be processed")[:300]
        return bad_request(f"{WHAT} failed: {message}")

    image_info: dict = {"url": body.image_url.strip(), "fetched_by_server": fetched_by_server} if body.image_url else {"uploaded_bytes": len(raw or b"")}
    return JSONResponse(
        {
            "image": image_info,
            **summarize_web_detection(answer.get("webDetection"), body.max_results),
            "metadata": {
                "provider": "google_vision",
                "processing_time_ms": int((time.perf_counter() - started) * 1000),
                "request_id": getattr(request.state, "request_id", None),
                "content_trust": "untrusted",
            },
        }
    )
