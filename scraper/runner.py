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
    """Scrape DANA catalog pages and upsert the canonical SKU catalog.

    Returns the number of SKUs upserted.
    """
    games = store.get_games()
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
            sku_code = f"{game['slug']}-{package['base_units']}{'+' + str(package['bonus_units']) if package.get('bonus_units') else ''}"
            store.upsert_game_sku(
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
        summary["error"] = "unmatched package (no SKU with equal effective units)"
        if match.ambiguous:
            summary["error"] = "ambiguous package (multiple SKUs match)"
        insert_success_log(store, mapping, result, matched=False, sku_id=None)
        store.update_mapping_status(mapping["id"], "success")
        return summary

    # Compute effective price and insert the matched log.
    eup = effective_unit_price(result["total_price"], result["base_units"], result.get("bonus_units", 0))
    insert_success_log(store, mapping, result, matched=True, sku_id=match.sku["id"], eup=eup)
    store.update_mapping_status(mapping["id"], "success")
    summary["status"] = "success"
    summary["matched"] = True
    summary["sku"] = match.sku
    summary["eup"] = eup
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
    store.insert_price_log(
        {
            "mapping_id": mapping["id"],
            "sku_id": sku_id,
            "game_id": mapping["game_id"],
            "source_id": mapping["source_id"],
            "product_name": result.get("product_name"),
            "total_price": float(result["total_price"]),
            "currency": "IDR",
            "base_units": result["base_units"],
            "bonus_units": result.get("bonus_units", 0),
            "effective_units": result["base_units"] + result.get("bonus_units", 0),
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

        dana_log = store.get_latest_dana_log(sku.id)
        if dana_log is None or dana_log.get("effective_unit_price") is None:
            continue
        dana_eup = Decimal(str(dana_log["effective_unit_price"]))
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

def run() -> int:
    load_dotenv()
    store = Store()
    run_row = store.start_run()
    run_id = run_row["id"] if run_row else None
    log(f"Scrape run {run_id} started")

    totals = {"total": 0, "success": 0, "failed": 0, "alerts": 0}
    notes_parts: list[str] = []

    try:
        sku_count = discover_dana_skus(store)
        notes_parts.append(f"skus_upserted={sku_count}")

        mappings = store.get_enabled_mappings()
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
                totals["success"] += 1
            else:
                totals["failed"] += 1
                notes_parts.append(f"fail:{summary['source']}={summary.get('error', '')[:80]}")
            time.sleep(REQUEST_DELAY_SECONDS)

        totals["alerts"] = evaluate_alerts(store, summaries)

        status = "success" if totals["failed"] == 0 else ("partial" if totals["success"] > 0 else "failed")
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


if __name__ == "__main__":
    sys.exit(run())
