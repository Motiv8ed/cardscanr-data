-- PROPOSED ONLY — do not apply to production without owner review.
-- Rollback: 20261116120001_ebay_cross_user_price_cache_proposed_rollback.sql

create table if not exists public.ebay_cross_user_price_cache (
    id uuid primary key default gen_random_uuid(),
    cache_key text not null unique,
    catalogue_card_id text,
    language text not null default 'en',
    condition_band text not null default 'raw',
    market_country text not null default 'AU',
    currency text not null default 'AUD',
    current_market_price numeric,
    price_source text,
    last_priced_at timestamptz,
    freshness_days integer not null default 7,
    created_at timestamptz not null default timezone('utc', now()),
    updated_at timestamptz not null default timezone('utc', now())
);

create index if not exists ebay_cross_user_price_cache_card_lang_idx
    on public.ebay_cross_user_price_cache (catalogue_card_id, language, condition_band);

comment on table public.ebay_cross_user_price_cache is
    'Cross-user AU sold price cache (card + language + condition band). Unused until EBAY_SCALE_CROSS_USER_CACHE is enabled.';