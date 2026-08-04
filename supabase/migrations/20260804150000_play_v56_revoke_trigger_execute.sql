-- play_v56_revoke_trigger_execute
-- Trigger helpers must not be callable via PostgREST.
REVOKE ALL ON FUNCTION public.customer_force_owner_user_id() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.customer_force_owner_user_id() FROM anon, authenticated;
REVOKE ALL ON FUNCTION public.customer_set_updated_at() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.customer_set_updated_at() FROM anon, authenticated;
GRANT EXECUTE ON FUNCTION public.customer_force_owner_user_id() TO postgres, service_role;
GRANT EXECUTE ON FUNCTION public.customer_set_updated_at() TO postgres, service_role;
