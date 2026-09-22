"""Supabase persistence layer for the scraper (server-side, secret key).

All reads/writes the scraper needs live here so the rest of the package stays
storage-agnostic. Uses the Supabase secret key - server-side only, never in
the Streamlit dashboard.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from supabase import create_client

STALE_AFTER_HOURS = 30


class Store:
    def __init__(self, url: str | None = None, secret_key: str | None = None):
        url = url or os.environ["SUPABASE_URL"]
        key = secret_key or os.environ["SUPABASE_SECRET_KEY"]
        self.client = create_client(url, key)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_games(self) -> list[dict]:
        return self.client.table("games").select("*").eq("active", True).execute().data or []

    def get_sources(self) -> list[dict]:
        return self.client.table("sources").select("*").execute().data or []

    def get_skus(self, game_id: int) -> list[dict]:
        return (
            self.client.table("game_skus")
            .select("*")
            .eq("game_id", game_id)
            .eq("active", True)
            .execute()
            .data
            or []
        )

    def delete_broken_named_skus(self, game_id: int) -> int:
        """Remove SKUs whose display name still contains raw price text
        (e.g. '5 Diamonds Rp1.150 Rp1.000') - artifacts of an older parser
        version. Their price logs are kept (sku_id is set to null)."""
        response = (
            self.client.table("game_skus")
            .delete()
            .eq("game_id", game_id)
            .filter("display_name", "match", "Rp[0-9]")
            .select("id")
            .execute()
        )
        return len(response.data or [])

    def prune_miscomposed_skus(self, game_id: int) -> int:
        """Remove stale SKUs created by older parser versions:

        - unit SKUs whose stored composition contradicts their own display
          name (e.g. name says '14 Diamonds (13 + 1 Bonus)' = 13+1 but the
          row stores 14+1);
        - pass SKUs whose display name still contains promo ribbon text
          (e.g. 'Weekly Diamond Pass SATSET MURAH') - they would steal
          matches from their clean counterpart.

        Manually maintained SKUs whose names carry no unit info are kept.
        """
        from .extractors import clean_product_name, parse_units

        skus = self.get_skus(game_id)
        to_delete: list[int] = []
        for sku in skus:
            name = sku.get("display_name") or ""
            if sku.get("base_units", 0) > 0:
                parsed = parse_units(name)
                if parsed and tuple(parsed) != (sku["base_units"], sku["bonus_units"]):
                    to_delete.append(sku["id"])
            elif clean_product_name(name) != name:
                to_delete.append(sku["id"])
        if not to_delete:
            return 0
        response = (
            self.client.table("game_skus")
            .delete()
            .in_("id", to_delete)
            .select("id")
            .execute()
        )
        return len(response.data or [])

    def get_enabled_mappings(self) -> list[dict]:
        """Enabled mappings belonging to enabled sources."""
        response = (
            self.client.table("source_sku_mappings")
            .select("*, sources!inner(enabled, source_type, name, slug)")
            .eq("enabled", True)
            .eq("sources.enabled", True)
            .execute()
        )
        return response.data or []

    def get_latest_dana_log(self, sku_id: int) -> dict | None:
        response = (
            self.client.table("price_logs")
            .select("*, sources!inner(source_type)")
            .eq("sku_id", sku_id)
            .eq("sources.source_type", "dana")
            .eq("scrape_status", "success")
            .order("captured_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = response.data or []
        return rows[0] if rows else None

    def get_alert_state(self, sku_id: int, source_id: int) -> dict | None:
        response = (
            self.client.table("undercut_alert_states")
            .select("*")
            .eq("sku_id", sku_id)
            .eq("source_id", source_id)
            .limit(1)
            .execute()
        )
        rows = response.data or []
        return rows[0] if rows else None

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def upsert_game_sku(self, game_id: int, sku: dict) -> dict:
        """Insert or update a canonical DANA SKU discovered by the scraper."""
        payload = {
            "game_id": game_id,
            "sku_code": sku["sku_code"],
            "display_name": sku["display_name"],
            "base_units": sku["base_units"],
            "bonus_units": sku.get("bonus_units", 0),
            "dana_current_price": sku.get("dana_current_price"),
        }
        response = (
            self.client.table("game_skus")
            .upsert(payload, on_conflict="game_id,sku_code")
            .execute()
        )
        return (response.data or [None])[0]

    def insert_price_log(self, log: dict) -> dict | None:
        response = self.client.table("price_logs").insert(log).execute()
        return (response.data or [None])[0]

    def update_mapping_status(self, mapping_id: int, status: str, error: str | None = None) -> None:
        self.client.table("source_sku_mappings").update(
            {
                "last_scrape_status": status,
                "last_scrape_at": datetime.now(timezone.utc).isoformat(),
                "last_error": error,
            }
        ).eq("id", mapping_id).execute()

    def set_alert_state(self, sku_id: int, source_id: int, is_undercutting: bool, undercut_pct) -> None:
        payload = {
            "sku_id": sku_id,
            "source_id": source_id,
            "is_undercutting": is_undercutting,
            "undercut_pct": float(undercut_pct) if undercut_pct is not None else None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.client.table("undercut_alert_states").upsert(
            payload, on_conflict="sku_id,source_id"
        ).execute()

    def mark_alerted(self, sku_id: int, source_id: int) -> None:
        self.client.table("undercut_alert_states").update(
            {"last_alerted_at": datetime.now(timezone.utc).isoformat()}
        ).eq("sku_id", sku_id).eq("source_id", source_id).execute()

    def start_run(self) -> dict:
        response = self.client.table("scrape_runs").insert({"status": "running"}).execute()
        return (response.data or [None])[0]

    def finish_run(self, run_id: int, status: str, totals: dict, notes: str | None = None) -> None:
        self.client.table("scrape_runs").update(
            {
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "status": status,
                "sources_total": totals.get("total", 0),
                "sources_success": totals.get("scraped", 0),
                "sources_failed": totals.get("failed", 0),
                "alerts_sent": totals.get("alerts", 0),
                "notes": notes,
            }
        ).eq("id", run_id).execute()

    # ------------------------------------------------------------------
    # Staleness
    # ------------------------------------------------------------------

    def is_stale(self, captured_at: str | None) -> bool:
        if not captured_at:
            return True
        try:
            captured = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        except ValueError:
            return True
        age = datetime.now(timezone.utc) - captured
        return age > timedelta(hours=STALE_AFTER_HOURS)
