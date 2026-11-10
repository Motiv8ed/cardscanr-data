# (c) Frozen PageSnapshot/PageDecision — R6-P0-v1

Normative design frozen for separate approval A; NOT existing implementation or test result. Based on accepted R6 and Andy t588u D1–D4. Future change requires versioned reason/diff and owner checkpoint, not silent fixture edits.

## Typed model (declarative specification, no code implemented)

All records deeply immutable: tuples/frozen mappings, no mutable dict/list escape. Serialized child output is untrusted data, not permission. Validate schema/types/limits/canonical serialization before constructing trusted records.

| Field/type | Required rule |
|---|---|
| protocol_version: exact enum R6-P0-v1; capture_id: unique opaque UUID | Unknown version, reused or absent ID cannot mint permission. |
| job_id, price_key_id, attempt_id: opaque nonempty IDs | Exact current request match; never nojob/noattempt fallback for price work. |
| query_fingerprint: SHA256; requested_market: AU; requested_currency: AUD | Canonical request/key identity; structurally validated expected origin/final URL. No URL substring or token-overlap identity substitute. |
| worker_id, lease_id/epoch, lease_expiry: typed envelope | Match active claim/current owner; record only safe IDs or digests, never credentials. Sink checks lease again at commit. |
| browser_session_id, target_id/tab_id, frame_id, document_id/navigation_generation | Exact current target/document; URL equality is not target identity. Prior session/tab not eligible. Adapter lacking capability records UNKNOWN. |
| started_at/completed_at: aware timestamps; monotonic_duration_ms; capture_deadline; observed_navigation_generation before/after | Bounded capture duration/TTL/skew; same generation throughout; snapshot predates decision within approved expiry. UTC wire data is rendered AEST to owner. Proposed TTL30s, capture deadline15s and skew5s are conservative design defaults pending P1 checkpoint; owner may approve tighter, no silent widening. |
| expected_target_url, actual_final_url, actual_title: Observed[str] | Current observation provenance; exact approved HTTPS hostname/market/query policy. Sanitize user/session query parameters for audit. Never stale inventory URL/title fallback. |
| http_status: Observed[integer or UNKNOWN] | Absent is UNKNOWN, not200. Reliable current403 is access stop; unavailable HTTP status alone does not invalidate otherwise adequate browser evidence. |
| visible_text: Observed[UTF8 text]; raw_html: optional Observed[bytes ref] | Representations distinct. Original byte hashes retained; whitespace normalization is a derived matching view only. Raw HTML/script/CSS/comments cannot independently prove visible auth/challenge/SORRY or healthy listings. |
| visible_auth_forms/modals/session/optional-region observations; active_challenge/title/URL/widget; SORRY/title/error; consent: tuples of Fact | Fact = kind + TRUE/FALSE/UNKNOWN + evidence IDs + scoped node/region/role + source/assurance. TRUE requires qualified current support, not raw hidden words. Optional must be verified nonblocking bounded component, never global suffix. |
| inert_item_references: tuple[CaptureItemReference] | Current document/node/item reference and raw text references only; no parsed usable SoldComp, accepted price or aggregate before decision. No stale candidates or previous job listings. |
| body_complete, parser_ready, zero_results, Sold_state: Observed[tristate] | Complete authoritative current body plus verified current structure; process_capture=1, selectors, URL, prior X11 or length alone insufficient. Zero results requires its own complete current structure. Contradictory complete-listings+zero-results is ambiguity. |
| prior_navigation_context: immutable optional reference | Navigation hint only; never current evidence or permission, and never cancels security. |
| payload bindings: hashes/byte lengths of HTML/body/item refs/meta; artifact refs | Hash exact original bytes and validate approved canonical root/no symlink/shared-last escape before consumption. Include manifest binding and reject replacement/replay. Proposed bounds5MiB HTML/1MiB visible text/1000 inert nodes; excess -> incomplete, never silent success truncation. |

Observed[T] fields: value, origin enum **CURRENT_CAPTURE / PRIOR_NAVIGATION / RAW_SOURCE / APPROX_VISIBLE_HTML / UNKNOWN**, representation/capture method, evidence_id, binding IDs, observation time, assurance, completeness/unknown reason. Origin is NOT visibility. Confidence is categorical support annotation, never a weighted override of hard facts or missing proof. Raw source labelled CURRENT_CAPTURE in time is still RAW_SOURCE in representation; preserve both dimensions.

## Visibility per capture path

- **CDP/Playwright DOM:** bounded same-document evaluation where supported; body.innerText plus scoped current form/modal/widget observations and layout/role evidence. Verify target/document generation before/after; capture title/URL/body/item refs consistently. Cross-origin frame, closed shadow, overlay/occlusion or unsupported layout is explicit unknown. Visible badge/resource != blocking active challenge. textContent/ARIA/title candidate text is not automatically user-visible security copy.
- **X11 clipboard:** current attempt nonce/generation handshake, expected browser/focus, URL/title before/after, bounded timing, selected body text consistency, no address-bar/old clipboard reuse. Clipboard alone cannot prove full DOM visibility/current document; unsupported guarantee remains unknown. Empty/garbage copy is incomplete; reliable current title/URL auth remains security even with empty body. No UI navigation or clipboard operation executed in P0.
- **Windows UIA:** sparse/current accessibility text is provisional nav evidence, not authoritative body. Verify script protocol/exit status and target/window identity; require stronger same-document capture for price permission. Unknown external script version -> unsupported, not success.
- **HTML-only:** source extraction strips nonvisible regions for diagnostics, never turns approximate extraction into verified original visibility. Optional future bounded inert local render may attest that render only, network/remote assets/scripts disabled, with assurance clearly scoped; it does NOT prove external CSS/JS visibility of the original page. Without adequate current render attestation -> UNKNOWN/non-price. Old success expectations get explicit approved dispositions, not deletion.

