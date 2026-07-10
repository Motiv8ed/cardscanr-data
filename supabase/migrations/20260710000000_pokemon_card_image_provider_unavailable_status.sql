-- Allow provider_image_unavailable status for cards whose provider identity is valid
-- but the approved public CDN has no image asset yet.

alter table public.pokemon_card_image_records
  drop constraint if exists pokemon_card_image_records_status_valid;

alter table public.pokemon_card_image_records
  add constraint pokemon_card_image_records_status_valid check (
    status in (
      'pending',
      'processing',
      'completed',
      'failed',
      'skipped',
      'verified',
      'provider_image_unavailable'
    )
  );
