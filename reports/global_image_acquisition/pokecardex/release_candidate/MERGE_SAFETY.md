# Merge safety for zh-Hans metadata RC (data PR)

## Cloudflare Pages / live CDN

- Pages build root for this repo is `public/` (see root README).
- This PR does **not** modify any path under `public/`.
- Cloudflare Pages checks on the PR create a **preview** deployment of the
  unchanged `public/` tree; they do not rewrite production pack manifests by
  themselves when `public/` is identical to `main`.
- Merging to `main` may re-publish Pages from `public/`, still without Chinese
  pack or RC catalogue promotion, because those artifacts are not under `public/`.

## Production catalogue / collections

- Live `D:\CardScanR_Data\...\cards.jsonl` is an external reproduce input only.
- Worktree `data/global/catalogue/cards.jsonl` is regenerable / not the live CDN pack.
- Production pack `build98_zhcn/zh-cn.sqlite` (41,185 IDs) is never rewritten by
  RC tools.
- See `FUTURE_IDENTITY_CROSSWALK.md` before any future pack cutover.

## Reproduce inputs retained after scope trim

Required committed inputs (unchanged):

- `catalogue_review/pokecardex_zh_hans_printings_review.jsonl`
- `auto_resolve/auto_resolved.jsonl`
- `zh_hans_catalogue_overlap_matches.jsonl`

Large regenerable acquisition outputs removed from this PR (image manifest,
variant links, visual/webp audits, Korean side-notes). Acquisition *tools*
remain so the pipeline can regenerate those artifacts locally when staging is
present.
