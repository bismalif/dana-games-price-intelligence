"""Deterministic price/package extraction from competitor pages.

Fallback order (cheapest and most reliable first):
  1. JSON-LD structured data (schema.org Product / Offer)
  2. Meta tags (og:price:amount, product:price:amount, itemprop=price)
  3. Configured CSS selectors (from source_sku_mappings)
  4. Text heuristics on visible elements (IDR prices + unit counts)

All functions take BeautifulSoup objects / plain strings so they are fully
unit-testable offline.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation

from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

IDR_PRICE_RE = re.compile(r"(?:rp\.?\s*)([0-9][0-9.\s,]{2,})|([0-9][0-9.]{4,})(?:\s*(?:idr|rupiah))", re.IGNORECASE)
UNITS_RE = re.compile(r"(\d[\d.,]*)\s*(?:diamond|dm|dias|coin|gold|uc|voucher|item|unit)", re.IGNORECASE)
BONUS_RE = re.compile(r"\+\s*(\d[\d.,]*)\s*(?:bonus|extra|free)?", re.IGNORECASE)


def parse_idr_price(text: str) -> Decimal | None:
    """Extract an IDR amount from text like 'Rp 199.000' or 'Rp199,000'."""
    if not text:
        return None
    match = IDR_PRICE_RE.search(text)
    if not match:
        return None
    raw = match.group(1) or match.group(2)
    return _to_decimal(raw)


def _to_decimal(raw: str) -> Decimal | None:
    if not raw:
        return None
    cleaned = raw.strip().replace(" ", "").replace("\u00a0", "")
    if not cleaned or not any(ch.isdigit() for ch in cleaned):
        return None
    has_dot = "." in cleaned
    has_comma = "," in cleaned
    if has_dot and has_comma:
        # Decide by whichever separator comes last.
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif has_dot:
        # IDR: '.' is a thousands separator when the last group has 3 digits
        # ('199.000' -> 199000); otherwise it is a decimal point.
        if len(cleaned.split(".")[-1]) == 3:
            cleaned = cleaned.replace(".", "")
    elif has_comma:
        # IDR: ',' thousands ('199,000' -> 199000) vs decimal ('199,5').
        if len(cleaned.split(",")[-1]) == 3:
            cleaned = cleaned.replace(",", "")
        else:
            cleaned = cleaned.replace(",", ".")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    # Sanity: IDR prices for game top-ups are at least hundreds of rupiah.
    if value <= 0 or value > Decimal("100000000"):
        return None
    return value


def parse_units(text: str) -> tuple[int, int] | None:
    """Extract (base_units, bonus_units) from text like '86 Diamonds + 8 Bonus'.

    Returns None when no unit count is found.
    """
    if not text:
        return None
    unit_match = UNITS_RE.search(text)
    if not unit_match:
        # Fall back to a leading number (e.g. '86 Diamonds' without keyword
        # match is handled above; bare '258' titles are too risky to guess).
        return None
    base = _to_int(unit_match.group(1))
    if base is None or base <= 0:
        return None
    bonus = 0
    bonus_match = BONUS_RE.search(text, pos=unit_match.end())
    if bonus_match:
        bonus_val = _to_int(bonus_match.group(1))
        if bonus_val is not None and 0 < bonus_val <= base:
            bonus = bonus_val
    return base, bonus


def _to_int(raw: str) -> int | None:
    if not raw:
        return None
    cleaned = raw.replace(".", "").replace(",", "").strip()
    try:
        return int(cleaned)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 1. JSON-LD
# ---------------------------------------------------------------------------

def extract_from_json_ld(soup: BeautifulSoup) -> dict | None:
    """Look for schema.org Product entries in <script type="application/ld+json">."""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for product in _iter_products(data):
            offer = product.get("offers") or {}
            offers = offer if isinstance(offer, list) else [offer]
            for candidate in offers:
                price = candidate.get("price") or candidate.get("lowPrice")
                price_decimal = _to_decimal(str(price)) if price is not None else None
                if price_decimal is None:
                    continue
                currency = (candidate.get("priceCurrency") or "IDR").upper()
                if currency != "IDR":
                    continue
                name = product.get("name") or ""
                units = parse_units(name)
                return {
                    "product_name": name,
                    "total_price": price_decimal,
                    "currency": currency,
                    "base_units": units[0] if units else None,
                    "bonus_units": units[1] if units else 0,
                    "method": "json_ld",
                    "confidence": 0.95,
                }
    return None


def _iter_products(data):
    if isinstance(data, dict):
        if data.get("@type") in ("Product", "Offer", "ProductGroup"):
            yield data
        for value in data.values():
            yield from _iter_products(value)
    elif isinstance(data, list):
        for item in data:
            yield from _iter_products(item)


# ---------------------------------------------------------------------------
# 2. Meta tags
# ---------------------------------------------------------------------------

META_PRICE_PROPS = ("og:price:amount", "product:price:amount", "twitter:data1")


def extract_from_meta(soup: BeautifulSoup) -> dict | None:
    for prop in META_PRICE_PROPS:
        tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        if not tag:
            continue
        price = _to_decimal(tag.get("content", ""))
        if price is None:
            continue
        currency_tag = soup.find("meta", attrs={"property": "og:price:currency"}) or soup.find(
            "meta", attrs={"property": "product:price:currency"}
        )
        currency = ((currency_tag.get("content") if currency_tag else "") or "IDR").upper()
        if currency != "IDR":
            continue
        title_tag = soup.find("meta", attrs={"property": "og:title"}) or soup.title
        name = (title_tag.get("content") if title_tag and title_tag.has_attr("content") else "") or (
            title_tag.string if title_tag else ""
        ) or ""
        units = parse_units(name)
        if units is None:
            continue  # meta prices without unit context are too risky
        return {
            "product_name": name.strip(),
            "total_price": price,
            "currency": currency,
            "base_units": units[0],
            "bonus_units": units[1],
            "method": "meta_tags",
            "confidence": 0.8,
        }
    return None


# ---------------------------------------------------------------------------
# 3. Configured CSS selectors
# ---------------------------------------------------------------------------

def extract_from_selectors(soup: BeautifulSoup, price_selector: str | None, units_selector: str | None) -> dict | None:
    if not price_selector:
        return None
    price_el = soup.select_one(price_selector)
    if not price_el:
        return None
    price = parse_idr_price(price_el.get_text(" ", strip=True))
    if price is None:
        return None
    name = ""
    units = None
    if units_selector:
        units_el = soup.select_one(units_selector)
        if units_el:
            units_text = units_el.get_text(" ", strip=True)
            units = parse_units(units_text)
            name = units_text
    if units is None:
        # Try the page title / headings for unit context.
        heading = soup.find(["h1", "h2"])
        name = heading.get_text(" ", strip=True) if heading else ""
        units = parse_units(name)
    if units is None:
        return None
    return {
        "product_name": name.strip(),
        "total_price": price,
        "currency": "IDR",
        "base_units": units[0],
        "bonus_units": units[1],
        "method": "css_selectors",
        "confidence": 0.85,
    }


# ---------------------------------------------------------------------------
# 4. Text heuristics over visible elements
# ---------------------------------------------------------------------------

PRICE_TEXT_RE = re.compile(r"rp\.?\s*[0-9]", re.IGNORECASE)


def extract_from_text(soup: BeautifulSoup) -> dict | None:
    """Scan visible elements for paired unit labels and IDR prices.

    Returns the first coherent (units, price) pair, or a list-style result
    when the page shows a package grid.
    """
    results = []
    for el in soup.find_all(string=PRICE_TEXT_RE):
        container = el.parent
        if container is None:
            continue
        # Walk up a couple of levels to find a container holding both units
        # and price (card-style layouts).
        scope = container
        for _ in range(3):
            scope_text = scope.get_text(" ", strip=True)
            units = parse_units(scope_text)
            price = parse_idr_price(scope_text)
            if units and price:
                results.append(
                    {
                        "product_name": scope_text[:200],
                        "total_price": price,
                        "currency": "IDR",
                        "base_units": units[0],
                        "bonus_units": units[1],
                        "method": "text_heuristics",
                        "confidence": 0.6,
                    }
                )
                break
            if scope.parent is None:
                break
            scope = scope.parent
    if not results:
        return None
    # Prefer the smallest price among detected cards (list pages often show
    # 'starting from' prices); otherwise return the first coherent result.
    return min(results, key=lambda r: r["total_price"])


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def extract(html: str, price_selector: str | None = None, units_selector: str | None = None) -> dict | None:
    """Run the deterministic extraction chain. Returns a normalized dict or None."""
    soup = BeautifulSoup(html or "", "html.parser")
    for extractor in (
        lambda: extract_from_json_ld(soup),
        lambda: extract_from_meta(soup),
        lambda: extract_from_selectors(soup, price_selector, units_selector),
        lambda: extract_from_text(soup),
    ):
        result = extractor()
        if result:
            return result
    return None