## Authority and decision enum

One pure **classify_snapshot(PageSnapshot) -> PageDecision**. PageDecision contains decision_id/version, snapshot_id+content hash, request/lease/document binding, all reasons/evidence IDs, enum, may_parse_price(default FALSE), action plan, expiry and permitted diagnostics class. Consumers revalidate same decision at parser, pricing and DB handoffs; they do NOT run new classifiers. A flag/boolean, mutable ProviderResult or self-declared authoritative child receipt is insufficient.

| Outcome | Parse price / usable comp / price | Market publication | Audit/failure | Latch/recovery |
|---|---|---|---|---|
| AUTH_REQUIRED | NO/NO/NO | NO | bounded/redacted allowed | Immediate security stop; no auto refresh/retry/clear |
| CHALLENGE_STOP | NO/NO/NO | NO | allowed | Same |
| ACCESS_BLOCKED | NO/NO/NO | NO | allowed | Same, including reliable403 |
| AMBIGUOUS_NO_PRICE | NO/NO/NO | NO | allowed | Security/integrity stop; retain existing latch, no automatic retry |
| SORRY_COOLDOWN | NO/NO/NO | NO | allowed | Original SORRY cooldown+existing transient thresholds; invalidate reuse; next job only if gate permits |
| INCOMPLETE_TRANSIENT | NO/NO/NO | NO | allowed | Invalidate reuse; one transient per attempt; existing3-consecutive/5-hourly threshold; bounded local fresh-surface recovery only when permitted, never security retry |
| VERIFIED_SOLD_RESULTS | ELIGIBLE/ELIGIBLE/ELIGIBLE | ONLY after identity/date/quality/market/source-precedence/current-lease and transactional guards | allowed | Does not auto-clear any existing HARD_STOP; work gate must already permit attempt |
| VERIFIED_NO_RESULTS | NO/NO/NO | No new numeric price/market comps | checked/failure metadata only under no-freshening whitelist | Retain last-good; no automatic latch clear |

Eligibility is not an instruction to compute a price or guarantee sufficient quality; later filters may reject all comps. No usable SoldComp before VERIFIED_SOLD_RESULTS. No price parsing for VERIFIED_NO_RESULTS. Non-PASS cannot reach parser/filter/pricer. Failure cache metadata whitelist may update refresh_status/last_error only if it does not alter value/source/evidence refs/price-observed or freshness timestamps; generic updated_at cannot make old price look fresh. Otherwise use separate failure/job/audit storage. No hidden writes via successful completion or healthy handoff. DB role/transaction changes belong separate future approval, not A.

## Exhaustive mixed-signal table

**MIXED_SIGNAL_OUTCOME_TABLE.csv has4096 rows: every Boolean combination of12 dimensions.** This is a static generated policy specification, not tests or an implemented classifier. Dimensions include every requested auth/challenge/SORRY/incomplete/optional/prior/Sold-looking/hidden-source combination plus binding validity,403, completed current Sold and verified zero-results.

- binding_valid_current covers request/attempt/lease/tab/document/time/hashes/AU/AUD only; it does NOT assert body completeness/visibility. Invalid binding -> AMBIGUOUS_NO_PRICE, preserve unbound observed signals in diagnostics, never claim those are confirmed current auth.
- With valid binding, auth/challenge/SORRY/403 dimensions are independently qualified CURRENT facts. Unknown/unsupported facts remain unknown and cannot support complete document. Missing body visibility -> incomplete/zero eligibility; unresolved security ambiguity -> AMBIGUOUS_NO_PRICE. Table applies after this qualification, not to arbitrary untrusted input flags.
- Precedence: invalid envelope -> ambiguity; otherwise auth > active challenge >403 >SORRY >incomplete > contradictory results/empty -> ambiguity > completed Sold > verified empty > incomplete. All negative reasons retained, even though primary enum is single. Reliable auth+SORRY+incomplete stays AUTH; challenge+listings stays CHALLENGE. Unknown security never becomes convenient success.
- Optional banner, prior state, generic Sold-looking markers and source-only script/CSS/comments NEVER improve outcome/cancel security. Strong current complete structure is a separate required dimension; prior or selectors cannot set it. Hidden tokens never independently create negative or positive facts. An unbounded optional-looking suffix is NOT the verified optional dimension and cannot waive auth.
- Prior context may coexist with healthy CURRENT capture; stale capture cannot: binding becomes false. An old observed wall is not current proof but still cannot price. Zero-results/listings contradiction is non-price. Each row enumerates parse/comp/price/publication/audit/latch/recovery permissions.

No decisions here clear state, operate a browser or authorize any contact. Future implementation must test this specification and preserve actual reason/evidence provenance, not simply trust a CSV lookup.
