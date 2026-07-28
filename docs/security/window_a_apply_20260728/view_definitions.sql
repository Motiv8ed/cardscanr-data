-- card_image_manifests_current reloptions: {security_invoker=true}
-- card_image_manifests_with_legacy_records reloptions: {security_invoker=true}

CREATE VIEW public.card_image_manifests_current
WITH (security_invoker = true) AS
 SELECT id,
    card_id,
    set_id,
    language,
    variant,
    rights_status,
    r2_display_key,
    r2_thumbnail_key,
    public_display_url,
    public_thumbnail_url,
    content_sha256,
    width,
    height,
    byte_size,
    mime_type,
    verification_status,
    is_current,
    quality_classification
   FROM card_image_manifests m
  WHERE is_current = true AND verification_status = 'verified'::text;

-- Legacy view: security_invoker=true; SELECT grant service_role only.
-- Definition retained for admin/pipeline; omitted URL/hash values in evidence.
