"""Exact pricing math using Decimal. Shared by scraper and dashboard."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

TWO_PLACES = Decimal("0.01")
FOUR_PLACES = Decimal("0.0001")


def effective_units(base_units: int, bonus_units: int) -> int:
    """Total units a buyer receives: base + bonus."""
    return int(base_units) + int(bonus_units)


def effective_unit_price(total_price, base_units: int, bonus_units: int) -> Decimal | None:
    """total_price / (base_units + bonus_units), quantized to 4 dp.

    Returns None when units are missing/zero or price is missing/negative -
    never returns zero for missing data.
    """
    if total_price is None or base_units is None:
        return None
    units = effective_units(base_units, bonus_units or 0)
    if units <= 0:
        return None
    price = Decimal(str(total_price))
    if price < 0:
        return None
    return (price / Decimal(units)).quantize(FOUR_PLACES, rounding=ROUND_HALF_UP)


def compare_status(dana_price, competitor_price) -> str:
    """Zero-threshold comparison badge.

    Returns one of: 'dana_cheaper', 'competitor_cheaper', 'price_match',
    or 'unknown' when either side is missing.
    """
    if dana_price is None or competitor_price is None:
        return "unknown"
    dana = Decimal(str(dana_price))
    comp = Decimal(str(competitor_price))
    if comp < dana:
        return "competitor_cheaper"
    if comp > dana:
        return "dana_cheaper"
    return "price_match"


def undercut_pct(dana_price, competitor_price) -> Decimal | None:
    """How much cheaper the competitor is, as a percentage (positive number).

    Returns None when not undercutting or data is missing.
    """
    if dana_price is None or competitor_price is None:
        return None
    dana = Decimal(str(dana_price))
    comp = Decimal(str(competitor_price))
    if dana <= 0 or comp >= dana:
        return None
    return ((dana - comp) / dana * Decimal(100)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def is_undercutting(dana_price, competitor_price, threshold_pct: Decimal = Decimal("10")) -> bool:
    """Alert rule: competitor effective price <= dana * (1 - threshold/100)."""
    pct = undercut_pct(dana_price, competitor_price)
    if pct is None:
        return False
    return pct >= threshold_pct
