"""Scraper orchestration: DANA SKU discovery -> competitor scraping ->
matching -> price logs -> undercut state -> DingTalk alerts.

Run locally:  python -m scraper.runner
Runs in CI:   .github/workflows/scrape.yml (daily 10:00 Asia/Jakarta)
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal

from dotenv import load_dotenv

from .ai_parser import parse_with_gemini
from .alerts import send_ops_alert, send_undercut_alert
from .browser import open_page
from .extractors import extract
from .matcher import CandidatePackage, match_package
from .pricing import effective_unit_price, is_undercutting, undercut_pct
from .store import Store

REQUEST_DELAY_SECONDS = float(os.environ.get("REQUEST_DELAY_SECONDS", "3.0"))
# First successful run establishes the baseline; alerts start afterwards.
ALLOW_BASELINE_ALERTS = os.environ.get("ALLOW_BASELINE_ALERTS", "0") == "1"


def log(message: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat()}] {message}", flush=True)


# ---------------------------------------------------------------------------
# DANA SKU discovery
# ---------------------------------------------------------------------------

def discover_dana_skus(store: Store) -> int:
    """Scrape DANA catalog pages: upsert SKUs AND write DANA price logs.

    DANA price logs are what the comparison/alert logic reads, so they must
    exist even when the SKU catalog is already populated.

    Returns the number of SKUs upserted.
    """
    games = store.get_games()
    dana_source = next(
        (s for s in store.get_sources() if s.get("source_type") == "dana"),
        None,
    )
    if dana_source is None:
        log("WARN: no DANA source configured; skipping discovery")
        return 0

    count = 0
    for game in games:
        dana_url = game.get("dana_url")
        if not dana_url:
            continue
        log(f"Discovering DANA SKUs for {game['name']}: {dana_url}")
        try:
            with open_page(dana_url) as html:
                result = extract(html)
                # The single-package extractor returns one card; catalog pages
                # list many - parse the full card grid from the same HTML.
                packages = extract_dana_packages(html) or ([result] if result else [])
        except Exception as exc:
            log(f"ERROR: DANA discovery failed for {game['name']}: {exc}")
            continue
        if not packages:
            log(f"WARN: no packages parsed from DANA page for {game['name']}")
            continue

        for package in packages:
            if not package.get("base_units"):
                continue  # cannot catalogue a package without unit counts
            sku_code = f"{game['slug']}-{package['base_units']}{'+' + str(package['bonus_units']) if package.get('bonus_units') else ''}"
            sku = store.upsert_game_sku(
                game["id"],
                {
                    "sku_code": sku_code,
                    "display_name": package.get("product_name") or sku_code,
                    "base_units": package["base_units"],
                    "bonus_units": package.get("bonus_units", 0),
                    "dana_current_price": float(package["total_price"]),
                },
            )
            count += 1
            log(f"Upserted SKU {sku_code}: IDR {package['total_price']}")

            # Record the DANA price log for this SKU (comparison baseline).
            if sku is not None:
                eup = effective_unit_price(
                    package["total_price"], package["base_units"], package.get("bonus_units", 0)
                )
                store.insert_price_log(
                    {
                        "sku_id": sku["id"],
                        "game_id": game["id"],
                        "source_id": dana_source["id"],
                        "product_name": package.get("product_name") or sku_code,
                        "total_price": float(package["total_price"]),
                        "currency": "IDR",
                        "base_units": package["base_units"],
                        "bonus_units": package.get("bonus_units", 0),
                        "effective_units": package["base_units"] + package.get("bonus_units", 0),
                        "effective_unit_price": float(eup) if eup is not None else None,
                        "matched": True,
                        "scrape_status": "success",
                        "parser_method": "dana_catalog",
                        "source_url": dana_url,
                    }
                )
        time.sleep(REQUEST_DELAY_SECONDS)
    return count


def extract_dana_packages(html: str) -> list[dict]:
    """Parse every package card from a DANA catalog page."""
    from bs4 import BeautifulSoup

    from .extractors import parse_idr_price, parse_units

    soup = BeautifulSoup(html, "html.parser")
    packages: list[dict] = []
    seen: set[tuple[int, int]] = set()
    for el in soup.find_all(string=lambda t: t and "rp" in t.lower()):
        scope = el.parent
        for _ in range(3):
            text = scope.get_text(" ", strip=True) if scope else ""
            units = parse_units(text)
            price = parse_idr_price(text)
            if units and price and (units[0], units[1]) not in seen:
                seen.add((units[0], units[1]))
                packages.append(
                    {
                        "product_name": text[:200],
                        "total_price": price,
                        "base_units": units[0],
                        "bonus_units": units[1],
                    }
                )
                break
            if scope is None or scope.parent is None:
                break
            scope = scope.parent
    return packages


# ---------------------------------------------------------------------------
# Competitor scraping
# ---------------------------------------------------------------------------

def scrape_mapping(store: Store, mapping: dict, skus: list[dict]) -> dict:
    """Scrape one competitor mapping. Returns a summary dict for the run log."""
    source = mapping["sources"]
    summary = {
        "source": source["name"],
        "game_id": mapping["game_id"],
        "status": "failed",
        "matched": False,
        "alerted": False,
    }

    if mapping.get("requires_auth"):
        summary["error"] = "skipped: requires authentication"
        store.update_mapping_status(mapping["id"], "failed", summary["error"])
        return summary

    url = mapping["product_url"]
    log(f"Scraping {source['name']} ({mapping.get('product_label') or ''}): {url}")
    try:
        with open_page(url) as html:
            result = extract(html, mapping.get("price_selector"), mapping.get("units_selector"))
            if result is None:
                log(f"Deterministic extraction failed; trying Gemini fallback for {url}")
                result = parse_with_gemini(html, url)
    except Exception as exc:
        summary["error"] = f"browser error: {exc}"
        store.update_mapping_status(mapping["id"], "failed", str(exc)[:500])
        insert_failure_log(store, mapping, summary["error"])
        return summary

    if result is None:
        summary["error"] = "extraction failed (deterministic + Gemini)"
        store.update_mapping_status(mapping["id"], "failed", summary["error"])
        insert_failure_log(store, mapping, summary["error"])
        return summary

    # A price without a unit count (e.g. JSON-LD price, generic product name)
    # is still recorded - as an unmatched product for manual mapping - but
    # never crashes the run. The scrape itself worked: status = success.
    if result.get("base_units") is None:
        summary["status"] = "success"
        summary["note"] = "price found but unit count missing (unmatched)"
        insert_success_log(store, mapping, result, matched=False, sku_id=None)
        store.update_mapping_status(mapping["id"], "success")
        log(f"{source['name']}: recorded unmatched price (no unit count)")
        return summary

    # Match to a canonical SKU.
    package = CandidatePackage(
        product_name=result.get("product_name") or "",
        base_units=result["base_units"],
        bonus_units=result.get("bonus_units", 0),
        total_price=float(result["total_price"]),
        product_url=url,
    )
    match = match_package(package, [dict_to_sku(s) for s in skus], mapping.get("sku_id"))
    if match.sku is None:
        # Scraping worked; matching failed. Recorded as an unmatched product
        # (dashboard > Unmatched Products) and counted as a healthy scrape.
        reason = "unmatched package (no SKU with equal effective units)"
        if match.ambiguous:
            reason = "ambiguous package (multiple SKUs match)"
        summary["status"] = "success"
        summary["note"] = reason
        insert_success_log(store, mapping, result, matched=False, sku_id=None)
        store.update_mapping_status(mapping["id"], "success")
        log(f"{source['name']}: {reason} - saved to Unmatched Products")
        return summary

    # Compute effective price and insert the matched log.
    eup = effective_unit_price(result["total_price"], result["base_units"], result.get("bonus_units", 0))
    insert_success_log(store, mapping, result, matched=True, sku_id=match.sku["id"], eup=eup)
    store.update_mapping_status(mapping["id"], "success")
    summary["status"] = "success"
    summary["matched"] = True
    summary["sku"] = match.sku
    summary["eup"] = eup
    log(f"{source['name']}: matched {match.sku.display_name} at {eup}/unit")
    return summary


def dict_to_sku(row: dict) -> object:
    from .matcher import SkuDefinition

    return SkuDefinition(
        id=row["id"],
        sku_code=row["sku_code"],
        display_name=row["display_name"],
        base_units=row["base_units"],
        bonus_units=row.get("bonus_units", 0),
        game_id=row["game_id"],
        dana_current_price=row.get("dana_current_price"),
    )


def insert_failure_log(store: Store, mapping: dict, error: str) -> None:
    store.insert_price_log(
        {
            "mapping_id": mapping["id"],
            "game_id": mapping["game_id"],
            "source_id": mapping["source_id"],
            "scrape_status": "failed",
            "error_message": error[:500],
            "source_url": mapping["product_url"],
            "currency": "IDR",
        }
    )


def insert_success_log(
    store: Store,
    mapping: dict,
    result: dict,
    matched: bool,
    sku_id: int | None,
    eup: Decimal | None = None,
) -> None:
    base_units = result.get("base_units")
    bonus_units = result.get("bonus_units", 0) or 0
    effective = base_units + bonus_units if base_units is not None else None
    store.insert_price_log(
        {
            "mapping_id": mapping["id"],
            "sku_id": sku_id,
            "game_id": mapping["game_id"],
            "source_id": mapping["source_id"],
            "product_name": result.get("product_name"),
            "total_price": float(result["total_price"]),
            "currency": "IDR",
            "base_units": base_units,
            "bonus_units": bonus_units,
            "effective_units": effective,
            "effective_unit_price": float(eup) if eup is not None else None,
            "matched": matched,
            "scrape_status": "success",
            "parser_method": result.get("method"),
            "parser_confidence": result.get("confidence"),
            "source_url": mapping["product_url"],
        }
    )


# ---------------------------------------------------------------------------
# Alert evaluation
# ---------------------------------------------------------------------------

def evaluate_alerts(store: Store, summaries: list[dict]) -> int:
    """Compare matched competitor prices against DANA's latest price.

    Alerts fire only on the transition not-undercutting -> undercutting
    (threshold 10%). The first baseline run records state without alerting
    unless ALLOW_BASELINE_ALERTS=1.
    """
    alerts_sent = 0

    for summary in summaries:
        if not summary.get("matched") or summary.get("sku") is None:
            continue
        sku = summary["sku"]
        game_id = sku.game_id

        # DANA baseline: prefer the latest scraped DANA price log; fall back
        # to the SKU's stored price (manual entry or last successful scrape).
        dana_log = store.get_latest_dana_log(sku.id)
        if dana_log is not None and dana_log.get("effective_unit_price") is not None:
            dana_eup = Decimal(str(dana_log["effective_unit_price"]))
        elif sku.dana_current_price:
            dana_eup = effective_unit_price(
                sku.dana_current_price, sku.base_units, sku.bonus_units
            )
        else:
            continue
        comp_eup = summary.get("eup")
        if comp_eup is None:
            continue

        undercutting = is_undercutting(dana_eup, comp_eup)
        pct = undercut_pct(dana_eup, comp_eup)
        previous = store.get_alert_state(sku.id, summary_source_id(summary))

        was_undercutting = bool(previous and previous.get("is_undercutting"))
        store.set_alert_state(sku.id, summary_source_id(summary), undercutting, pct)

        should_alert = undercutting and not was_undercutting
        if undercutting and previous is None and not ALLOW_BASELINE_ALERTS:
            should_alert = False  # baseline run: record state only
        if should_alert:
            game_name = next(
                (g["name"] for g in store.get_games() if g["id"] == game_id),
                f"game {game_id}",
            )
            response = send_undercut_alert(
                game_name=game_name,
                sku_name=sku.display_name,
                source_name=summary["source"],
                dana_effective=dana_eup,
                competitor_effective=comp_eup,
                pct_cheaper=pct,
                product_url=summary.get("product_url"),
            )
            if response is not None:
                store.mark_alerted(sku.id, summary_source_id(summary))
                alerts_sent += 1
                log(f"ALERT sent: {sku.display_name} undercut by {summary['source']} ({pct}%)")
    return alerts_sent


def summary_source_id(summary: dict) -> int:
    # scrape_mapping stores source info under 'sources' from the joined row.
    return summary["_source_id"]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(skip_dana: bool = False, dana_only: bool = False) -> int:
    load_dotenv()
    store = Store()
    run_row = store.start_run()
    run_id = run_row["id"] if run_row else None
    log(f"Scrape run {run_id} started")

    # scraped = healthy scrape (matched or unmatched); matched/unmatched are
    # tracked separately so a blocked DANA catalog never reads as "all failed".
    totals = {"total": 0, "scraped": 0, "matched": 0, "unmatched": 0, "failed": 0, "alerts": 0}
    notes_parts: list[str] = []
    sku_count = 0

    try:
        if not skip_dana:
            sku_count = discover_dana_skus(store)
            notes_parts.append(f"skus_upserted={sku_count}")
            if sku_count == 0:
                notes_parts.append("dana_discovery=blocked_or_empty")
                log(
                    "NOTE: DANA discovery found no SKUs (likely blocked from this network). "
                    "Fix options: (1) run 'python -m scraper.runner --dana-only' from a "
                    "network that can reach dana.id, or (2) add SKUs manually in the "
                    "dashboard Admin tab."
                )

        mappings = [] if dana_only else store.get_enabled_mappings()
        totals["total"] = len(mappings)
        log(f"Scraping {len(mappings)} enabled mappings")

        summaries = []
        for mapping in mappings:
            game_skus = store.get_skus(mapping["game_id"])
            summary = scrape_mapping(store, mapping, game_skus)
            summary["_source_id"] = mapping["source_id"]
            summary["product_url"] = mapping["product_url"]
            summaries.append(summary)
            if summary["status"] == "success":
                totals["scraped"] += 1
                if summary.get("matched"):
                    totals["matched"] += 1
                else:
                    totals["unmatched"] += 1
            else:
                totals["failed"] += 1
                notes_parts.append(f"fail:{summary['source']}={summary.get('error', '')[:80]}")
            time.sleep(REQUEST_DELAY_SECONDS)

        totals["alerts"] = evaluate_alerts(store, summaries)

        if totals["scraped"] == 0 and totals["failed"] > 0:
            status = "failed"
        elif totals["failed"] > 0:
            status = "partial"
        else:
            status = "success"
        if run_id is not None:
            store.finish_run(run_id, status, totals, "; ".join(notes_parts) or None)
        log(f"Run {run_id} finished: {status} {totals}")
        return 0 if status in ("success", "partial") else 1

    except Exception as exc:
        log(f"FATAL: run {run_id} failed: {exc}")
        if run_id is not None:
            store.finish_run(run_id, "failed", totals, str(exc)[:500])
        send_ops_alert("Price Scraper Failed", f"Run {run_id} failed: {exc}")
        return 2


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="DANA price scraper")
    parser.add_argument(
        "--dana-only",
        action="store_true",
        help="Only discover DANA SKUs/prices. Run this from a network that can reach dana.id.",
    )
    parser.add_argument(
        "--competitors-only",
        action="store_true",
        help="Skip DANA discovery (e.g. when CI is IP-blocked by dana.id).",
    )
    args = parser.parse_args()
    return run(skip_dana=args.competitors_only, dana_only=args.dana_only)


if __name__ == "__main__":
    sys.exit(main())
