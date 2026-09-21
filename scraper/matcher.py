"""Automatic SKU matching: game + effective units.

A competitor package matches a DANA SKU when both belong to the same game and
the effective unit totals are equal (base + bonus). Manual overrides in
source_sku_mappings.sku_id always win over automatic matching.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CandidatePackage:
    """A package parsed from any source (DANA or competitor)."""

    product_name: str
    base_units: int
    bonus_units: int = 0
    total_price: float | None = None
    currency: str = "IDR"
    product_url: str | None = None

    @property
    def effective_units(self) -> int:
        return self.base_units + self.bonus_units


@dataclass
class SkuDefinition:
    """A canonical DANA SKU."""

    id: int | None
    sku_code: str
    display_name: str
    base_units: int
    bonus_units: int = 0
    game_id: int | None = None
    dana_current_price: float | None = None

    @property
    def effective_units(self) -> int:
        return self.base_units + self.bonus_units


@dataclass
class MatchResult:
    sku: SkuDefinition | None
    candidates: list[SkuDefinition] = field(default_factory=list)
    ambiguous: bool = False


def match_package(
    package: CandidatePackage,
    skus: list[SkuDefinition],
    manual_sku_id: int | None = None,
) -> MatchResult:
    """Match a competitor package to a canonical SKU.

    Priority: manual override > exact effective-unit match > no match.
    """
    if manual_sku_id is not None:
        for sku in skus:
            if sku.id == manual_sku_id:
                return MatchResult(sku=sku, candidates=[sku])
        return MatchResult(sku=None, candidates=[])

    candidates = [s for s in skus if s.effective_units == package.effective_units]
    if len(candidates) == 1:
        return MatchResult(sku=candidates[0], candidates=candidates)
    if len(candidates) > 1:
        # Ambiguous: same effective units, multiple SKUs (e.g. different
        # bonus splits). Leave unmatched for manual mapping.
        return MatchResult(sku=None, candidates=candidates, ambiguous=True)
    return MatchResult(sku=None, candidates=[])
