# (d) Frozen historical identity, fixture and privacy inventory

## VERIFIED metadata, no test execution

HISTORICAL_CASE_MANIFEST.json/.csv preserves **249 exact collected independent case IDs**, original source/archived path, SHA256, function line, static assertion text, historical receipt+revision/outcome, expectation review and privacy/license class. Source and archived files are present. IDs retain whitespace/parameter spelling; do not relabel or deduplicate away failures. Groups:

| Historical group | Original source | Count |
|---|---|---:|
| Original acceptance | 20261110_data_pr30_b9b9fd56/test_independent_acceptance.py |5|
| Older adversarial | 20261110_data_pr30_704d1b44/test_prior_adversarial.py |38|
| Required86 | 20261110_data_pr30_704d1b44/test_fresh_variants.py |86|
| R4 additional31 | 20261110_data_pr30_704d1b44/test_refactor_adversarial.py |31|
| Later62-era33 | 20261110_data_pr30_62f3c0aa/test_final_adversarial.py |33|
| Fresh R5 | 20261110_data_pr31_9895cffa/test_r5_independent.py |56|

Count244 is the decision document's124+31+33+56;249 adds the original5, not a claim of five new R5 cases. Original5 receipt is at b9b9fd56; their individual outcomes were NOT rerun at R5. Manifest distinguishes that explicitly. All P0 executed flags false. Archived XML files are parsed as data, never imported as tests. Nested exception expectations and static assertions are pointers to original oracles, not re-evaluated expressions.

CLASSIFIER_CORPUS_MANIFEST.json freezes **873 IDs/groups/input digests**, not raw page text. Its18 healthy fixture representations are six whitespace views x three representations of the same synthetic Bloodmoon fixture, not18 independent live pages. Three passive controls are synthetic. Historical repository-suite IDs/statuses, including skips/errors/subtest XML records, are in HISTORICAL_REPOSITORY_CASE_IDS.json. XML record counts can include subtests and are not interchangeable with reported test-function counts. The old1428/47/41/1 and1457/47/41/1 receipts remain historical only.

## Fixture presence/provenance

FIXTURE_INVENTORY.json records object/working-file presence and SHA256 without copying payloads:

- **MISSING in all four inspected box checkouts/trees:** handoff tropius_prior.json, ninetales_cdp_targets.json, tropius_sold_page.html under reports/artifacts/inter_card_provider_handoff_closure/fixtures. Historical13 common failures remain unclosed. Prior notes said files existed on a desktop; corrected desktop is now identified/reachable, but these fixture payloads were not inspected or transferred; their PC presence remains unverified. An earlier allowed-root transfer was refused; no workaround/retry/transfer occurred in P0.
- **PRESENT repository fixtures:** tests/fixtures/ebay_sold/bloodmoon_ursaluna_54_sold_fixture.html and unicode_capture/unicode_sold_fixture.html/.meta.json; exact per-pin hashes in inventory. Presence is not legal clearance or live-page provenance. No payload in zip.
- **INLINE SYNTHETIC:** BLOODMOON_FIXTURE_HTML and constants in the six independent suites/probe. Inventory records definition names/lines/AST digests and owning file hash, not full values. Source functions use mocks and constructed text/forms/resources; no new capture is needed to replay them after A approval.

Capture date, capture operator/consent, full original source URL/licensing and redaction chain for the three missing originals are **UNKNOWN**. Do not invent provenance from a filename. Author/source identity for synthetic tests is the archived independent QA suite; third-party page/trademark derivation and public redistribution rights are not independently established. No license grant inferred from private repository access. Root/tree license markers are separately listed if found in BASELINE_LICENSE_MARKERS.json; absence does not settle legal rights.

## Disputed semantics — explicit, not waivers

- **Nine optional controls:** exact IDs/old expected success/observed auth/rationale in each matching manifest row (D-01..D-09). Andy's accepted rule settles benign nonblocking prompt on complete page: it must not stop the page, and must never waive separate auth. P1 must establish bounded region/current completeness using versioned fixture metadata. If original fixture lacks those facts, label inadequate evidence versus a policy defect explicitly; preserve original raw oracle and receipt. No silent assertion rewrite, removal or waiver.
- **HTML-only success controls:** review every case marked HTML_ONLY_ASSURANCE_REVIEW. Source-only apparent health no longer authorizes price; an old helper-level success assertion may remain a provisional content observation but cannot be treated as R6 permission. Record old assertion, missing assurance, new contract mapping, supporting fixture and explicit accepted disposition. Do not quietly make a security test benign by deleting credentials/challenge copy.
- **Security-critical cases:** genuine auth+optional, challenge+Sold, access403, early empty-body downgrade and poisoned-tab paths remain mandatory blockers. No waivers for accepted comps/price from any of them. All40 R5 auth composition/provider failures must be retained. Four hidden-source false stops are separate availability regressions, not justification to weaken auth.
- **Fixture failures:** execute all21 handoff tests with authorized fixture-present data in future A checkpoint (old8pass+13missing). Synthetic substitutes may add coverage but do not erase13 missing originals. If recovery remains blocked, stop for owner decision; no acceptance by omission.

## Recommended privacy/retention policy — NOT executed

Classification: internal synthetic fixtures/test metadata may enter the reviewed private repo; captured HTML/UIA/clipboard/profile-derived evidence is RESTRICTED until reviewed. Never include cookies, credentials, storage, payment/contact/account details, personal browser state or unrelated files. Redact usernames/email/account IDs, session URLs/tokens, query user identifiers, device/session details. Preserve auth semantics via synthetic replacements rather than leaking real credential fields. Page titles/listing photos/content can carry third-party rights; prefer minimal synthetic derivatives and owner/legal clearance for original reuse.

Keep original restricted fixtures encrypted/private, owner+named independent-QA access only, with source/operator/date/consent/license, hash and redaction-transform manifest. Proposed raw diagnostic TTL48hours, maximum7days only for an explicitly retained incident; minimized failure/audit metadata30days; signed/hash receipt manifests90days. These are recommendations needing later policy implementation approval, not deletes/TTL jobs executed now. Preserve frozen synthetic regression identities long-term; retained originals only where lawful and necessary. Legal/security hold overrides deletion schedule.

Durable safety/usage ledger is NOT ordinary diagnostic TTL: retain enough rolling24h history plus required audit, and never expire an active HARD_STOP or reset budget to comply with a retention proposal. Failure metadata must not freshen retained prices. The pack contains metadata/hashes only; no restricted transfer/capture occurred.
