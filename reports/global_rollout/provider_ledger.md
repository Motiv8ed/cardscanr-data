# Global Provider and Terms Ledger

Terms reviewed: **2026-07-11**

Metadata access and artwork redistribution are evaluated separately. No provider is treated as granting artwork redistribution merely because its metadata or client code is open source.

## tcgdex
- Terms status: **pending_human_review**
- Metadata: approved_with_conditions
- Image rehosting: pending_human_review
- Authentication: none
- Required environment variables: none
- Free limits: Free; no published hard request limit. Provider asks bulk users to cache responses.
- Paid requirements: none
- Adapter: implemented_global_metadata_and_public_image_preflight
- Documentation: https://tcgdex.dev/
- Terms: https://github.com/tcgdex/cards-database

## pokemon_tcg_api
- Terms status: **pending_human_review**
- Metadata: approved_with_conditions
- Image rehosting: pending_human_review
- Authentication: optional X-Api-Key; anonymous access allowed at lower limits
- Required environment variables: POKEMON_TCG_API_KEY
- Free limits: Anonymous: 1,000 requests/day and 30/minute. Free API key: 20,000/day by default.
- Paid requirements: none documented for default keyed access
- Adapter: existing_english_adapter_and_catalogue
- Documentation: https://docs.pokemontcg.io/
- Terms: https://dev.pokemontcg.io/terms

## pokewallet
- Terms status: **pending_human_review**
- Metadata: approved_with_conditions
- Image rehosting: pending_human_review
- Authentication: X-API-Key
- Required environment variables: POKEWALLET_API_KEY
- Free limits: 100 requests/hour, 1,000 requests/day, $0/month
- Paid requirements: Pro is €20/month for 5,000/hour and 50,000/day; never auto-upgrade
- Adapter: existing_metadata_image_adapter_and_global_rate_limiter
- Documentation: https://www.pokewallet.io/api-docs
- Terms: https://www.pokewallet.io/terms-conditions

## scrydex
- Terms status: **prohibited**
- Metadata: pending_human_review
- Image rehosting: prohibited_without_written_authorization
- Authentication: X-Api-Key plus X-Team-ID
- Required environment variables: SCRYDEX_API_KEY, SCRYDEX_TEAM_ID
- Free limits: No $0 catalogue plan found on the current pricing page.
- Paid requirements: Starter: US$29/month, 5,000 credits, US$0.006 per overage credit
- Adapter: credential_preflight_prepared_no_paid_requests_executed
- Documentation: https://scrydex.com/docs
- Terms: https://scrydex.com/terms

## ximilar
- Terms status: **approved_with_conditions**
- Metadata: prohibited_as_catalogue_source
- Image rehosting: not_applicable
- Authentication: Authorization: Token
- Required environment variables: XIMILAR_API_TOKEN
- Free limits: 1,000 credits/month; TCG identification currently costs 10 credits
- Paid requirements: paid credit plans/packs for scale; never auto-purchase
- Adapter: credential_preflight_only_recognition_reserved
- Documentation: https://docs.ximilar.com/collectibles/recognition
- Terms: https://www.ximilar.com/

## official_regional_pokemon_catalogues
- Terms status: **pending_human_review**
- Metadata: pending_human_review
- Image rehosting: pending_human_review
- Authentication: no documented developer API discovered
- Required environment variables: none
- Free limits: not applicable
- Paid requirements: not applicable
- Adapter: not_implemented_no_scraping_or_browser_automation
- Documentation: https://www.pokemon.com/
- Terms: provider_and_region_specific
