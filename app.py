"""DANA Games Price Intelligence Dashboard (Streamlit).

Internal comparative pricing dashboard backed by Supabase.
Secrets required (Streamlit Cloud -> Settings -> Secrets, or .streamlit/secrets.toml):
    SUPABASE_URL
    SUPABASE_PUBLISHABLE_KEY

The Supabase SECRET key must NEVER be placed here - it is only for the scraper.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd
import streamlit as st
from supabase import Client, create_client

from scraper.pricing import compare_status, effective_unit_price, is_undercutting, undercut_pct

STALE_AFTER_HOURS = 30
UNDERCUT_THRESHOLD = 10  # percent

WIB = timezone(timedelta(hours=7))

st.set_page_config(page_title="DANA Price Intelligence", page_icon="🎮", layout="wide")


# ---------------------------------------------------------------------------
# Supabase + auth
# ---------------------------------------------------------------------------

@st.cache_resource
def get_client() -> Client:
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_PUBLISHABLE_KEY"])


def restore_session() -> None:
    if st.session_state.get("user") is not None:
        return
    try:
        session = get_client().auth.get_session()
        if session and session.user:
            st.session_state.user = session.user
    except Exception:
        st.session_state.user = None


def get_role(user) -> str:
    try:
        response = (
            get_client()
            .table("user_profiles")
            .select("role")
            .eq("id", user.id)
            .limit(1)
            .execute()
        )
        rows = response.data or []
        return rows[0].get("role", "viewer") if rows else "viewer"
    except Exception:
        return "viewer"


def render_login() -> None:
    st.title("🎮 DANA Price Intelligence")
    st.caption("Internal dashboard - sign in with your DANA team account.")
    with st.form("login"):
        email = st.text_input("Email")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in", use_container_width=True)
    if submitted:
        try:
            get_client().auth.sign_in_with_password({"email": email.strip(), "password": password})
            st.rerun()
        except Exception:
            st.error("Sign-in failed. Check your credentials and try again.")


# ---------------------------------------------------------------------------
# Data loading (cached 60s)
# ---------------------------------------------------------------------------

def _rows(query) -> list[dict]:
    try:
        return query.execute().data or []
    except Exception as exc:
        st.error(f"Database query failed: {exc}")
        return []


@st.cache_data(ttl=60, show_spinner="Loading pricing data...")
def load_data(user_id: str) -> dict:
    sb = get_client()
    games = _rows(sb.table("games").select("*").eq("active", True).order("name"))
    skus = _rows(sb.table("game_skus").select("*").order("effective_units"))
    sources = _rows(sb.table("sources").select("*").order("name"))
    mappings = _rows(sb.table("source_sku_mappings").select("*").order("id"))
    latest = _rows(sb.table("latest_price_logs").select("*"))
    alert_states = _rows(sb.table("undercut_alert_states").select("*"))
    runs = _rows(sb.table("scrape_runs").select("*").order("started_at", desc=True).limit(15))
    unmatched = _rows(
        sb.table("unmatched_products")
        .select("*")
        .order("captured_at", desc=True)
        .limit(100)
    )
    return {
        "games": games,
        "skus": [s for s in skus if s.get("active")],
        "sources": sources,
        "mappings": mappings,
        "latest": latest,
        "alert_states": alert_states,
        "runs": runs,
        "unmatched": unmatched,
    }


def fetch_history(user_id: str, sku_id: int) -> list[dict]:
    sb = get_client()
    return _rows(
        sb.table("price_logs")
        .select("captured_at, total_price, effective_unit_price, source_id, sources(name)")
        .eq("sku_id", sku_id)
        .eq("scrape_status", "success")
        .order("captured_at")
        .limit(1000)
    )


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def fmt_rp(value) -> str:
    if value is None:
        return "—"
    try:
        return f"Rp {float(value):,.0f}".replace(",", ".")
    except (TypeError, ValueError):
        return "—"


def fmt_eup(value) -> str:
    if value is None:
        return "—"
    try:
        return f"Rp {float(value):,.2f}".replace(",", "§").replace(".", ",").replace("§", ".")
    except (TypeError, ValueError):
        return "—"


def age_label(captured_at: str | None) -> str:
    if not captured_at:
        return "no data"
    try:
        captured = datetime.fromisoformat(str(captured_at).replace("Z", "+00:00"))
    except ValueError:
        return "?"
    hours = (datetime.now(timezone.utc) - captured).total_seconds() / 3600
    if hours >= STALE_AFTER_HOURS:
        return f"⚠️ {int(hours)}h (stale)"
    return f"{int(hours)}h ago"


STATUS_BADGE = {
    "dana_cheaper": "🟢 DANA Cheaper",
    "competitor_cheaper": "🔴 Competitor Cheaper",
    "price_match": "⚖️ Price Match",
    "unknown": "⚪ No Data",
}


# ---------------------------------------------------------------------------
# Comparison table
# ---------------------------------------------------------------------------

def build_comparison(data: dict, game_id: int) -> pd.DataFrame:
    dana_source_ids = {s["id"] for s in data["sources"] if s.get("source_type") == "dana"}
    source_by_id = {s["id"]: s for s in data["sources"]}
    sku_by_id = {s["id"]: s for s in data["skus"]}

    dana_latest: dict[int, dict] = {}
    competitor_latest: dict[tuple[int, int], dict] = {}
    for log in data["latest"]:
        if log.get("source_id") in dana_source_ids:
            dana_latest[log["sku_id"]] = log
        elif log.get("sku_id") is not None:
            competitor_latest[(log["sku_id"], log["source_id"])] = log

    rows = []
    for sku in [s for s in data["skus"] if s["game_id"] == game_id]:
        units = sku["effective_units"]
        units_label = f"{sku['base_units']} + {sku['bonus_units']} = {units}" if units > 0 else "Pass"
        dana_log = dana_latest.get(sku["id"])
        # Prefer the latest scraped DANA price log; fall back to the SKU's
        # stored price (manual entry via Admin tab, or last discovery run).
        dana_price = dana_log.get("total_price") if dana_log else sku.get("dana_current_price")
        if units > 0:
            dana_eup = effective_unit_price(dana_price, sku["base_units"], sku["bonus_units"])
        else:
            # Pass products: compare by final total price.
            dana_eup = Decimal(str(dana_price)) if dana_price is not None else None
        dana_age = age_label(dana_log.get("captured_at")) if dana_log else "stored price"

        if not any(key[0] == sku["id"] for key in competitor_latest):
            rows.append(
                {
                    "SKU": sku["display_name"],
                    "Units": units_label,
                    "DANA Price": fmt_rp(dana_price),
                    "DANA Eff./Unit": fmt_eup(dana_eup),
                    "DANA Freshness": dana_age,
                    "Competitor": "—",
                    "Comp. Price": "—",
                    "Comp. Eff./Unit": "—",
                    "Diff. %": "—",
                    "Status": STATUS_BADGE["unknown"],
                    "Flag": "No competitor data",
                }
            )
            continue

        for (sku_id, source_id), log in sorted(competitor_latest.items()):
            if sku_id != sku["id"]:
                continue
            comp_price = log.get("total_price")
            comp_base = log.get("base_units")
            if units > 0 and comp_base:
                comp_eup = effective_unit_price(comp_price, comp_base, log.get("bonus_units") or 0)
            elif units == 0:
                comp_eup = Decimal(str(comp_price)) if comp_price is not None else None
            else:
                comp_eup = None
            diff = (
                undercut_pct(dana_eup, comp_eup) if dana_eup and comp_eup else None
            )
            status = compare_status(dana_eup, comp_eup)
            undercut = is_undercutting(dana_eup, comp_eup) if dana_eup and comp_eup else False
            comp_fresh = age_label(log.get("captured_at"))
            if undercut:
                flag = "🚨 ≥10% undercut"
            elif log.get("parser_method") == "gemini_fallback":
                flag = "🤖 AI-parsed"
            else:
                flag = ""
            rows.append(
                {
                    "SKU": sku["display_name"],
                    "Units": units_label,
                    "DANA Price": fmt_rp(dana_price),
                    "DANA Eff./Unit": fmt_eup(dana_eup),
                    "DANA Freshness": dana_age,
                    "Competitor": source_by_id.get(source_id, {}).get("name", "?"),
                    "Comp. Price": fmt_rp(comp_price),
                    "Comp. Eff./Unit": fmt_eup(comp_eup),
                    "Diff. %": f"-{float(diff):.2f}%" if diff is not None else "—",
                    "Status": STATUS_BADGE[status],
                    "Flag": flag or f"⏱ {comp_fresh}",
                }
            )
    return pd.DataFrame(rows)


def render_comparison(data: dict) -> None:
    game_names = {g["name"]: g["id"] for g in data["games"]}
    if not game_names:
        st.info("No active games configured yet. Add a game in the Admin tab.")
        return
    selected_game = st.selectbox("Game", list(game_names.keys()))
    df = build_comparison(data, game_names[selected_game])

    if df.empty:
        st.info("No SKUs found for this game yet. Run the scraper to discover DANA SKUs.")
        return

    undercuts = df["Flag"].str.contains("undercut", na=False).sum()
    stale = df["Flag"].str.contains("stale", na=False).sum() + df["DANA Freshness"].str.contains("stale", na=False).sum()
    col1, col2, col3 = st.columns(3)
    col1.metric("Rows shown", len(df))
    col2.metric("Undercut flags (≥10%)", int(undercuts))
    col3.metric("Stale indicators", int(stale))

    styler = df.style.map(
        lambda v: (
            "background-color: #ffcccc; color: #8b0000; font-weight: 600"
            if "Competitor Cheaper" in str(v)
            else "background-color: #ccffcc; color: #006400"
            if "DANA Cheaper" in str(v)
            else "background-color: #fff3cd; color: #7a5c00"
            if "Price Match" in str(v)
            else ""
        ),
        subset=["Status"],
    ).map(
        lambda v: "background-color: #ffcccc; color: #8b0000; font-weight: 600"
        if "undercut" in str(v)
        else "color: #b45309"
        if "stale" in str(v) or "AI-parsed" in str(v)
        else "",
        subset=["Flag"],
    )
    st.dataframe(styler, use_container_width=True, hide_index=True)
    st.caption(
        f"Effective unit price = total price ÷ (base units + bonus units). "
        f"Zero-threshold comparison; 🚨 = at least {UNDERCUT_THRESHOLD}% cheaper (alert threshold). "
        f"Data older than {STALE_AFTER_HOURS}h is marked stale."
    )


# ---------------------------------------------------------------------------
# Trends
# ---------------------------------------------------------------------------

def render_trends(data: dict, user_id: str) -> None:
    import plotly.express as px

    if not data["skus"]:
        st.info("No SKUs yet - run the scraper first.")
        return
    sku_labels = {f"{s['display_name']}": s for s in data["skus"]}
    selected = st.selectbox("SKU", list(sku_labels.keys()))
    sku = sku_labels[selected]

    history = fetch_history(user_id, sku["id"])
    if not history:
        st.info("No price history for this SKU yet.")
        return

    dana_source_ids = {s["id"] for s in data["sources"] if s.get("source_type") == "dana"}
    rows = []
    for log in history:
        source_name = log["sources"]["name"] if isinstance(log.get("sources"), dict) else "?"
        rows.append(
            {
                "captured_at": pd.to_datetime(log["captured_at"]),
                "source": source_name,
                "kind": "DANA" if log.get("source_id") in dana_source_ids else "Competitor",
                "Effective price / unit": float(log["effective_unit_price"]) if log.get("effective_unit_price") else None,
            }
        )
    df = pd.DataFrame(rows).dropna(subset=["Effective price / unit"])
    fig = px.line(
        df,
        x="captured_at",
        y="Effective price / unit",
        color="source",
        line_dash="kind",
        markers=True,
        title=f"Effective price per unit - {sku['display_name']}",
        labels={"captured_at": "Captured (UTC)"},
    )
    st.plotly_chart(fig, use_container_width=True)

    st.subheader("Raw history")
    st.dataframe(
        df.sort_values("captured_at", ascending=False),
        use_container_width=True,
        hide_index=True,
    )


# ---------------------------------------------------------------------------
# Source health
# ---------------------------------------------------------------------------

def render_health(data: dict) -> None:
    game_by_id = {g["id"]: g["name"] for g in data["games"]}
    source_by_id = {s["id"]: s for s in data["sources"]}

    rows = []
    for mapping in data["mappings"]:
        source = source_by_id.get(mapping["source_id"], {})
        status = mapping.get("last_scrape_status")
        status_label = (
            "✅ success"
            if status == "success"
            else "❌ failed"
            if status == "failed"
            else "⚪ never scraped"
        )
        if mapping.get("requires_auth"):
            access = "🔒 requires auth"
        elif mapping.get("enabled"):
            access = "✅ enabled"
        else:
            access = "⛔ disabled"
        rows.append(
            {
                "Source": source.get("name", "?"),
                "Game": game_by_id.get(mapping["game_id"], "?"),
                "Access": access,
                "Last scrape": status_label,
                "When": age_label(mapping.get("last_scrape_at")),
                "URL": mapping.get("product_url", ""),
                "Error": (mapping.get("last_error") or "")[:120],
            }
        )
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    st.subheader("Recent scrape runs")
    if data["runs"]:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Started": r["started_at"],
                        "Finished": r.get("finished_at") or "—",
                        "Status": r.get("status"),
                        "Sources": f"{r.get('sources_success', 0)}/{r.get('sources_total', 0)} ok",
                        "Alerts": r.get("alerts_sent", 0),
                        "Notes": (r.get("notes") or "")[:200],
                    }
                    for r in data["runs"]
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.caption("No scrape runs recorded yet.")


# ---------------------------------------------------------------------------
# Unmatched products (admin)
# ---------------------------------------------------------------------------

def render_unmatched(data: dict, is_admin: bool) -> None:
    if not data["unmatched"]:
        st.success("No unmatched competitor products. 🎉")
        return
    df = pd.DataFrame(
        [
            {
                "Product": log.get("product_name") or "(unnamed)",
                "Price": fmt_rp(log.get("total_price")),
                "Units": f"{log.get('base_units')}+{log.get('bonus_units') or 0}",
                "Captured": age_label(log.get("captured_at")),
                "Source URL": log.get("source_url") or "",
            }
            for log in data["unmatched"]
        ]
    )
    st.dataframe(df, use_container_width=True, hide_index=True)

    if not is_admin:
        st.caption("Admins can map these products to SKUs without any code changes.")
        return

    st.subheader("Map an unmatched product to a DANA SKU")
    with st.form("map_unmatched"):
        options = {
            f"{(log.get('product_name') or '(unnamed)')[:60]} - {fmt_rp(log.get('total_price'))}": log
            for log in data["unmatched"]
        }
        chosen_log = st.selectbox("Unmatched product", list(options.keys()))
        log = options[chosen_log]
        sku_options = {
            f"{s['display_name']} ({s['effective_units']} units)": s
            for s in data["skus"]
            if s["game_id"] == log.get("game_id")
        }
        if not sku_options:
            st.warning("No SKUs for this game yet - run the scraper first.")
            st.form_submit_button("Map product", disabled=True)
            return
        chosen_sku = st.selectbox("DANA SKU", list(sku_options.keys()))
        if st.form_submit_button("Save mapping"):
            sku = sku_options[chosen_sku]
            try:
                get_client().table("source_sku_mappings").upsert(
                    {
                        "source_id": log["source_id"],
                        "game_id": log["game_id"],
                        "sku_id": sku["id"],
                        "product_url": log["source_url"],
                        "product_label": (log.get("product_name") or "")[:200],
                        "enabled": True,
                    },
                    on_conflict="source_id,game_id,product_url",
                ).execute()
                st.success("Mapping saved. Next scrape will match this product.")
                load_data.clear()
                st.rerun()
            except Exception as exc:
                st.error(f"Failed to save mapping: {exc}")


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

def render_admin(data: dict) -> None:
    sb = get_client()

    st.subheader("Competitor product URLs")
    with st.form("add_mapping"):
        source_options = {
            f"{s['name']}": s for s in data["sources"] if s.get("source_type") == "competitor"
        }
        game_options = {g["name"]: g for g in data["games"]}
        chosen_source = st.selectbox("Source", list(source_options.keys()))
        chosen_game = st.selectbox("Game", list(game_options.keys()))
        url = st.text_input("Product/catalog URL")
        label = st.text_input("Label (optional)")
        manual_sku = st.selectbox(
            "Manual SKU override (optional)",
            ["(automatic matching)"]
            + [f"{s['display_name']} ({s['effective_units']} units)" for s in data["skus"]],
        )
        price_sel = st.text_input("Price CSS selector (optional)")
        units_sel = st.text_input("Units CSS selector (optional)")
        if st.form_submit_button("Add mapping"):
            if not url.strip():
                st.error("URL is required.")
            else:
                sku_map = {f"{s['display_name']} ({s['effective_units']} units)": s for s in data["skus"]}
                sku_id = (
                    sku_map[manual_sku]["id"]
                    if manual_sku != "(automatic matching)" and manual_sku in sku_map
                    else None
                )
                try:
                    sb.table("source_sku_mappings").upsert(
                        {
                            "source_id": source_options[chosen_source]["id"],
                            "game_id": game_options[chosen_game]["id"],
                            "sku_id": sku_id,
                            "product_url": url.strip(),
                            "product_label": label.strip() or None,
                            "price_selector": price_sel.strip() or None,
                            "units_selector": units_sel.strip() or None,
                            "enabled": True,
                        },
                        on_conflict="source_id,game_id,product_url",
                    ).execute()
                    st.success("Mapping saved.")
                    load_data.clear()
                    st.rerun()
                except Exception as exc:
                    st.error(f"Failed: {exc}")

    with st.expander("Enable / disable mappings"):
        mapping_options = {
            f"{source_by_name(data, m['source_id'])} - {game_by_name(data, m['game_id'])}": m
            for m in data["mappings"]
        }
        with st.form("toggle_mapping"):
            chosen = st.selectbox("Mapping", list(mapping_options.keys()))
            mapping = mapping_options[chosen]
            new_enabled = st.checkbox("Enabled", value=mapping.get("enabled", False))
            new_auth = st.checkbox("Requires authentication", value=mapping.get("requires_auth", False))
            if st.form_submit_button("Update"):
                sb.table("source_sku_mappings").update(
                    {"enabled": new_enabled, "requires_auth": new_auth}
                ).eq("id", mapping["id"]).execute()
                st.success("Updated.")
                load_data.clear()
                st.rerun()

    st.subheader("Sources")
    with st.form("toggle_source"):
        source_rows = {f"{s['name']} ({s.get('source_type')})": s for s in data["sources"]}
        chosen_source = st.selectbox("Source", list(source_rows.keys()))
        source = source_rows[chosen_source]
        new_enabled = st.checkbox("Enabled", value=source.get("enabled", False))
        if st.form_submit_button("Update source"):
            sb.table("sources").update({"enabled": new_enabled}).eq("id", source["id"]).execute()
            st.success("Updated.")
            load_data.clear()
            st.rerun()

    st.subheader("Games")
    with st.form("add_game"):
        slug = st.text_input("Slug (e.g. pubg-mobile)")
        name = st.text_input("Display name")
        dana_url = st.text_input("DANA catalog URL (for SKU discovery)")
        if st.form_submit_button("Add game"):
            if slug.strip() and name.strip():
                try:
                    sb.table("games").upsert(
                        {
                            "slug": slug.strip().lower(),
                            "name": name.strip(),
                            "dana_url": dana_url.strip() or None,
                            "active": True,
                        },
                        on_conflict="slug",
                    ).execute()
                    st.success("Game saved.")
                    load_data.clear()
                    st.rerun()
                except Exception as exc:
                    st.error(f"Failed: {exc}")
            else:
                st.error("Slug and display name are required.")

    st.subheader("DANA SKU prices (manual override)")
    st.caption("Normally updated by the scraper. Use this when DANA scraping is unavailable.")
    with st.form("update_price"):
        sku_rows = {f"{s['display_name']} ({s['effective_units']} units)": s for s in data["skus"]}
        if not sku_rows:
            st.info("No SKUs yet.")
            st.form_submit_button("Save price", disabled=True)
            return
        chosen_sku = st.selectbox("SKU", list(sku_rows.keys()))
        sku = sku_rows[chosen_sku]
        price = st.number_input(
            "DANA price (IDR)",
            min_value=0.0,
            value=float(sku.get("dana_current_price") or 0),
            step=500.0,
        )
        if st.form_submit_button("Save price"):
            sb.table("game_skus").update({"dana_current_price": price}).eq("id", sku["id"]).execute()
            st.success("Price saved.")
            load_data.clear()
            st.rerun()


def source_by_name(data: dict, source_id: int) -> str:
    return next((s["name"] for s in data["sources"] if s["id"] == source_id), "?")


def game_by_name(data: dict, game_id: int) -> str:
    return next((g["name"] for g in data["games"] if g["id"] == game_id), "?")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    restore_session()
    user = st.session_state.get("user")
    if user is None:
        render_login()
        return

    role = get_role(user)
    is_admin = role == "admin"

    with st.sidebar:
        st.title("🎮 Price Intelligence")
        st.caption(f"{user.email} · {role}")
        if st.button("Sign out", use_container_width=True):
            try:
                get_client().auth.sign_out()
            except Exception:
                pass
            st.session_state.user = None
            load_data.clear()
            st.rerun()
        if st.button("🔄 Refresh data", use_container_width=True):
            load_data.clear()
            st.rerun()

    st.title("DANA Games - Price Intelligence Dashboard")
    data = load_data(user.id)

    tab_comparison, tab_trends, tab_health, tab_unmatched, tab_admin = st.tabs(
        [
            "💰 Price Comparison",
            "📈 Trends",
            "🩺 Source Health",
            "🧩 Unmatched Products",
            "⚙️ Admin",
        ]
    )

    with tab_comparison:
        render_comparison(data)
    with tab_trends:
        render_trends(data, user.id)
    with tab_health:
        render_health(data)
    with tab_unmatched:
        render_unmatched(data, is_admin)
    with tab_admin:
        if is_admin:
            render_admin(data)
        else:
            st.info("Admin access required to modify configuration.")


if __name__ == "__main__":
    main()
