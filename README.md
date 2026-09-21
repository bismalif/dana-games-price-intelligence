# DANA Games Price Intelligence Engine

Automated price monitoring for DANA Games top-up SKUs against competitor
platforms (Codashop, UniPin, GoPay Games, Lapakgaming, GOGOGO, MobaPay, and
more). Zero-cost stack: GitHub Actions (scraper) + Supabase (database) +
Streamlit Community Cloud (dashboard) + Gemini (AI fallback parser) +
DingTalk (alerts).

## How it works

```
GitHub Actions (daily 10:00 WIB)
  Playwright loads each competitor page
    -> deterministic extraction (JSON-LD / meta / selectors / text)
    -> Gemini fallback ONLY if that fails (validated strictly)
  -> prices matched to DANA SKUs by game + effective units
  -> stored in Supabase (price_logs)
  -> undercut state evaluated (>=10% cheaper)
  -> DingTalk group alert on state change

Streamlit Cloud dashboard
  -> login (Supabase Auth), comparison table, trends, source health,
     unmatched products, admin config (no code changes needed)
```

Key formula: `effective unit price = total price / (base units + bonus units)`.
Comparison is zero-threshold (any difference is flagged); DingTalk alerts fire
only when a competitor is at least 10% cheaper AND that state is new.

## Setup (one time)

1. **Supabase**: create project -> SQL Editor -> run `supabase/schema.sql`,
   then `supabase/seed.sql`. Enable Auth -> Email provider.
2. **GitHub**: push this repo to GitHub (private). Add Actions secrets:
   `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `GEMINI_API_KEY`,
   `DINGTALK_WEBHOOK_URL`, `DINGTALK_SECRET`.
3. **Streamlit Cloud**: new app from this repo, main file `app.py`. Add secrets:
   `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY` (never the secret key).
4. **First run**: Actions -> Price Scraper -> Run workflow. The first run is a
   baseline (records alert state, does not page DingTalk).

Full details: see `PRICE_INTELLIGENCE_CONTEXT.md`.

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env   # fill in real values; .env is gitignored

python -m unittest discover -s tests -t .   # offline tests
python -m scraper.runner                    # full scrape (needs .env)
streamlit run app.py                        # dashboard (needs .streamlit/secrets.toml)
```

## Security rules

- The Supabase **secret** key (`sb_secret_...`) goes ONLY into GitHub Actions
  secrets. Never into Streamlit secrets, source code, or chat.
- `.env` and `.streamlit/secrets.toml` are gitignored - never commit them.
- Gemini receives only a reduced, sanitized DOM snapshot of public pages and
  its output is validated before storage; it never touches the database.
