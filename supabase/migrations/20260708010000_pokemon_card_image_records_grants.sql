-- Harden pokemon_card_image_records API access for service role and public read.

GRANT SELECT, INSERT, UPDATE, DELETE ON public.pokemon_card_image_records TO service_role;
GRANT SELECT ON public.pokemon_card_image_records TO anon, authenticated;

ALTER TABLE public.pokemon_card_image_records ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS pokemon_card_image_records_public_read ON public.pokemon_card_image_records;
CREATE POLICY pokemon_card_image_records_public_read
  ON public.pokemon_card_image_records
  FOR SELECT
  TO anon, authenticated
  USING (true);
