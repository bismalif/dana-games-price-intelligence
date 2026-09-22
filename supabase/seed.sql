-- ============================================================================
-- DANA Games Price Intelligence - seed data
-- Run AFTER schema.sql. Idempotent: safe to re-run.
--
-- Sources reporting "login required" (SEAGM, PlayAsia, KiosGamer) are seeded
-- DISABLED with requires_auth = true until authorized access is confirmed.
-- Midasbuy is seeded disabled because it carries no MLBB/Free Fire catalog.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- Games (DANA catalog URLs are used by the scraper for SKU discovery)
-- ---------------------------------------------------------------------------
insert into public.games (slug, name, dana_url, active) values
    ('mobile-legends', 'Mobile Legends: Bang Bang', 'https://www.dana.id/games/detail/game/mobile_legends', true),
    ('free-fire', 'Free Fire', 'https://www.dana.id/games/detail/game/free_fire', true)
on conflict (slug) do update
    set name = excluded.name,
        dana_url = excluded.dana_url,
        updated_at = now();

-- ---------------------------------------------------------------------------
-- Sources
-- ---------------------------------------------------------------------------
insert into public.sources (slug, name, source_type, base_url, enabled, notes) values
    ('dana-games',  'DANA Games',  'dana',       'https://www.dana.id/games/home',      true,  'Canonical price reference'),
    ('codashop',    'Codashop',    'competitor', 'https://www.codashop.com/',           true,  null),
    ('unipin',      'UniPin',      'competitor', 'https://www.unipin.com/',             true,  null),
    ('gopay-games', 'GoPay Games', 'competitor', 'https://gopay.co.id/games',           true,  null),
    ('lapakgaming', 'Lapakgaming', 'competitor', 'https://www.lapakgaming.com/',        true,  null),
    ('gogogo',      'GOGOGO',      'competitor', 'https://gogogo.id/',                  true,  null),
    ('mobapay',     'MobaPay',     'competitor', 'https://www.mobapay.com/',            true,  'MLBB only'),
    ('seagm',       'SEAGM',       'competitor', 'https://www.seagm.com/',              false, 'Reports login requirement; disabled until authorized access is confirmed'),
    ('playasia',    'PlayAsia',    'competitor', 'https://www.play-asia.com/',          false, 'Reports login requirement; disabled until authorized access is confirmed'),
    ('kiosgamer',   'KiosGamer',   'competitor', 'https://kiosgamer.co.id/',            false, 'Login required; disabled until authorized access is confirmed'),
    ('midasbuy',    'Midasbuy',    'competitor', 'https://www.midasbuy.com/',           false, 'No MLBB/Free Fire catalog; enable when PUBG Mobile or another supported title is added')
on conflict (slug) do update
    set name = excluded.name,
        source_type = excluded.source_type,
        base_url = excluded.base_url,
        enabled = excluded.enabled,
        notes = excluded.notes,
        updated_at = now();

-- ---------------------------------------------------------------------------
-- Competitor product mappings (sku_id = null -> automatic matching by
-- game + effective units; add a manual override from the dashboard if needed)
-- ---------------------------------------------------------------------------
insert into public.source_sku_mappings
    (source_id, game_id, product_url, product_label, enabled, requires_auth)
values
    -- Codashop (enabled)
    ((select id from public.sources where slug = 'codashop'), (select id from public.games where slug = 'mobile-legends'), 'https://www.codashop.com/id-id/mobile-legends', 'MLBB catalog', true, false),
    ((select id from public.sources where slug = 'codashop'), (select id from public.games where slug = 'free-fire'),      'https://www.codashop.com/id-id/free-fire',      'Free Fire catalog', true, false),

    -- UniPin (enabled)
    ((select id from public.sources where slug = 'unipin'), (select id from public.games where slug = 'mobile-legends'), 'https://www.unipin.com/id/mobile-legends', 'MLBB catalog', true, false),
    ((select id from public.sources where slug = 'unipin'), (select id from public.games where slug = 'free-fire'),      'https://www.unipin.com/id/garena-free-fire', 'Free Fire catalog', true, false),

    -- GoPay Games (enabled)
    ((select id from public.sources where slug = 'gopay-games'), (select id from public.games where slug = 'mobile-legends'), 'https://gopay.co.id/games/mobile-legends-bang-bang', 'MLBB catalog', true, false),
    ((select id from public.sources where slug = 'gopay-games'), (select id from public.games where slug = 'free-fire'),      'https://gopay.co.id/games/free-fire', 'Free Fire catalog', true, false),

    -- Lapakgaming (enabled)
    ((select id from public.sources where slug = 'lapakgaming'), (select id from public.games where slug = 'mobile-legends'), 'https://www.lapakgaming.com/id-id/mobile-legends', 'MLBB catalog', true, false),
    ((select id from public.sources where slug = 'lapakgaming'), (select id from public.games where slug = 'free-fire'),      'https://www.lapakgaming.com/id-id/free-fire', 'Free Fire catalog', true, false),

    -- GOGOGO (enabled)
    ((select id from public.sources where slug = 'gogogo'), (select id from public.games where slug = 'mobile-legends'), 'https://gogogo.id/mobile-legends', 'MLBB catalog', true, false),
    ((select id from public.sources where slug = 'gogogo'), (select id from public.games where slug = 'free-fire'),      'https://gogogo.id/free-fire', 'Free Fire catalog', true, false),

    -- MobaPay (enabled, MLBB only)
    ((select id from public.sources where slug = 'mobapay'), (select id from public.games where slug = 'mobile-legends'), 'https://www.mobapay.com/', 'MLBB catalog', true, false),

    -- SEAGM (disabled: login required)
    ((select id from public.sources where slug = 'seagm'), (select id from public.games where slug = 'mobile-legends'), 'https://www.seagm.com/mobile-legends', 'MLBB catalog', false, true),
    ((select id from public.sources where slug = 'seagm'), (select id from public.games where slug = 'free-fire'),      'https://www.seagm.com/free-fire-id-diamonds-top-up', 'Free Fire catalog', false, true),

    -- PlayAsia (disabled: login required)
    ((select id from public.sources where slug = 'playasia'), (select id from public.games where slug = 'mobile-legends'), 'https://www.play-asia.com/digital/mobile_legends/14/7123d', 'MLBB catalog', false, true),
    ((select id from public.sources where slug = 'playasia'), (select id from public.games where slug = 'free-fire'),      'https://www.play-asia.com/digital/free_fire/14/7123f', 'Free Fire catalog', false, true),

    -- KiosGamer (disabled: login required, Free Fire only)
    ((select id from public.sources where slug = 'kiosgamer'), (select id from public.games where slug = 'free-fire'), 'https://kiosgamer.co.id/app/100067', 'Free Fire catalog', false, true)
on conflict (source_id, game_id, product_url) do update
    set product_label = excluded.product_label,
        enabled = excluded.enabled,
        requires_auth = excluded.requires_auth,
        updated_at = now();
