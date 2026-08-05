/** Shared validation for CardScanR website feedback Edge Function. */

export const MAX_PAYLOAD_BYTES = 16_384; // 16 KiB

export const ALLOWED_FEEDBACK_TYPES = [
  "bug_crash",
  "feature_suggestion",
  "incorrect_card_match",
  "missing_card_or_set",
  "incorrect_market_price",
  "scanner_ocr",
  "collection_binder",
  "login_account",
  "other",
] as const;

export type FeedbackType = (typeof ALLOWED_FEEDBACK_TYPES)[number];

const EMAIL_RE =
  /^[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$/i;

export type ValidationError = { code: string; message: string };

export function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Strip HTML tags and control characters except newlines/tabs. */
export function sanitiseText(value: unknown, max: number): string | null {
  if (typeof value !== "string") return null;
  let text = value.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  text = text.replace(/<[^>]*>/g, " ");
  text = text.replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/g, "");
  text = text.replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n");
  text = text.trim();
  if (text.length === 0) return null;
  return text.slice(0, max);
}

export function sanitiseSingleLine(value: unknown, max: number): string | null {
  const text = sanitiseText(value, max);
  if (!text) return null;
  return text.replace(/\s+/g, " ").trim().slice(0, max);
}

export function normalizeEmail(raw: unknown): string | null {
  if (raw === undefined || raw === null || raw === "") return null;
  if (typeof raw !== "string") return null;
  const trimmed = raw.trim().toLowerCase();
  if (trimmed.length < 3 || trimmed.length > 320) return null;
  if (!EMAIL_RE.test(trimmed)) return null;
  return trimmed;
}

export const ALLOWED_CLIENT_FIELDS = new Set([
  "feedback_type",
  "subject",
  "description",
  "card_name",
  "set_name",
  "collector_number",
  "app_version",
  "android_version",
  "device_model",
  "contact_email",
  "reproduction_steps",
  "turnstile_token",
  "source",
]);

export type NormalizedFeedback = {
  feedback_type: FeedbackType;
  subject: string;
  description: string;
  card_name: string | null;
  set_name: string | null;
  collector_number: string | null;
  app_version: string | null;
  android_version: string | null;
  device_model: string | null;
  contact_email: string | null;
  reproduction_steps: string | null;
  turnstile_token: string;
  source: "legal_site" | "website";
};

export function validateClientPayload(
  body: unknown,
): { ok: true; value: NormalizedFeedback } | { ok: false; error: ValidationError } {
  if (!isPlainObject(body)) {
    return { ok: false, error: { code: "invalid_payload", message: "Invalid request." } };
  }

  for (const key of Object.keys(body)) {
    if (!ALLOWED_CLIENT_FIELDS.has(key)) {
      return { ok: false, error: { code: "unexpected_field", message: "Invalid request." } };
    }
  }

  const typeRaw = sanitiseSingleLine(body.feedback_type, 64);
  if (!typeRaw || !(ALLOWED_FEEDBACK_TYPES as readonly string[]).includes(typeRaw)) {
    return {
      ok: false,
      error: { code: "feedback_type", message: "Select a valid feedback type." },
    };
  }

  const subject = sanitiseSingleLine(body.subject, 120);
  if (!subject || subject.length < 4) {
    return {
      ok: false,
      error: { code: "subject", message: "Subject must be at least 4 characters." },
    };
  }

  const description = sanitiseText(body.description, 4000);
  if (!description || description.length < 10) {
    return {
      ok: false,
      error: {
        code: "description",
        message: "Description must be at least 10 characters.",
      },
    };
  }

  let contactEmail: string | null = null;
  if (body.contact_email !== undefined && body.contact_email !== null && body.contact_email !== "") {
    contactEmail = normalizeEmail(body.contact_email);
    if (!contactEmail) {
      return {
        ok: false,
        error: { code: "contact_email", message: "Enter a valid email address or leave it blank." },
      };
    }
  }

  const turnstile = typeof body.turnstile_token === "string" ? body.turnstile_token.trim() : "";
  if (!turnstile || turnstile.length > 2048) {
    return {
      ok: false,
      error: { code: "turnstile", message: "Bot protection challenge is required." },
    };
  }

  let source: "legal_site" | "website" = "legal_site";
  if (body.source !== undefined && body.source !== null && body.source !== "") {
    const s = sanitiseSingleLine(body.source, 32);
    if (s !== "legal_site" && s !== "website") {
      return { ok: false, error: { code: "source", message: "Invalid request." } };
    }
    source = s;
  }

  return {
    ok: true,
    value: {
      feedback_type: typeRaw as FeedbackType,
      subject,
      description,
      card_name: sanitiseSingleLine(body.card_name, 160),
      set_name: sanitiseSingleLine(body.set_name, 160),
      collector_number: sanitiseSingleLine(body.collector_number, 40),
      app_version: sanitiseSingleLine(body.app_version, 40),
      android_version: sanitiseSingleLine(body.android_version, 40),
      device_model: sanitiseSingleLine(body.device_model, 80),
      contact_email: contactEmail,
      reproduction_steps: sanitiseText(body.reproduction_steps, 2000),
      turnstile_token: turnstile,
      source,
    },
  };
}

export function isApprovedOrigin(origin: string | null, allowlist: string[]): boolean {
  if (!origin) return false;
  return allowlist.includes(origin);
}

export function buildCorsHeaders(origin: string | null, allowlist: string[]): HeadersInit {
  const allowed = origin && isApprovedOrigin(origin, allowlist) ? origin : allowlist[0] ?? "";
  return {
    "Access-Control-Allow-Origin": allowed,
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers":
      "authorization, content-type, x-client-info, apikey",
    "Access-Control-Max-Age": "86400",
    "Vary": "Origin",
  };
}

export async function sha256Hex(input: string): Promise<string> {
  const data = new TextEncoder().encode(input);
  const digest = await crypto.subtle.digest("SHA-256", data);
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

export async function hmacSha256Hex(secret: string, input: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(input));
  return Array.from(new Uint8Array(sig))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}
