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

IDR_PRICE_RE = re.compile(
    r"\b(?:rp|idr)\.?\s*:?\s*([0-9][0-9.\s,]{2,})|([0-9][0-9.]{4,})(?:\s*(?:idr|rupiah))",
    re.IGNORECASE,
)
UNITS_RE = re.compile(r"(\d[\d.,]*)\s*(?:diamond|dm|dias|coin|gold|uc|voucher|item|unit)", re.IGNORECASE)
BONUS_RE = re.compile(r"\+\s*(\d[\d.,]*)\s*(?:bonus|extra|free)?", re.IGNORECASE)
# DANA catalog format: '14 Diamonds (13 + 1 Bonus)' = total (base + bonus).
# The parenthesized composition is authoritative, NOT the leading total.
COMPOSITION_RE = re.compile(
    r"(\d[\d.,]*)\s*(?:diamonds?|dias|dm)\s*\(\s*(\d[\d.,]*)\s*\+\s*(\d[\d.,]*)\s*(?:bonus)?\s*\)",
    re.IGNORECASE,
)
# UniPin-style composition: '11 + 1 Diamonds' = base + bonus.
REVERSE_COMPOSITION_RE = re.compile(
    r"(\d[\d.,]*)\s*\+\s*(\d[\d.,]*)\s*(?:diamonds?|dias|dm)\b",
    re.IGNORECASE,
)

# Realistic IDR bounds for game top-ups. Anything outside is a mispaired
# text fragment (e.g. a page header priced against a unit label), not a
# real package - rejecting it prevents garbage rows and false alerts.
MIN_TOTAL_PRICE = Decimal("500")
MAX_TOTAL_PRICE = Decimal("50000000")
MIN_PER_UNIT = Decimal("100")
MAX_PER_UNIT = Decimal("25000")


def price_plausible(total: Decimal, base_units: int, bonus_units: int) -> bool:
    if total < MIN_TOTAL_PRICE or total > MAX_TOTAL_PRICE:
        return False
    units = base_units + bonus_units
    if units > 0:
        per_unit = total / Decimal(units)
        if per_unit < MIN_PER_UNIT or per_unit > MAX_PER_UNIT:
            return False
    return True


def parse_idr_price(text: str) -> Decimal | None:
    """Extract the FIRST IDR amount from text like 'Rp 199.000'."""
    prices = parse_idr_prices(text)
    return prices[0] if prices else None


def parse_idr_prices(text: str) -> list[Decimal]:
    """Extract ALL IDR amounts in text, in order of appearance.

    Catalog cards often show both an original and a discounted price
    ('5 Diamonds Rp1.150 Rp1.000') - the LAST one is the final price.
    """
    if not text:
        return []
    values: list[Decimal] = []
    for match in IDR_PRICE_RE.finditer(text):
        raw = match.group(1) or match.group(2)
        value = _to_decimal(raw)
        if value is not None:
            values.append(value)
    return values


def final_idr_price(text: str) -> Decimal | None:
    """The FINAL (discounted) price: the last IDR amount in the text."""
    prices = parse_idr_prices(text)
    return prices[-1] if prices else None


# Promotional ribbon text that contaminates product labels on catalog cards
# (e.g. 'Weekly Diamond Pass Sat Set Murah' -> 'Weekly Diamond Pass').
PROMO_PHRASES = (
    "sat set",
    "satset",
    "special discount",
    "best seller",
    "terlaris",
    "terbaru",
    "pengisian pertama",
    "rewards",
    "promo",
    "diskon",
    "murah",
    "off",
)


