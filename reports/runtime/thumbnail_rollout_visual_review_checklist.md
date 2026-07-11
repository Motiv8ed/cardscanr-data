# Thumbnail Rollout — Visual Review Checklist

**Human visual approval: NOT PROVIDED** (automated checklist only).

## Artefacts to review

- [ ] `reports/runtime/thumbnail_rollout_gate_a_reconciled_contact_sheet.png`
- [ ] `reports/runtime/thumbnail_rollout_gate_b_canary_contact_sheet.png`
- [ ] `reports/runtime/thumbnail_rollout_gate_b_full_contact_sheet.png` (after Gate B full)
- [ ] `reports/runtime/thumbnail_rollout_500_combined_contact_sheet.png` (after reconcile)

## Checklist (per labelled tile)

1. **Language** — label shows `en`; artwork matches English print where distinguishable.
2. **Set identity** — set id in label matches the card’s set (promoted numeric sets for PokeWallet).
3. **Collector number** — including leading zeros and slash forms (`076/131`, `002/189`).
4. **Card name** — artwork matches the named identity (no wrong-print swaps).
5. **Artwork printing** — holo / stamped / cosmos / full-art variants match the identity slug.
6. **Promos and unusual numbering** — promo and energy cards look correct for their numbers.
7. **Replacement cards** — Gate A replacements are present; unresolved me3/me2pt5 originals are listed separately, not as imported thumbs.
8. **PokeWallet promoted cards** — numeric set ids and slash collector numbers render correctly.

## Automated gate

Stop the rollout if automated identity/URL/dimension checks report a likely mismatch.
Do not mark this checklist as human-approved unless a reviewer explicitly signs off.
