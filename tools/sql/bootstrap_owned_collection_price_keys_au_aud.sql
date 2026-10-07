"""SQL bootstrap for owned collection → market_price_keys (AU/AUD).

Run via Supabase SQL editor / MCP execute_sql with service role.

Portal valuation only prices rows that resolve to market_price_keys.
This script ensures keys for eligible owned printings without inventing prices.
Follow with: python workers/bulk_reference_price_sync.py

Skips incomplete identity rows (missing collector_number / card_name / set_id).
"""

-- eligible owned rows
with owned as (
  select
    c.card_name,
    c.set_name,
    c.set_id,
    c.collector_number,
    coalesce(nullif(trim(c.language), ''), 'en') as language,
    coalesce(nullif(trim(c.variant), ''), 'Normal') as variant,
    coalesce(nullif(trim(c.condition), ''), 'Unreviewed') as condition,
    nullif(trim(c.physical_printing_id), '') as physical_printing_id,
    nullif(trim(c.identity_model_version), '') as identity_model_version,
    public.cardscanr_market_price_fingerprint(
      c.card_name, c.set_name, c.set_id, c.collector_number, c.language, c.variant, c.condition, 'au', 'aud'
    ) as fingerprint
  from public.customer_collection_items c
  where c.deleted_at is null
    and nullif(trim(c.collector_number), '') is not null
    and nullif(trim(c.card_name), '') is not null
    and nullif(trim(c.set_id), '') is not null
),
ins as (
  select
    public.get_or_create_market_price_key(
      'pokemon',
      o.card_name,
      public.cardscanr_normalize_price_text(o.card_name),
      coalesce(o.set_name, o.set_id),
      o.set_id,
      o.collector_number,
      o.language,
      o.variant,
      o.condition,
      'au',
      'aud',
      o.fingerprint,
      now(),
      null,
      null,
      '[]'::jsonb,
      o.physical_printing_id,
      o.identity_model_version
    ) as key_id
  from owned o
)
select
  (select count(*) from owned) as owned_eligible,
  (select count(distinct key_id) from ins) as distinct_keys_ensured,
  (select count(*) from public.market_price_keys) as keys_total_now;
