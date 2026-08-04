// Supabase Edge Function: delete-account
//
// Server-owned CardScanR account deletion (Play v56).
// - JWT verified by platform (verify_jwt = true).
// - User id is taken only from the validated session (JWT sub).
// - Never trusts a user id from the request body.
// - Calls customer_purge_cloud_collection_data as service_role for that user.
// - Best-effort deletes related beta / legacy cloud rows when tables exist.
// - Deletes the Auth user via admin API.
// - Minimal success body: { ok: true }
// - Do not log emails, JWTs, tokens, or other personal data.

import { createClient } from 'https://esm.sh/@supabase/supabase-js@2.49.1'

const ALLOWED_ORIGINS = new Set([
  'https://cardscanr.com',
  'https://www.cardscanr.com',
  'https://app.cardscanr.com',
  'https://motiv8ed.github.io',
  'http://localhost:3000',
  'http://localhost:5173',
  'http://127.0.0.1:3000',
  'http://127.0.0.1:5173',
])

type CloudTable = {
  table: string
  column: 'user_id' | 'id'
}

/** Legacy production mobile tables (children before parents). */
const LEGACY_TABLES: ReadonlyArray<CloudTable> = [
  { table: 'scan_sessions', column: 'user_id' },
  { table: 'user_cards', column: 'user_id' },
  { table: 'user_collections', column: 'user_id' },
  { table: 'user_profiles', column: 'id' },
]

/** Beta / request tables — optional; ignore if missing. */
const BETA_TABLES: ReadonlyArray<CloudTable> = [
  { table: 'beta_feedback_attachments', column: 'user_id' },
  { table: 'beta_feedback_reports', column: 'user_id' },
  { table: 'beta_error_reports', column: 'user_id' },
  { table: 'beta_analytics_events', column: 'user_id' },
  { table: 'beta_device_installations', column: 'user_id' },
  { table: 'beta_profiles', column: 'user_id' },
]

function corsHeaders(req: Request): Record<string, string> {
  const origin = req.headers.get('Origin') ?? ''
  const allowOrigin = ALLOWED_ORIGINS.has(origin)
    ? origin
    : 'https://cardscanr.com'
  return {
    'Access-Control-Allow-Origin': allowOrigin,
    'Access-Control-Allow-Headers':
      'authorization, x-client-info, apikey, content-type',
    'Access-Control-Allow-Methods': 'POST, OPTIONS',
    Vary: 'Origin',
  }
}

function jsonResponse(
  req: Request,
  status: number,
  body: Record<string, unknown>,
): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...corsHeaders(req), 'Content-Type': 'application/json' },
  })
}

function isMissingRelationError(message: string | undefined): boolean {
  if (!message) return false
  const normalized = message.toLowerCase()
  return (
    normalized.includes('does not exist') ||
    normalized.includes('could not find the table') ||
    normalized.includes('could not find the function') ||
    normalized.includes('schema cache')
  )
}

function isMissingUserError(message: string | undefined): boolean {
  if (!message) return false
  const normalized = message.toLowerCase()
  return (
    normalized.includes('user not found') ||
    normalized.includes('not found') ||
    normalized.includes('does not exist')
  )
}

async function bestEffortDeleteRows(
  admin: ReturnType<typeof createClient>,
  entry: CloudTable,
  userId: string,
): Promise<'ok' | 'missing' | 'failed'> {
  try {
    const { error } = await admin.from(entry.table).delete().eq(entry.column, userId)
    if (!error) return 'ok'
    if (isMissingRelationError(error.message)) return 'missing'
    return 'failed'
  } catch (_) {
    return 'missing'
  }
}

Deno.serve(async (req) => {
  if (req.method === 'OPTIONS') {
    return new Response('ok', { headers: corsHeaders(req) })
  }
  if (req.method !== 'POST') {
    return jsonResponse(req, 405, { error: 'method_not_allowed' })
  }

  const supabaseUrl = Deno.env.get('SUPABASE_URL') ?? ''
  const anonKey = Deno.env.get('SUPABASE_ANON_KEY') ?? ''
  const serviceRoleKey = Deno.env.get('SUPABASE_SERVICE_ROLE_KEY') ?? ''
  if (!supabaseUrl || !anonKey || !serviceRoleKey) {
    return jsonResponse(req, 500, { error: 'server_misconfigured' })
  }

  const authHeader = req.headers.get('Authorization') ?? ''
  if (!authHeader.startsWith('Bearer ')) {
    return jsonResponse(req, 401, { error: 'missing_bearer_token' })
  }

  // Derive caller from JWT only — ignore any body user id.
  const userClient = createClient(supabaseUrl, anonKey, {
    global: { headers: { Authorization: authHeader } },
  })
  const {
    data: { user },
    error: userError,
  } = await userClient.auth.getUser()
  if (userError || !user?.id) {
    return jsonResponse(req, 401, { error: 'unauthorized' })
  }

  const userId = user.id
  const admin = createClient(supabaseUrl, serviceRoleKey)

  try {
    // Customer portal cloud wipe (service_role may pass any p_user_id;
    // we always pass JWT sub).
    const { error: purgeError } = await admin.rpc(
      'customer_purge_cloud_collection_data',
      { p_user_id: userId },
    )
    if (purgeError && !isMissingRelationError(purgeError.message)) {
      return jsonResponse(req, 500, { error: 'customer_purge_failed' })
    }

    for (const entry of BETA_TABLES) {
      // Optional beta/request rows — ignore missing tables and row errors.
      await bestEffortDeleteRows(admin, entry, userId)
    }

    for (const entry of LEGACY_TABLES) {
      const result = await bestEffortDeleteRows(admin, entry, userId)
      if (result === 'failed') {
        return jsonResponse(req, 500, { error: 'cloud_row_delete_failed' })
      }
    }

    const { error: deleteError } = await admin.auth.admin.deleteUser(userId)
    if (deleteError && !isMissingUserError(deleteError.message)) {
      return jsonResponse(req, 500, { error: 'auth_user_delete_failed' })
    }

    return jsonResponse(req, 200, { ok: true })
  } catch (_) {
    return jsonResponse(req, 500, { error: 'unexpected_failure' })
  }
})
