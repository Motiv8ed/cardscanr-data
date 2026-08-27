-- International pricing fallback provenance on shared market_price_cache rows.

alter table public.market_price_cache
  add column if not exists source_market_country text,
  add column if not exists source_currency text,
  add column if not exists source_price numeric(12,2),
  add column if not exists source_low_price numeric(12,2),
  add column if not exists source_high_price numeric(12,2),
  add column if not exists fx_rate numeric(18,8),
  add column if not exists fx_rate_timestamp timestamptz,
  add column if not exists fx_rate_source text,
  add column if not exists international_source_market text,
  add column if not exists international_fallback_at timestamptz,
  add column if not exists international_fallback_reason text;

comment on column public.market_price_cache.display_price_source is
  'Presentation class: verified_au/local_verified, reference, international_estimate, pending_verification, unavailable.';

create index if not exists market_price_cache_display_source_idx
  on public.market_price_cache (display_price_source);

create index if not exists market_price_cache_international_source_idx
  on public.market_price_cache (international_source_market)
  where display_price_source = 'international_estimate';
