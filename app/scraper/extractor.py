"""Main-text extraction: HTML in, clean article text out.

Trafilatura does the real work: it is purpose-built to find the main content of a page and drop navigation, ads, cookie banners,
sidebars, footers, comments and tracking markup. If it finds too little (some small or unusual pages) a conservative lxml fallback
collects the paragraphs of the page's <article>/<main> after removing scripts, styles, navigation, forms and elements whose class/id
marks them as banners, ads, sidebars or pop-ups.

Notes that matter in production:
  * Trafilatura's `deduplicate` option keeps GLOBAL state (it remembers text it has already returned and later drops it), which would
    make a re-fetched page come back empty. It is deliberately off.
  * Extraction is CPU-bound and cost grows with page size, so the HTML handed to it is capped (about 12x the text we will keep).
    Callers run extract_content in a worker thread under a timeout.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import trafilatura
from lxml import etree
from lxml import html as lxml_html

from app.scraper.sanitizer import sanitize_text, truncate_text
from app.util import parse_datetime

HTML_TO_TEXT_FACTOR = 12  # cap the HTML at max_chars * 12 (min MIN_HTML_CHARS): markup-heavy pages are ~10:1 markup to text
MIN_HTML_CHARS = 200_000
MIN_GOOD_TEXT = 80  # below this many characters the primary extractor's result is not trusted

_DROP_TAGS = ("script", "style", "noscript", "template", "iframe", "object", "embed", "svg", "canvas", "form", "button", "select", "input", "textarea", "nav", "aside", "footer", "dialog")
_BOILERPLATE = re.compile(
    r"(cookie|consent|gdpr|ccpa|privacy-banner|adsbygoogle|advert|\bads?\b|ad-slot|sponsor|promo|newsletter|subscribe|popup|modal|overlay|sidebar|"
    r"social|share-|sharing|breadcrumb|skip-link|paywall|tracking|outbrain|taboola|toolbar|banner)",
    re.IGNORECASE,
)
_BOILERPLATE_ROLES = {"navigation", "banner", "complementary", "contentinfo", "dialog", "alertdialog"}
_KEEP_TAGS = {"html", "body", "main", "article"}


@dataclass(slots=True)
class Extracted:
    text: str
    title: str | None
    published_at: str | None
    truncated: bool


def _fallback(html_text: str) -> tuple[str, str | None]:
    """(text, title) from the page's main element using lxml only. ('', None) if the HTML cannot be parsed."""
    try:
        root = lxml_html.fromstring(html_text)
    except (etree.ParserError, etree.XMLSyntaxError, ValueError):
        return "", None
    title = " ".join(str(root.xpath("string(//title)")).split()) or None
    etree.strip_elements(root, *_DROP_TAGS, with_tail=False)
    for element in root.xpath("//*[@class or @id or @role]"):
        if element.tag in _KEEP_TAGS or element.getparent() is None:
            continue
        marker = f"{element.get('class', '')} {element.get('id', '')}"
        if (element.get("role", "").lower() in _BOILERPLATE_ROLES or _BOILERPLATE.search(marker)) and not element.xpath(".//article|.//main"):
            element.drop_tree()
    mains = root.xpath("//article|//main|//*[@role='main']")
    scope = mains[0] if mains else (root.find("body") if root.find("body") is not None else root)
    blocks: list[str] = []
    for node in scope.xpath(".//h1|.//h2|.//h3|.//p|.//li|.//blockquote|.//pre"):
        text = " ".join(node.text_content().split())
        if len(text) >= 40 or (node.tag in ("h1", "h2", "h3") and len(text) >= 8):
            blocks.append(text)
    return "\n\n".join(blocks), title


def extract_content(html_text: str, url: str, max_chars: int, max_html_chars: int | None = None) -> Extracted:
    """Clean text (and title / date when found) from an HTML page. Empty text means nothing usable was found.

    `max_html_chars` caps how much HTML is processed (default: 12x max_chars, at least 200,000): a page's cost grows with its size and
    only the first `max_chars` of text are kept anyway."""
    html_cap = max_html_chars or max(MIN_HTML_CHARS, max_chars * HTML_TO_TEXT_FACTOR)
    html_text = html_text[:html_cap]
    text = ""
    title: str | None = None
    date: str | None = None
    try:
        doc = trafilatura.bare_extraction(
            html_text,
            url=url,
            include_comments=False,
            include_tables=False,
            include_images=False,
            include_links=False,
            favor_precision=True,
            deduplicate=False,  # see module docstring: this option is global state
            with_metadata=True,
        )
        if doc is not None:
            text = getattr(doc, "text", None) or (doc.get("text") if isinstance(doc, dict) else "") or ""
            title = getattr(doc, "title", None) or (doc.get("title") if isinstance(doc, dict) else None)
            date = getattr(doc, "date", None) or (doc.get("date") if isinstance(doc, dict) else None)
    except Exception:  # noqa: BLE001 - a parser bug on one odd page must not fail the request; use the fallback
        text = ""
    if len(text.strip()) < MIN_GOOD_TEXT:
        fallback_text, fallback_title = _fallback(html_text)
        if len(fallback_text) > len(text.strip()):
            text = fallback_text
        title = title or fallback_title
    text, truncated = truncate_text(sanitize_text(text), max_chars)
    return Extracted(text=text, title=sanitize_text(title) if title else None, published_at=parse_datetime(date), truncated=truncated)
