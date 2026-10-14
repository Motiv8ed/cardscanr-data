# Future production zh-cn identity crosswalk (NOT this PR)

Status: **planning only**. Merging the metadata RC PRs must not replace the
production zh-cn pack, rewrite collection rows, or publish a new pack.

## Two identity spaces

| Space | Source | Count | ID shape |
| --- | --- | --- | --- |
| Production pack | `artifacts/build98_zhcn/zh-cn.sqlite` | **41,185** | Provider-native printings (`ptcg-chs` / `pokemon-tcg-kb` style) |
| RC catalogue | isolated worktree `cards.jsonl` zh-Hans rows | **14,923** | CardScanR `pokemon\|zh-Hans\|…` canonical printings |

Measured (see `PROD_PACK_COMPATIBILITY.json`):

- Exact `canonicalPrintingId` overlap: **0**
- Soft set-code + collector overlap: **875**
- Production pack file untouched by RC tooling: **yes**

Because exact ID overlap is zero, a naïve “install RC pack as zh-cn” cutover
would **orphan every existing collection binding** that stores production pack
IDs. Soft overlaps (875) are candidate *links*, not proof of sameness across
variants, languages, or ownership rows.

## Safe future cutover requirements

1. **Never delete or rewrite** the 41,185 production printing IDs while any
   user collection, cloud sync row, export, or offline cache may still reference
   them.
2. **Additive alias table** only: map `productionCanonicalPrintingId` ↔
   `rcCanonicalPrintingId` (and optional set/collector keys) without changing
   the owned ID that collections persist.
3. **Variant-aware matching** for the 875 soft overlaps: require set + collector
   **and** variant/finish/language evidence before linking; leave ambiguous rows
   unlinked.
4. **Dual-read period**: lookups accept both ID spaces; writes continue to use
   the ID the collection already stores.
5. **Ownership persistence tests**: migrate/replay a fixture of owned cards
   across pack versions; assert zero lost `canonicalPrintingId` values and
   stable ownership counts.
6. **Pack publish is a separate release**: new CDN/R2 pack + manifest + app
   selector enablement only after alias coverage thresholds and QA sign-off.
7. **Feature flags stay off until cutover**: `ENABLE_CN_KR_B1_METADATA` and
   public zh language advertisement remain disabled by default.

## What this metadata PR is allowed to do

- Land RC tooling, committed reproduce inputs, and QA-only packs locally.
- Document PokéCardex attribution.
- Prove EN/JA preservation and placeholder-only artwork for *new* RC rows.

## What this metadata PR must not do

- Replace `build98_zhcn` / live zh-cn pack.
- Change `public/` CDN manifests or promote RC sqlite to Pages/R2.
- Enable Chinese in public selectors or production catalogue modes.
- Remap or overwrite user collection identities.
