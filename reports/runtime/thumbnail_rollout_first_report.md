# Thumbnail Rollout — First Report

- Classification: **PARTIAL**
- Validated matchable total: 36542
- Usable public URL total (estimate): 20359
- Provider failure totals (live sample): {'http_404': 25, 'auth_401_403': 25}
- Proposed 500-card English batch: `reports\runtime\thumbnail_rollout_en_500_manifest.json`
- Batch SHA-256: `2e84741b1921388baac2346387040f65a7dbea0acf11924076809d7ec2438f5e`
- Estimated 500-batch thumb storage: 6504500 bytes
- Estimated full English matchable thumb storage: 304085375 bytes
- Estimated full catalogue matchable thumb storage: 475374878 bytes
- Dry-run stop: False
- Full display-image import run: **False**
- Flutter modified: **False**
- Full English import run: **False**

## Stop gate

Stopped after planning and 500-card dry run. Awaiting approval before execute.

## Unresolved risks

- PokeWallet catalogue URLs require authentication; Manual Add still shows placeholders until Supabase thumbs are wired.
- 38,036 unresolved cards remain outside the validated provider chain.
- Do not auto-proceed from 500-card batch to full English import.
- Catalogue wiring, search-index rebuild, and Flutter checks are deferred until English thumbnail batch approval.
- Ambiguous PokeWallet identities: 7246
