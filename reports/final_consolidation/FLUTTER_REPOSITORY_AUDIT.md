# Flutter Repository Audit

Generated: 2026-08-03 (live verification)

## Identity

| Field | Value |
|---|---|
| Worktree | `D:\CardScanR_worktrees\flutter_cloudflare_catalogue_packs_20260803` |
| Repository root | same (linked to `D:\Card Scanner App`) |
| Remote | `origin` → `https://github.com/Motiv8ed/Card-Scanner-App.git` |
| Remote repository | `Motiv8ed/Card-Scanner-App` |
| Current branch | `feature/cloudflare-catalogue-packs-20260803` |
| Feature HEAD | `75cb09b0567d3af999dc0170174a101bf68dd237` |
| Local `main` | `b5149b5565cbd3a697747bf7d3f0512a31aaeffb` |
| `origin/main` | `b5149b5565cbd3a697747bf7d3f0512a31aaeffb` |
| Fast-forward into main | **YES** (`main` is ancestor of feature) |
| Feature on remote | **NO** (local-only) |
| Commits after known `75cb09b` | **0** |
| Feature commits not in main | **14** |
| Main commits not in feature | **0** |
| Tags | none |
| Open PRs | unknown (`gh` CLI not installed); GitHub API not queried from this host yet |

## Feature commits ahead of main

1. `e126ca7` checkpoint before checking out main
2. `f4e0268` Add reference-counted collection image pinning and cache controls.
3. `1bebba3` Wire collection image pinning into real inventory workflows.
4. `5b36b54` Add production-path pinning tests and integration reports.
5. `f16d3ff` Document pinning integration suite results and remaining blockers.
6. `ccc84be` Add owned sealed-product inventory with pin integration.
7. `ef0f76b` Add viewport-aware thumbnail request cancellation and dedupe.
8. `0431951` Align image-cloud tests and goldens with Cloudflare-native display policy.
9. `fb22627` Refresh collection goldens for intentional ArtworkUnavailable placeholder chrome.
10. `d07196f` Document pinning finish baseline, sealed ownership, suite inventory, and emulator native-lib repair.
11. `196b73e` Fix sealed entry points, dark list contrast, and catalogue manifest fallback.
12. `686b023` Update collection goldens for sealed empty-state entry and refresh pinning finish reports.
13. `a77f9b2` Complete pinning finish acceptance evidence: full suite pass and emulator QA.
14. `75cb09b` Ignore local canary catalogue SQLite artifacts under pinning finish reports.

## Original checkout

| Field | Value |
|---|---|
| Path | `D:\Card Scanner App` |
| Branch | `main` |
| HEAD | `b5149b55…` |
| Owner uncommitted changes | **NO** (clean) |

## Worktrees

14 registered worktrees (see `REMOTE_BRANCH_INVENTORY.json`). Primary + feature + multiple historical customer-sync / Play / QA worktrees.

## Branch classification summary

| Class | Remote | Local |
|---|---:|---:|
| main | 1 | 1 |
| fully_merged | 5 | 115 |
| unique_commits | 90 | 9 |
| obsolete_worktree | 1 | 7 |
| active_owner_work | 0 | 6 |
| unknown | 0 | 0 |

### Active owner / protected local branches (do not delete until tagged or merged)

- `feature/cloudflare-catalogue-packs-20260803` (this release)
- `feature/play-compliance-v1`
- `release/play-internal-real-screenshots-20260801_072321`
- `release/play-priced-real-screenshots-20260802_073615`
- `fix/binder-cloud-uuid-mapping-phase3f`
- `qa/scanner-screenshot-v3`

### Unique remote branches requiring archive tags before deletion

Most `origin/copilot/*` tips are **not** ancestors of `origin/main` (stale remote PR tips). Local same-named branches are largely fully merged. Before remote deletion, each unique tip must be archived with an annotated tag.

Notable unique remotes:

- `origin/codex/fix-dashboard-checking-state-and-price-history` (70 unique)
- `origin/rescue/main-local-before-reconcile-20260721` (1 unique)
- ~88× `origin/copilot/*` unique tips

## Dirty state on feature worktree

Uncommitted report/UI evidence under `card_scanner_app/reports/collection_image_pinning_finish/` plus generated registrant churn. Must be committed or excluded before merge.

## Merge readiness (pre Phase 5–7)

- FF merge into `main` is possible after tests pass.
- Production packed catalogue activation and canary-fallback policy changes must land on this feature branch first.
- No force-push required.
