"""Gemini fallback parser - used ONLY when deterministic extraction fails.

Contract:
  - Receives a REDUCED, sanitized DOM snapshot (never full page, never any
    user/account data - competitor product pages are public marketing pages).
  - Must return strict JSON matching the extraction schema.
  - Python validates every field; invalid output is discarded, never stored.
  - Gemini never writes to the database and never executes code.
"""

from __future__ import annotations

import json
import os
import re

MAX_SNAPSHOT_CHARS = 20_000

SCHEMA_HINT = """\
Return ONLY a JSON object with exactly these keys (no markdown, no prose):
{
  "product_name": string,
  "total_price": number,          // IDR, the listed selling price
  "currency": "IDR",
  "base_units": number,           // main unit count (e.g. 86 diamonds)
  "bonus_units": number,          // bonus units, 0 if none
  "confidence": number            // 0.0 - 1.0
}
If you cannot determine the price or units reliably, return:
{"error": "not_found"}
"""

_SCRIPT_RE = re.compile(r"<(script|style|noscript|svg)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_BLANK_RE = re.compile(r"\n\s*\n+")


def reduce_dom(html: str, max_chars: int = MAX_SNAPSHOT_CHARS) -> str:
    """Strip scripts/styles/comments and tags, keep visible text + links."""
    cleaned = _SCRIPT_RE.sub(" ", html or "")
    cleaned = _COMMENT_RE.sub(" ", cleaned)
    # Keep href attributes so product links survive the reduction.
    cleaned = re.sub(r"<a\s+[^>]*href=\"([^\"]+)\"[^>]*>", r" [link: \1] ", cleaned)
    text = _TAG_RE.sub("\n", cleaned)
    text = _BLANK_RE.sub("\n", text)
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    return text[:max_chars]


def _coerce_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= 1_000_000 else None


def _coerce_price(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if 0 < number <= 100_000_000 else None


def validate_result(data: dict) -> dict | None:
    """Validate a Gemini (or any) extraction result. Returns normalized dict or None."""
    if not isinstance(data, dict) or data.get("error"):
        return None
    price = _coerce_price(data.get("total_price"))
    base_units = _coerce_int(data.get("base_units"))
    if price is None or base_units is None or base_units <= 0:
        return None
    currency = str(data.get("currency") or "IDR").upper()
    if currency != "IDR":
        return None
    bonus_units = _coerce_int(data.get("bonus_units")) or 0
    if bonus_units < 0 or bonus_units > base_units:
        bonus_units = 0
    try:
        confidence = float(data.get("confidence") or 0.5)
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = min(max(confidence, 0.0), 1.0)
    return {
        "product_name": str(data.get("product_name") or "")[:200],
        "total_price": price,
        "currency": "IDR",
        "base_units": base_units,
        "bonus_units": bonus_units,
        "method": "gemini_fallback",
        "confidence": round(confidence, 3),
    }


def parse_with_gemini(html: str, page_url: str, model_name: str | None = None) -> dict | None:
    """Send a reduced DOM snapshot to Gemini and return a validated result.

    Returns None when Gemini is not configured, the call fails, or the
    response fails validation. Never raises into the scraper loop.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return None
    model_name = (model_name or os.environ.get("GEMINI_MODEL") or "gemini-2.0-flash").strip()

    try:
        from google import genai
    except ImportError:
        return None

    snapshot = reduce_dom(html)
    prompt = (
        "You are a precise data-extraction assistant for a game top-up price "
        "monitor. The following is visible text from a product page.\n\n"
        f"Page URL: {page_url}\n\n"
        f"{SCHEMA_HINT}\n\nPAGE CONTENT:\n{snapshot}"
    )

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(model=model_name, contents=prompt)
        raw = response.text or ""
        # Tolerate markdown-fenced JSON.
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
        data = json.loads(raw)
    except Exception:
        return None

    return validate_result(data if isinstance(data, dict) else {})
