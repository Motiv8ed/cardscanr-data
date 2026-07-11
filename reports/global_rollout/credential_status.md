# Provider Credential Status

No secret value or partial key is included in this report.

## tcgdex
- Key present: **not required**
- Validation: not tested
- Account/quota state: `{'state': 'no_account_required'}`
- Account: https://tcgdex.dev/
- Plan: No account required
- Free quota: Free; no published hard rate limit
- Environment variables: none
- Required scopes: none
- Reason: No credential is required.
- Local configuration: `config/provider_credentials.local.json`
- Validation command: `python tools/global_rollout.py credentials-status --provider tcgdex --validate`
- Resume command: `python tools/global_rollout.py resume`

## pokemon_tcg_api
- Key present: **no**
- Validation: not tested
- Account/quota state: `{'state': 'not_configured'}`
- Account: https://dev.pokemontcg.io/
- Plan: Free key (optional for higher quota)
- Free quota: Anonymous 1,000/day and 30/minute; keyed default 20,000/day
- Environment variables: POKEMON_TCG_API_KEY
- Required scopes: read-only cards and sets API access
- Reason: Optional higher-quota validation and English metadata reconciliation.
- Local configuration: `config/provider_credentials.local.json`
- Validation command: `python tools/global_rollout.py credentials-status --provider pokemon_tcg_api --validate`
- Resume command: `python tools/global_rollout.py resume`

## pokewallet
- Key present: **yes**
- Validation: not tested
- Account/quota state: `{'state': 'configured'}`
- Account: https://www.pokewallet.io/dashboard
- Plan: Free
- Free quota: 100/hour and 1,000/day
- Environment variables: POKEWALLET_API_KEY
- Required scopes: read-only cards, sets, search, and image endpoints
- Reason: Authenticated localized metadata/image coverage; bulk image use remains at the terms gate.
- Local configuration: `config/provider_credentials.local.json`
- Validation command: `python tools/global_rollout.py credentials-status --provider pokewallet --validate`
- Resume command: `python tools/global_rollout.py resume`

## scrydex
- Key present: **no**
- Validation: not tested
- Account/quota state: `{'state': 'not_configured'}`
- Account: https://scrydex.com/register
- Plan: Paid Starter, US$29/month
- Free quota: No current $0 plan found; Starter includes 5,000 credits
- Environment variables: SCRYDEX_API_KEY, SCRYDEX_TEAM_ID
- Required scopes: read-only Pokémon cards, expansions, and image URLs
- Reason: English/Japanese supplemental coverage, only after paid-spend and written-authorization gates.
- Local configuration: `config/provider_credentials.local.json`
- Validation command: `python tools/global_rollout.py credentials-status --provider scrydex --validate`
- Resume command: `python tools/global_rollout.py resume`

## ximilar
- Key present: **no**
- Validation: not tested
- Account/quota state: `{'state': 'not_configured'}`
- Account: https://app.ximilar.com/
- Plan: Free recognition plan
- Free quota: 1,000 credits/month; 10 credits per TCG identification
- Environment variables: XIMILAR_API_TOKEN
- Required scopes: TCG recognition endpoint only
- Reason: Optional recognition of user-owned captures; never used as an artwork source.
- Local configuration: `config/provider_credentials.local.json`
- Validation command: `python tools/global_rollout.py credentials-status --provider ximilar --validate`
- Resume command: `python tools/global_rollout.py resume`