def clean_product_name(text: str) -> str:
    """Strip price amounts, currency tokens, and promo ribbon text from a
    product label.

    '5 Diamonds Rp1.150 Rp1.000' -> '5 Diamonds'
    'Weekly Diamond Pass Sat Set Murah' -> 'Weekly Diamond Pass'
    '100 Diamonds (50+50) pengisian pertama! Dari -31%' -> '100 Diamonds (50+50)'
    """
    if not text:
        return ""
    cleaned = IDR_PRICE_RE.sub(" ", text)
    cleaned = re.sub(r"\b(?:rp|idr|rupiah)\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bdari\s*-\s*\d+\s*%", " ", cleaned, flags=re.IGNORECASE)
    for phrase in PROMO_PHRASES:
        cleaned = re.sub(rf"\b{re.escape(phrase)}\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—|·,.!$")
    return cleaned[:200]


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
    """Extract (base_units, bonus_units) from a product label.

    Supported formats:
      '86 Diamonds + 8 Bonus'            -> (86, 8)
      '14 Diamonds (13 + 1 Bonus)'       -> (13, 1)   # DANA: total(base+bonus)
      '70 Diamonds'                      -> (70, 0)

    Returns None when no unit count is found.
    """
    if not text:
        return None

    # DANA-style explicit composition: total (base + bonus).
    composition = COMPOSITION_RE.search(text)
    if composition:
        base = _to_int(composition.group(2))
        bonus = _to_int(composition.group(3))
        if base and base > 0 and bonus is not None and 0 <= bonus <= 1_000_000:
            return base, bonus

    # UniPin-style composition: '11 + 1 Diamonds' = base + bonus.
    reverse = REVERSE_COMPOSITION_RE.search(text)
    if reverse:
        base = _to_int(reverse.group(1))
        bonus = _to_int(reverse.group(2))
        if base and base > 0 and bonus is not None and 0 <= bonus <= base:
            return base, bonus

    unit_match = UNITS_RE.search(text)
    if not unit_match:
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

# No trailing \b: 'Rp1.150' has no boundary between 'p' and the digit.
PRICE_TEXT_RE = re.compile(r"\b(?:rp|idr)\.?\s*:?\s*[0-9]", re.IGNORECASE)

# Pass products have no fixed unit count ('Weekly Diamond Pass'); they are
# captured with base_units=0 and matched to SKUs by normalized name.
PASS_RE = re.compile(r"\b(weekly|monthly)\b[\w\s]{0,30}\bpass\b", re.IGNORECASE)

# A card scope larger than this is a grid, not a single package - pairing
# units with prices across it would be meaningless.
MAX_CARD_CHARS = 400


def _card_scope(node) -> str | None:
    """Find the smallest ancestor text scope that contains a price AND
    unit info (or a pass label). Walking up at most 4 levels keeps the
    scope inside one card; sibling-based layouts (Codashop, GoPay) put
    the price and the label in adjacent elements under a shared parent.
    """
    scope = node
    for _ in range(4):
        if scope is None:
            return None
        text = scope.get_text(" ", strip=True)
        if text and PRICE_TEXT_RE.search(text):
            has_units = parse_units(text) is not None
            is_pass = PASS_RE.search(text) is not None
            if (has_units or is_pass) and len(text) <= MAX_CARD_CHARS:
                return text
        scope = scope.parent
    return None


def _units_start_position(text: str) -> int | None:
    """Position of the FIRST unit-composition or unit-keyword match."""
    for pattern in (COMPOSITION_RE, REVERSE_COMPOSITION_RE, UNITS_RE):
        match = pattern.search(text)
        if match:
            return match.start()
    return None


