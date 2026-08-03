# Data Repository Audit

Generated: 2026-08-03 (live verification)

## Identity

| Field | Value |
|---|---|
| Worktree | `D:\CardScanR_worktrees\worldwide_catalogue_products_20260802` |
| Primary checkout | `D:\cardscanr-data` |
| Remote | `origin` → `https://github.com/Motiv8ed/cardscanr-data.git` |
| Remote repository | `Motiv8ed/cardscanr-data` |
| Current branch | `feature/worldwide-pokemon-catalogue-products-20260802` |
| Feature HEAD | `2bec716705638e737d8ec9f861cfdb5795b6bf04` |
| Local `main` / `origin/main` | `94fe6a41a9c31fa7a66de83626acfca5614702e1` |
| Fast-forward into main | **YES** |
| Feature on remote | **NO** (local-only) |
| Feature commits not in main | **108** |
| Main commits not in feature | **0** |
| Tags | none |
| Open PRs | **0** (GitHub API) |

## Primary checkout owner state

| Field | Value |
|---|---|
| Path | `D:\cardscanr-data` |
| Branch | `main` @ `94fe6a41` |
| Dirty | `.gitignore` CRLF-only noise (`assume-unchanged`); no semantic owner edits |

## Worktrees

| Path | Branch | Classification |
|---|---|---|
| `D:\cardscanr-data` | `main` | main |
| `…\worldwide_catalogue_products_20260802` | feature worldwide… | active_owner_work + unique_commits |
| `…\priced_assets_data_20260802_073615` | release/priced-assets… | fully_merged + obsolete_worktree (dirty WIP: ebay browser provider) |
| `…\security_advisor_history_20260728` | chore/security-advisor… | unique_commits (1 ahead / 1 behind) |

## Branch classification summary

| Class | Remote | Local |
|---|---:|---:|
| main | 1 | 1 |
| fully_merged | 22 | 1 |
| unique_commits | 1 | 2 |
| active_owner_work | 0 | 1 |
| obsolete_worktree | 0 | 1 |
| unknown | 0 | 0 |

### Must retain until tagged/merged

- Feature worldwide catalogue branch (108 unique commits + uncommitted pack tooling)
- `chore/security-advisor-remediation-history` (unique tip on remote)

### Fully merged remotes (delete-eligible after main push + verification)

All listed `origin/codex/*` and `origin/copilot/*` except security-advisor history.

## Uncommitted feature work (must commit before merge)

Modified:

- `tools/rewrite_catalogue_images_to_r2.py`

Untracked (pack / activation tooling + reports):

- `cardscanr_search_index/catalogue_packs.py`
- `tools/build_catalogue_packs.py`
- `tools/publish_catalogue_packs.py`
- `tools/fill_pack_nulls_and_rebuild.py`
- `tools/activate_production_packed_manifest.py`
- `reports/cloudflare_migration/*` (pack architecture, canary4, mirror reports)
- `reports/final_consolidation/*`

## Cloudflare catalogue state (at audit time)

| Object | Status |
|---|---|
| `…/search/catalogue.manifest.json` | HTTP **404** (pre-activation) |
| `…/packs/active/catalogue.packs.manifest.json` | HTTP **200** (canary4 packs) |
| Canary2/3/4 immutable manifests | HTTP **200** retained |
| Production null-filled packs | built locally as `production-packs-20260803` |

## Merge readiness

- FF into `main` possible after committing pack tooling and passing data-platform tests.
- Do not commit downloaded image binaries, runtime SQLite monoliths, secrets, or `cloudflare_env.local.json`.
