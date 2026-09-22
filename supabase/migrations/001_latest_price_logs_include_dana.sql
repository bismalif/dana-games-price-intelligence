-- ============================================================================
-- Migration 001: include DANA price logs in latest_price_logs
--
-- Problem: the original view keyed on mapping_id only, so DANA catalog logs
-- (which have no mapping) never appeared - the dashboard showed no DANA or
-- competitor comparison data.
--
-- Run in Supabase SQL Editor. Safe to re-run.
-- ============================================================================

create or replace view public.latest_price_logs
with (security_invoker = true) as
select distinct on (coalesce(mapping_id::text, 'dana-' || sku_id::text))
    *
from public.price_logs
where mapping_id is not null
   or (source_id in (select id from public.sources where source_type = 'dana')
       and sku_id is not null)
order by coalesce(mapping_id::text, 'dana-' || sku_id::text),
         captured_at desc,
         id desc;
