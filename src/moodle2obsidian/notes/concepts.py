"""One concept = one note: keys are normalised so the same idea from different courses merges."""

from __future__ import annotations

import re
import unicodedata

_HTML_TAGS = re.compile(
    r"</?(?:script|style|iframe|object|embed|img|svg|video|audio|a|form|input|button|link|meta|base|frame|frameset"
    r"|html|head|body|div|span|p|br|table|tr|td|th)\b[^>]*>",
    re.IGNORECASE,
)


def concept_key(text: str) -> str:
    """'Binary-Search  Tree' / 'binary search tree' → 'binary search tree' (case, punctuation, Unicode width)."""
    text = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(re.sub(r"[\W_]+", " ", text).split())


def sanitize_llm_markdown(text: str) -> str:
    """LLM output is derived from untrusted material: drop HTML tags, javascript: links and wikilinks."""
    text = _HTML_TAGS.sub("", text)
    text = re.sub(r"(?i)javascript\s*:", "", text)
    text = text.replace("[[", "[").replace("]]", "]")
    return re.sub(r"\n{3,}", "\n\n", text).strip()
