# Price Intelligence Engine - System Context

This document explains how the DANA Games Price Intelligence Engine works,
why each decision was made, and how to operate and maintain it. It is the
reference for anyone (human or AI agent) modifying this system later.

> Security note: this file must NEVER contain real credentials. Secret names
> and storage locations are documented; values live only in GitHub Actions
> secrets, Streamlit secrets, or a local gitignored `.env`.

---

## 1. Purpose and scope

Track competitor top-up prices for DANA Games SKUs and surface every price
difference instantly, so pricing decisions can be made the same day.

- **Games (v1):** Mobile Legends: Bang Bang, Free Fire
- **Currency:** IDR only
- **Price basis:** public listed price (`public_listed_price`) - no
  payment-method, first-purchase, coupon, or member pricing
- **Comparison:** zero-threshold - ANY difference is flagged on the dashboard
- **Alerting:** DingTalk, only when a competitor is >= 10% cheaper and that
  state is NEW (transition-based, not repeated)

## 2. Architecture

```
+---------------------------+        +---------------------------+
| GitHub Actions (cron)     |        | Streamlit Community Cloud |
| .github/workflows/        |        | app.py                    |
| scrape.yml                |        |                           |
|  - Playwright (Chromium)  |        |  - Supabase Auth login    |
|  - deterministic parsers  |  read/ |  - comparison table       |
|  - Gemini fallback        | write  |  - trends (plotly)        |
|  - DingTalk alerts        +------->|  - source health          |
+---------------------------+        |  - admin config UI        |
                                     +---------------------------+
                  +--------------------------------------+
                  | Supabase (PostgreSQL, free tier)     |
                  | games, game_skus, sources,           |
                  | source_sku_mappings, price_logs,     |
                  | undercut_alert_states, scrape_runs,  |
                  | user_profiles                        |
                  +--------------------------------------+
```

### Roles of the two AI components

- **Gemini API (runtime):** self-healing DOM parser inside the GitHub Actions
  scraper. Used ONLY when deterministic extraction fails. Returns strict JSON;
  Python validates every field before storage. It never writes to the
  database, never executes code, and never bypasses login/CAPTCHA.
- **OpenCode (development):** the coding agent used to build, debug, and
  maintain this repository. Not part of the production runtime.

## 3. Data model (supabase/schema.sql)

| Table | Purpose | Key columns |
|---|---|---|
| `games` | monitored titles | `slug`, `name`, `dana_url` (DANA catalog page used for SKU discovery) |
| `game_skus` | canonical DANA package catalog | `base_units`, `bonus_units`, `effective_units` (generated = base+bonus), `dana_current_price` |
| `sources` | DANA + competitor platforms | `source_type` (`dana`/`competitor`), `enabled` |
| `source_sku_mappings` | one competitor product page per (source, game) | `product_url`, optional `price_selector`/`units_selector`, optional `sku_id` manual override, `requires_auth`, last scrape status |
| `price_logs` | every scrape attempt, success AND failure | `total_price`, `effective_unit_price`, `matched`, `scrape_status`, `parser_method`, `parser_confidence` |
| `undercut_alert_states` | alert dedup | `is_undercutting`, `undercut_pct`, `last_alerted_at`, unique (sku, source) |
| `scrape_runs` | workflow health | status, source totals, alerts sent |
| `user_profiles` | dashboard roles | `role` (`admin`/`viewer`); FIRST signup becomes admin automatically |

Helper views: `latest_price_logs` (latest row per mapping),
`latest_dana_prices` (latest successful DANA price per SKU),
`unmatched_products` (successful competitor scrapes with no SKU match).

### Row Level Security

- `anon`: no access to anything.
- `authenticated`: read everything; writes on config tables only for admins
  (enforced via `public.is_admin()` security-definer function).
- Scraper uses the Supabase **secret key** (`service_role`) server-side, which
  bypasses RLS by design - hence it must never leave GitHub Actions / `.env`.

