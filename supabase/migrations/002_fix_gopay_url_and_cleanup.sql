-- ============================================================================
-- Migration 002: fix GoPay MLBB URL + clean up garbage rows from old parser
--
-- 1. GoPay's 'mobile-legends' slug returns HTTP 500; the real URL is
--    'mobile-legends-bang-bang'.
-- 2. Earlier parser versions stored mispaired text as matched prices
--    (e.g. 5 Diamonds at IDR 36.036/unit). Delete implausible rows.
-- 3. Reset the undercut alert baseline - polluted matches created false
--    "undercutting" states. Next scrape re-baselines silently.
--
-- Run in Supabase SQL Editor. Safe to re-run.
-- ============================================================================

update public.source_sku_mappings
set product_url = 'https://gopay.co.id/games/mobile-legends-bang-bang',
    updated_at = now()
where product_url = 'https://gopay.co.id/games/mobile-legends';

delete from public.price_logs
where scrape_status = 'success'
  and base_units > 0
  and (effective_unit_price < 100 or effective_unit_price > 25000);

delete from public.undercut_alert_states;
