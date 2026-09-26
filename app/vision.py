"""Image source finding with Google Cloud Vision "web detection": give an image, get the web pages that show it, identical and similar
copies, and Google's best guess of what it is.

Needs the Cloud Vision API enabled on a Google Cloud project with billing (1,000 web-detection units a month are free, then it is paid;
see README), and an API key allowed to call it: GOOGLE_VISION_API_KEY, or GOOGLE_API_KEY if that key may use both APIs.

The image is either a URL (Google downloads it) or bytes (base64). When Google cannot download a URL itself, this service downloads the
image through its SSRF-protected, robots-respecting fetcher and sends the bytes instead.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

import httpx

from app.config import Settings
from app.providers.base import ProviderBadResponse
from app.upstream import request_json
from app.util import strip_tags

ENDPOINT = "https://vision.googleapis.com/v1/images:annotate"
# Vision's per-image message when it could not fetch an image URL ("We can not access the URL currently. Please download the content
# and pass it in.")
_CANNOT_ACCESS = re.compile(r"access the url|download the content|url.{0,40}(unreachable|not accessible|cannot be accessed)", re.IGNORECASE)


class VisionClient:
    name: ClassVar[str] = "google_vision"

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client

    def is_configured(self) -> bool:
        return bool(self.settings.vision_api_key)

    def timeout_budget(self) -> float:
        return max(self.settings.request_timeout_seconds, 15.0)  # web detection on a large image can take several seconds

    async def web_detection(self, image: dict[str, Any], max_results: int) -> dict[str, Any]:
        """One images:annotate call. `image` is {"source": {"imageUri": url}} or {"content": base64}. Returns the per-image response,
        which holds either "webDetection" or an "error" about this image. Raises ProviderError for request-level failures."""
        body = {"requests": [{"image": image, "features": [{"type": "WEB_DETECTION", "maxResults": max_results}]}]}
        data = await request_json(self.client, "POST", ENDPOINT, params={"key": self.settings.vision_api_key}, json=body)
        responses = data.get("responses") if isinstance(data, dict) else None
        if not isinstance(responses, list) or not responses or not isinstance(responses[0], dict):
            raise ProviderBadResponse("unexpected response shape", upstream_status=200)
        return responses[0]


def cannot_access_url(image_error: dict[str, Any]) -> bool:
    return bool(_CANNOT_ACCESS.search(str(image_error.get("message") or "")))


def _urls(items: Any, limit: int) -> list[str]:
    out: list[str] = []
    for item in items if isinstance(items, list) else []:
        url = item.get("url") if isinstance(item, dict) else None
        if isinstance(url, str) and url.lower().startswith(("http://", "https://")) and url not in out:
            out.append(url)
        if len(out) >= limit:
            break
    return out


def summarize_web_detection(detection: Any, limit: int) -> dict[str, Any]:
    """Vision's webDetection as clean JSON: only http(s) URLs, plain-text titles, no duplicates."""
    detection = detection if isinstance(detection, dict) else {}
    pages = []
    for page in detection.get("pagesWithMatchingImages") or []:
        if not isinstance(page, dict):
            continue
        url = page.get("url")
        if not (isinstance(url, str) and url.lower().startswith(("http://", "https://"))):
            continue
        pages.append(
            {
                "url": url,
                "title": strip_tags(page.get("pageTitle")) or None,
                "full_matching_images": _urls(page.get("fullMatchingImages"), limit),
                "partial_matching_images": _urls(page.get("partialMatchingImages"), limit),
            }
        )
        if len(pages) >= limit:
            break
    entities = [
        {"description": e["description"], "score": round(float(e.get("score") or 0.0), 4)}
        for e in detection.get("webEntities") or []
        if isinstance(e, dict) and isinstance(e.get("description"), str) and e["description"].strip()
    ][:limit]
    labels = [str(b["label"]) for b in detection.get("bestGuessLabels") or [] if isinstance(b, dict) and b.get("label")]
    return {
        "best_guess_labels": labels,
        "entities": entities,
        "pages": pages,
        "full_matching_images": _urls(detection.get("fullMatchingImages"), limit),
        "partial_matching_images": _urls(detection.get("partialMatchingImages"), limit),
        "visually_similar_images": _urls(detection.get("visuallySimilarImages"), limit),
    }