### Auth trigger

`handle_new_user()` fires on `auth.users` insert: the first user to sign up
becomes `admin`, everyone after becomes `viewer`. Promote someone later with:
`update user_profiles set role='admin' where email='...'`.

## 4. Pricing math (scraper/pricing.py)

```
effective_units    = base_units + bonus_units
effective_unit_price = total_price / effective_units        (Decimal, 4dp)
status             = competitor < dana  -> competitor_cheaper
                     competitor > dana  -> dana_cheaper
                     equal              -> price_match
undercut_pct       = (dana - competitor) / dana * 100
is_undercutting    = undercut_pct >= 10
```

- All math uses `Decimal` (exact), never floats.
- Missing data returns `None`, never zero - failed scrapes are stored as
  `scrape_status='failed'` rows so gaps are visible, not disguised as cheap.

## 5. Matching (scraper/matcher.py)

Competitor packages are matched to DANA SKUs automatically:

1. Manual override wins if `source_sku_mappings.sku_id` is set.
2. Otherwise: same game AND equal effective units -> match.
3. Multiple SKUs with the same effective units -> ambiguous, left unmatched
   for manual mapping (visible in the dashboard's Unmatched Products tab).

## 6. Extraction pipeline (scraper/extractors.py, ai_parser.py)

Deterministic chain, cheapest/most-reliable first:

1. JSON-LD (`application/ld+json`, schema.org Product/Offer)
2. Meta tags (`og:price:amount`, `product:price:amount`, ...)
3. Configured CSS selectors from `source_sku_mappings` (optional)
4. Text heuristics (IDR regex + unit regex over visible elements)

If all fail -> **Gemini fallback**:

- Input: reduced DOM snapshot (scripts/styles stripped, 20k char cap).
- Output contract: strict JSON (`product_name`, `total_price`, `currency`,
  `base_units`, `bonus_units`, `confidence`) or `{"error": "not_found"}`.
- Validation: IDR only, sane price/units bounds, confidence clamped 0-1.
- Stored with `parser_method='gemini_fallback'` so AI-parsed rows are
  identifiable on the dashboard.

## 7. Alerting (scraper/alerts.py)

DingTalk custom group robot, signature security mode:

```
string_to_sign = f"{timestamp_ms}\n{secret}"
sign = urlsafe(base64(HmacSHA256(string_to_sign, key=secret)))
POST {webhook}&timestamp={ts}&sign={urlencode(sign)}
body: {"msgtype": "markdown", "markdown": {"title", "text"}, "at": {"isAtAll": false}}
```

Alert policy (`scraper/runner.py::evaluate_alerts`):

- Compare each matched competitor effective price vs DANA's latest effective
  price for the same SKU.
- Persist state in `undercut_alert_states`.
- Alert ONLY on transition `not undercutting -> undercutting` (>= 10%).
- The first run is a baseline: state is recorded, no alert
  (override with `ALLOW_BASELINE_ALERTS=1`).
- Ops alerts (separate) fire when the whole workflow fails.

## 8. Schedule and staleness

- Cron `0 3 * * *` UTC = **10:00 Asia/Jakarta** daily, plus manual
  `workflow_dispatch`.
- Data older than **30 hours** is flagged stale on the dashboard (one missed
  daily run tolerable; two = stale).
- Per-site politeness delay: `REQUEST_DELAY_SECONDS` (default 3s).

## 9. Secrets registry

| Secret | Used by | Stored in |
|---|---|---|
| `SUPABASE_URL` | scraper + dashboard | GitHub secrets, Streamlit secrets, `.env` |
| `SUPABASE_SECRET_KEY` | scraper ONLY | GitHub secrets, `.env` - NEVER Streamlit/code |
| `SUPABASE_PUBLISHABLE_KEY` | dashboard ONLY | Streamlit secrets |
| `GEMINI_API_KEY` | scraper fallback | GitHub secrets, `.env` |
| `GEMINI_MODEL` | scraper fallback (optional) | GitHub variables (default `gemini-2.0-flash`) |
| `DINGTALK_WEBHOOK_URL` | scraper alerts | GitHub secrets, `.env` |
| `DINGTALK_SECRET` | scraper alerts | GitHub secrets, `.env` |

If any secret is exposed: rotate it at the provider, update the secret store,
and deactivate the old value. Never paste secrets into chat, issues, or this
file.

## 10. Deployment (one-time setup)

1. **Supabase**: create project (Singapore region) -> SQL Editor ->
   run `supabase/schema.sql` then `supabase/seed.sql` -> Authentication ->
   enable Email provider -> create your user (first signup = admin).
2. **GitHub**: push repo (private) -> Settings > Secrets and variables >
   Actions -> add the secrets above.
3. **Streamlit Cloud**: share.streamlit.io -> New app -> repo, branch `main`,
   main file `app.py` -> Settings > Secrets -> `SUPABASE_URL` +
   `SUPABASE_PUBLISHABLE_KEY`.
4. **First scrape**: GitHub Actions -> Price Scraper -> Run workflow ->
   verify logs and `price_logs` rows in Supabase.
5. **DingTalk**: group -> Group Settings > Smart Group Assistant > Custom
   Robot -> security = signature -> store webhook + `SEC...` secret in GitHub
   secrets.

## 11. Operations playbook

| Symptom | Where to look | Fix |
|---|---|---|
| A source always fails | Dashboard > Source Health (`last_error`), Actions logs | Add/adjust CSS selectors via Admin tab; if the site changed layout, update selectors - no code change needed |
| Many `gemini_fallback` rows | `price_logs.parser_method` | Deterministic parsing broke for that site; add selectors to reduce AI usage |
| Unmatched products | Dashboard > Unmatched Products | Map them to SKUs in the UI (admin) |
| No alerts but undercuts visible | `undercut_alert_states` | Expected if state was already `undercutting`, or first-run baseline; check `last_alerted_at` |
| All data stale | `scrape_runs` | Check the last Actions run; workflow failure sends an ops alert to DingTalk |
| Dashboard shows nothing | Supabase Auth + RLS | Confirm user exists and email provider enabled; confirm publishable key in Streamlit secrets |

Manual commands:

```bash
python -m unittest discover -s tests -t .    # offline tests (no network)
python -m scraper.runner                     # one full scrape
```

## 12. Known limitations / risks

- Some competitors (SEAGM, PlayAsia, KiosGamer) are seeded DISABLED with
  `requires_auth=true` until authorized access is confirmed. Do not scrape
  login-gated pages without permission.
- Midasbuy is seeded disabled (no MLBB/FF catalog); enable when PUBG Mobile
  or another supported title is added.
- GitHub Actions runners share IP ranges; a site may block them while working
  locally. Failures are recorded per source, not silently dropped.
- Gemini free-tier quota may change; the system degrades gracefully (failed
  extraction) rather than storing bad data.
- DANA page structure may change; SKU discovery logs failures per game and
  the dashboard allows manual DANA price overrides (Admin tab).

## 13. File map

```
app.py                     Streamlit dashboard (entry point)
scraper/
  runner.py                orchestration + alert evaluation
  browser.py               Playwright page loading
  extractors.py            deterministic parsing chain
  ai_parser.py             Gemini fallback + validation
  matcher.py               game + effective-units matching
  pricing.py               Decimal pricing math (shared)
  alerts.py                DingTalk signed webhooks
  store.py                 Supabase persistence (secret key)
supabase/
  schema.sql               tables, views, RLS, triggers (run first)
  seed.sql                 games, sources, mappings (run second)
tests/                     offline unit tests (pricing, matching, alerts)
.github/workflows/scrape.yml  daily 10:00 WIB scraper + tests
```
