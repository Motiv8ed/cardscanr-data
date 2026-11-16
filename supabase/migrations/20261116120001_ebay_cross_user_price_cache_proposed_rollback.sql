-- Rollback for proposed cross-user cache table (apply only when reverting the proposal).

drop table if exists public.ebay_cross_user_price_cache;