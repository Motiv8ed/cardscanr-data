from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SQL = (ROOT / "supabase" / "migrations" / "20260613000000_user_price_refresh_batches.sql").read_text(
    encoding="utf-8"
)


def test_user_price_refresh_batch_tables_exist() -> None:
    assert "create table if not exists public.user_price_refresh_batches" in SQL
    assert "create table if not exists public.user_price_refresh_batch_items" in SQL
    assert "create table if not exists public.user_push_tokens" in SQL
    assert "create table if not exists public.user_notification_events" in SQL


def test_enqueue_missing_prices_rpc_exists_and_dedupes() -> None:
    assert "create or replace function public.enqueue_missing_prices_for_user_collection" in SQL
    assert "p_notify_on_complete boolean default true" in SQL
    assert "created_at > now() - interval '6 hours'" in SQL
    assert "public.request_market_price_refresh(" in SQL
    assert "public.get_user_price_refresh_batch_status(v_batch_id)" in SQL


def test_worker_completion_updates_batch_and_creates_one_notification_event() -> None:
    assert "create trigger trg_user_price_refresh_batch_job_update" in SQL
    assert "after update of status on public.market_price_refresh_jobs" in SQL
    assert "notification_created_at is null" in SQL
    assert "price_refresh_batch_completed" in SQL


def test_rls_and_function_grants_are_present() -> None:
    assert "alter table public.user_price_refresh_batches enable row level security" in SQL
    assert "user_price_refresh_batches_read_own" in SQL
    assert "user_push_tokens_own" in SQL
    assert (
        "grant execute on function public.enqueue_missing_prices_for_user_collection"
        in SQL
    )
