-- Play v56: optimize legacy user-data RLS initplans.
-- Applied live as play_v56_legacy_rls_initplan.
-- Replaces repeated auth.uid() with (select auth.uid()) on legacy tables.

do $$
begin
  -- user_profiles
  if exists (select 1 from pg_policies where tablename='user_profiles' and policyname='Users can view their own profile') then
    execute 'drop policy "Users can view their own profile" on public.user_profiles';
    execute 'create policy "Users can view their own profile" on public.user_profiles for select using ((select auth.uid()) = id)';
  end if;
  if exists (select 1 from pg_policies where tablename='user_profiles' and policyname='Users can insert their own profile') then
    execute 'drop policy "Users can insert their own profile" on public.user_profiles';
    execute 'create policy "Users can insert their own profile" on public.user_profiles for insert with check ((select auth.uid()) = id)';
  end if;
  if exists (select 1 from pg_policies where tablename='user_profiles' and policyname='Users can update their own profile') then
    execute 'drop policy "Users can update their own profile" on public.user_profiles';
    execute 'create policy "Users can update their own profile" on public.user_profiles for update using ((select auth.uid()) = id) with check ((select auth.uid()) = id)';
  end if;

  -- user_collections
  if exists (select 1 from pg_policies where tablename='user_collections' and policyname='Users can view their own collections') then
    execute 'drop policy "Users can view their own collections" on public.user_collections';
    execute 'create policy "Users can view their own collections" on public.user_collections for select using ((select auth.uid()) = user_id)';
  end if;
  if exists (select 1 from pg_policies where tablename='user_collections' and policyname='Users can insert their own collections') then
    execute 'drop policy "Users can insert their own collections" on public.user_collections';
    execute 'create policy "Users can insert their own collections" on public.user_collections for insert with check ((select auth.uid()) = user_id)';
  end if;
  if exists (select 1 from pg_policies where tablename='user_collections' and policyname='Users can update their own collections') then
    execute 'drop policy "Users can update their own collections" on public.user_collections';
    execute 'create policy "Users can update their own collections" on public.user_collections for update using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id)';
  end if;
  if exists (select 1 from pg_policies where tablename='user_collections' and policyname='Users can delete their own collections') then
    execute 'drop policy "Users can delete their own collections" on public.user_collections';
    execute 'create policy "Users can delete their own collections" on public.user_collections for delete using ((select auth.uid()) = user_id)';
  end if;

  -- user_cards
  if exists (select 1 from pg_policies where tablename='user_cards' and policyname='Users can view their own cards') then
    execute 'drop policy "Users can view their own cards" on public.user_cards';
    execute 'create policy "Users can view their own cards" on public.user_cards for select using ((select auth.uid()) = user_id)';
  end if;
  if exists (select 1 from pg_policies where tablename='user_cards' and policyname='Users can insert their own cards') then
    execute 'drop policy "Users can insert their own cards" on public.user_cards';
    execute 'create policy "Users can insert their own cards" on public.user_cards for insert with check ((select auth.uid()) = user_id)';
  end if;
  if exists (select 1 from pg_policies where tablename='user_cards' and policyname='Users can update their own cards') then
    execute 'drop policy "Users can update their own cards" on public.user_cards';
    execute 'create policy "Users can update their own cards" on public.user_cards for update using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id)';
  end if;
  if exists (select 1 from pg_policies where tablename='user_cards' and policyname='Users can delete their own cards') then
    execute 'drop policy "Users can delete their own cards" on public.user_cards';
    execute 'create policy "Users can delete their own cards" on public.user_cards for delete using ((select auth.uid()) = user_id)';
  end if;

  -- scan_sessions
  if exists (select 1 from pg_policies where tablename='scan_sessions' and policyname='Users can view their own scan sessions') then
    execute 'drop policy "Users can view their own scan sessions" on public.scan_sessions';
    execute 'create policy "Users can view their own scan sessions" on public.scan_sessions for select using ((select auth.uid()) = user_id)';
  end if;
  if exists (select 1 from pg_policies where tablename='scan_sessions' and policyname='Users can insert their own scan sessions') then
    execute 'drop policy "Users can insert their own scan sessions" on public.scan_sessions';
    execute 'create policy "Users can insert their own scan sessions" on public.scan_sessions for insert with check ((select auth.uid()) = user_id)';
  end if;
  if exists (select 1 from pg_policies where tablename='scan_sessions' and policyname='Users can update their own scan sessions') then
    execute 'drop policy "Users can update their own scan sessions" on public.scan_sessions';
    execute 'create policy "Users can update their own scan sessions" on public.scan_sessions for update using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id)';
  end if;
  if exists (select 1 from pg_policies where tablename='scan_sessions' and policyname='Users can delete their own scan sessions') then
    execute 'drop policy "Users can delete their own scan sessions" on public.scan_sessions';
    execute 'create policy "Users can delete their own scan sessions" on public.scan_sessions for delete using ((select auth.uid()) = user_id)';
  end if;

  -- market_price_refresh_jobs
  if exists (select 1 from pg_policies where tablename='market_price_refresh_jobs' and policyname='market_price_refresh_jobs_read_own') then
    execute 'drop policy market_price_refresh_jobs_read_own on public.market_price_refresh_jobs';
    execute 'create policy market_price_refresh_jobs_read_own on public.market_price_refresh_jobs for select to authenticated using (requested_by_user_id = (select auth.uid()))';
  end if;
end $$;