def extract_packages(html: str) -> list[dict]:
    """Extract EVERY package card from a catalog page.

    Card layout: a short text scope containing unit keywords (or a pass
    label) plus one or more IDR amounts. Uses the FINAL (last) price when a
    card shows original + discounted amounts. Deduplicates by unit
    composition (or pass name). Implausible pairings (e.g. a page header
    priced against a tiny unit label) are rejected.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    packages: list[dict] = []
    seen: set[tuple] = set()
    for el in soup.find_all(string=PRICE_TEXT_RE):
        scope_text = _card_scope(el.parent)
        if not scope_text:
            continue
        prices = parse_idr_prices(scope_text)
        if not prices:
            continue
        # Real cards lead with the denomination; banners and headers mention
        # units mid-sentence. Reject late unit mentions. Pass products have
        # no unit pattern at all - they are exempt from this gate.
        units_pos = _units_start_position(scope_text)
        is_pass = units_pos is None and bool(PASS_RE.search(scope_text))
        if not is_pass and (units_pos is None or units_pos > 60):
            continue
        units = parse_units(scope_text)
        if units:
            base, bonus = units
            key = ("units", base, bonus)
        elif PASS_RE.search(scope_text):
            base, bonus = 0, 0
            key = ("pass", clean_product_name(scope_text).lower())
        else:
            continue
        if key in seen:
            continue
        final = prices[-1]  # final price wins
        if not price_plausible(final, base, bonus):
            continue
        seen.add(key)
        packages.append(
            {
                "product_name": clean_product_name(scope_text) or f"package {base}+{bonus}",
                "total_price": final,
                "currency": "IDR",
                "base_units": base,
                "bonus_units": bonus,
                "method": "text_heuristics",
                "confidence": 0.6,
            }
        )
    return packages


def extract_from_text(soup: BeautifulSoup) -> dict | None:
    """Scan visible elements for paired unit labels and IDR prices.

    Returns the cheapest coherent (units, final price) pair - list pages
    often show 'starting from' prices.
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
            prices = parse_idr_prices(scope_text)
            units = parse_units(scope_text)
            if units and prices and len(scope_text) <= MAX_CARD_CHARS:
                final = prices[-1]  # final price wins
                if not price_plausible(final, units[0], units[1]):
                    continue
                results.append(
                    {
                        "product_name": clean_product_name(scope_text),
                        "total_price": final,
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
    return min(results, key=lambda r: r["total_price"])


# ---------------------------------------------------------------------------
# 5. Embedded framework JSON (__NEXT_DATA__ / __NUXT__)
# ---------------------------------------------------------------------------

# Sites like GoPay Games are Next.js SPAs whose catalog sometimes never
# hydrates in headless browsers - but the full product data sits in
# <script id="__NEXT_DATA__"> as JSON. We walk that tree and derive units
# from product NAME strings (schema-agnostic), never from arbitrary keys.
NAME_KEYS = {"name", "title", "productname", "product_name", "label", "itemname", "skuname"}
PRICE_KEYS = {
    "price",
    "sellingprice",
    "selling_price",
    "finalprice",
    "final_price",
    "saleprice",
    "sale_price",
    "discountedprice",
    "discounted_price",
    "listprice",
    "payamount",
    "pay_amount",
    "amount",
}


def extract_from_embedded_json(html: str) -> list[dict]:
    """Extract packages from framework state JSON embedded in the page."""
    soup = BeautifulSoup(html or "", "html.parser")
    blobs: list[str] = []
    for tag in soup.select("script#__NEXT_DATA__, script#__NUXT_DATA__"):
        if tag.string:
            blobs.append(tag.string)
    for script in soup.find_all("script"):
        text = script.string or ""
        for marker in ("window.__NUXT__=", "window.__INITIAL_STATE__="):
            if marker in text:
                blobs.append(text.split(marker, 1)[1])

    packages: list[dict] = []
    seen: set[tuple] = set()
    for blob in blobs:
        try:
            data = json.loads(blob[:2_000_000])
        except Exception:
            continue
        for item in _walk_json_products(data):
            key = (item["base_units"], item["bonus_units"], float(item["total_price"]))
            if key not in seen:
                seen.add(key)
                packages.append(item)
    return packages


def _walk_json_products(node, depth: int = 0) -> list[dict]:
    """Recursively find dicts that have a name-ish string AND a price-ish
    number, then derive units from the name text."""
    found: list[dict] = []
    if depth > 14:
        return found
    if isinstance(node, dict):
        name = next(
            (
                v
                for k, v in node.items()
                if k.lower() in NAME_KEYS and isinstance(v, str) and v.strip()
            ),
            None,
        )
        price = next(
            (
                v
                for k, v in node.items()
                if k.lower() in PRICE_KEYS and isinstance(v, (int, float)) and not isinstance(v, bool)
            ),
            None,
        )
        if name is not None and price is not None:
            units = parse_units(name)
            is_pass = units is None and bool(PASS_RE.search(name))
            if units or is_pass:
                base, bonus = units if units else (0, 0)
                total = Decimal(str(price))
                if price_plausible(total, base, bonus):
                    found.append(
                        {
                            "product_name": clean_product_name(name) or f"package {base}+{bonus}",
                            "total_price": total,
                            "currency": "IDR",
                            "base_units": base,
                            "bonus_units": bonus,
                            "method": "embedded_json",
                            "confidence": 0.85,
                        }
                    )
        for value in node.values():
            found.extend(_walk_json_products(value, depth + 1))
    elif isinstance(node, list):
        for value in node[:1000]:
            found.extend(_walk_json_products(value, depth + 1))
    return found


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
