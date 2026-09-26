"""Cleaning of extracted text before it is returned to an AI agent.

Page text is untrusted. Beyond removing markup remnants we strip characters that only serve to hide or reorder text (zero-width and
bidirectional-override characters), which are a known way to smuggle instructions past a human reviewer.
"""

from __future__ import annotations

import re
import unicodedata

# zero-width space/joiners, bidi embeddings/overrides/isolates, word joiner, BOM, soft hyphen
_INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤⁦-⁩﻿­]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_TRAILING_SPACE = re.compile(r"[ \t]+\n")
_MANY_NEWLINES = re.compile(r"\n{3,}")
_MANY_SPACES = re.compile(r"[ \t]{2,}")


def sanitize_text(text: str | None) -> str:
    """Plain, tidy text: no control or invisible characters, single spaces, at most one blank line in a row."""
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    text = _CONTROL.sub(" ", _INVISIBLE.sub("", text))
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    text = _MANY_SPACES.sub(" ", text)
    text = _TRAILING_SPACE.sub("\n", text)
    return _MANY_NEWLINES.sub("\n\n", text).strip()


def truncate_text(text: str, max_chars: int) -> tuple[str, bool]:
    """Cut to at most max_chars, at a word boundary. Returns (text, was_truncated)."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False
    cut = text[:max_chars]
    boundary = max(cut.rfind(" "), cut.rfind("\n"))
    if boundary > max_chars * 0.6:
        cut = cut[:boundary]
    return cut.rstrip(), True
