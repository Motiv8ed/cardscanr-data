# eBay worker capture fixtures (R6 offline)

This directory holds **metadata-only** oracle manifests for Approval A checkpoints P1–P3. No restricted historical page captures are committed to git.

- **249** historical case IDs and expectations: `docs/ebay_r6_p0/EBAY_R6_P0_20261110/HISTORICAL_CASE_MANIFEST.json`
- **873** classifier corpus IDs: `docs/ebay_r6_p0/EBAY_R6_P0_20261110/CLASSIFIER_CORPUS_MANIFEST.json`
- **4096** mixed-signal policy rows: `docs/ebay_r6_p0/EBAY_R6_P0_20261110/MIXED_SIGNAL_OUTCOME_TABLE.csv`

## Handoff fixture blockers (P1)

These three authorized originals are **missing** from all inspected checkouts. Tests that require them skip with `HANDOFF_FIXTURE_BLOCKER` until Andy separately authorizes transfer:

1. `reports/artifacts/inter_card_provider_handoff_closure/fixtures/tropius_prior.json`
2. `reports/artifacts/inter_card_provider_handoff_closure/fixtures/ninetales_cdp_targets.json`
3. `reports/artifacts/inter_card_provider_handoff_closure/fixtures/tropius_sold_page.html`

Set `EBAY_HANDOFF_FIXTURE_ROOT` to an approved private directory when fixtures are restored (never commit raw captures).

## Synthetic inert fixtures

`inert/` contains network-disabled HTML used only for offline adapter/protocol tests. These are synthetic and do not substitute for missing historical handoff originals.
