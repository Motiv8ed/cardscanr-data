import { createClient } from "https://esm.sh/@supabase/supabase-js@2.49.1";
import {
  MAX_PAYLOAD_BYTES,
  buildCorsHeaders,
  hmacSha256Hex,
  sha256Hex,
  validateClientPayload,
} from "./_shared/website_feedback_validation.ts";

const GENERIC_SUCCESS =
  "Thank you. Your feedback was received. Keep your reference if you need to follow up.";

function envAllowlist(): string[] {
  const raw = Deno.env.get("WEBSITE_FEEDBACK_CORS_ORIGINS") ?? "";
  const fromEnv = raw.split(",").map((s) => s.trim()).filter((s) => s.length > 0);
  const required = [
    "https://cardscanr.com",
    "https://www.cardscanr.com",
  ];
  // Temporary GitHub Pages validation host (documented fallback).
  const fallback = [
    "https://motiv8ed.github.io",
  ];
  return Array.from(new Set([...required, ...fallback, ...fromEnv]));
}

function jsonResponse(
  body: Record<string, unknown>,
  status: number,
  cors: HeadersInit,
): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      ...cors,
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
    },
  });
}

async function verifyTurnstile(token: string): Promise<boolean> {
  const secret = Deno.env.get("TURNSTILE_SECRET_KEY");
  if (!secret) {
    console.error("turnstile_secret_missing");
    return false;
  }
  const form = new URLSearchParams();
  form.set("secret", secret);
  form.set("response", token);
  // Do not forward remoteip: Edge proxy IPs often differ from browser IP.

  const res = await fetch(
    "https://challenges.cloudflare.com/turnstile/v0/siteverify",
    { method: "POST", body: form },
  );
  if (!res.ok) {
    console.error("turnstile_http_error", res.status);
    return false;
  }
  const data = await res.json() as { success?: boolean; "error-codes"?: string[] };
  if (data.success !== true) {
    console.error(
      "turnstile_rejected",
      Array.isArray(data["error-codes"]) ? data["error-codes"].join(",") : "unknown",
    );
    return false;
  }
  return true;
}

Deno.serve(async (req) => {
  const allowlist = envAllowlist();
  const origin = req.headers.get("Origin");
  const cors = buildCorsHeaders(origin, allowlist);

  if (req.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: cors });
  }

  if (req.method !== "POST") {
    return jsonResponse({ ok: false, code: "method_not_allowed" }, 405, cors);
  }

  if (origin && !allowlist.includes(origin)) {
    return jsonResponse({ ok: false, code: "cors_denied" }, 403, cors);
  }

  const contentLength = Number(req.headers.get("Content-Length") ?? "0");
  if (contentLength > MAX_PAYLOAD_BYTES) {
    return jsonResponse({ ok: false, code: "payload_too_large" }, 413, cors);
  }

  let rawText: string;
  try {
    rawText = await req.text();
  } catch {
    return jsonResponse({ ok: false, code: "invalid_payload" }, 400, cors);
  }

  if (rawText.length > MAX_PAYLOAD_BYTES) {
    return jsonResponse({ ok: false, code: "payload_too_large" }, 413, cors);
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(rawText);
  } catch {
    return jsonResponse({ ok: false, code: "invalid_json" }, 400, cors);
  }

  const validated = validateClientPayload(parsed);
  if (!validated.ok) {
    return jsonResponse(
      { ok: false, code: validated.error.code, message: validated.error.message },
      400,
      cors,
    );
  }

  const value = validated.value;
  const turnstileOk = await verifyTurnstile(value.turnstile_token);
  if (!turnstileOk) {
    return jsonResponse({ ok: false, code: "turnstile_failed" }, 403, cors);
  }

  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  const serviceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  if (!supabaseUrl || !serviceKey) {
    console.error("server_misconfigured");
    return jsonResponse({ ok: false, code: "server_error" }, 500, cors);
  }

  const admin = createClient(supabaseUrl, serviceKey, {
    auth: { persistSession: false, autoRefreshToken: false },
  });

  const remoteIp = req.headers.get("CF-Connecting-IP") ??
    req.headers.get("X-Forwarded-For")?.split(",")[0]?.trim() ??
    "unknown";

  const rateSalt = Deno.env.get("WEBSITE_FEEDBACK_RATE_SALT") ??
    Deno.env.get("TURNSTILE_SECRET_KEY") ??
    "cardscanr-website-feedback";
  const rateKey = (await hmacSha256Hex(rateSalt, `ip:${remoteIp}`)).slice(0, 48);

  const { data: rateData, error: rateError } = await admin.rpc(
    "website_feedback_rate_limit_hit",
    {
      p_bucket_key: `wf:${rateKey}`,
      p_limit: 5,
      p_window_seconds: 3600,
    },
  );

  if (rateError) {
    console.error("rate_limit_error");
    return jsonResponse({ ok: false, code: "server_error" }, 500, cors);
  }

  if (rateData && (rateData as { allowed?: boolean }).allowed === false) {
    return jsonResponse({ ok: false, code: "rate_limited" }, 429, cors);
  }

  const contentHash = await sha256Hex(
    [
      value.feedback_type,
      value.subject.toLowerCase(),
      value.description.toLowerCase(),
      value.contact_email ?? "",
    ].join("|"),
  );

  const { turnstile_token: _token, ...persistable } = value;
  void _token;

  const { data, error } = await admin.rpc("website_feedback_submit_internal", {
    p_payload: {
      ...persistable,
      content_hash: contentHash,
      rate_limit_key: rateKey,
      status: "new",
    },
  });

  if (error) {
    console.error("submit_rpc_error");
    return jsonResponse({ ok: false, code: "server_error" }, 500, cors);
  }

  const result = data as {
    ok?: boolean;
    public_reference?: string;
    duplicate?: boolean;
    code?: string;
  };

  if (result?.ok !== true || !result.public_reference) {
    return jsonResponse({ ok: false, code: "server_error" }, 500, cors);
  }

  return jsonResponse(
    {
      ok: true,
      public_reference: result.public_reference,
      duplicate: result.duplicate === true,
      message: GENERIC_SUCCESS,
    },
    200,
    cors,
  );
});
