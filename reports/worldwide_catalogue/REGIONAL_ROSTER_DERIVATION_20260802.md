# Regional roster derivation

Generated on 2026-08-02 from the pinned TCGdex staging snapshot.

CardScanR created provisional printing skeletons only when all of the following were true:

- the source set payload explicitly supplied a non-empty local set name for the target language;
- the target release had a positive official card count and no existing printings;
- the same canonical set had a populated English/INTL release;
- the English roster row count exactly equalled the target release's official count.

The derivation does not create card localisations or image candidates. Each derived printing has one
`regional-variant-unclassified` variant and three open reconciliation items: local metadata, physical
variant classification, and an exact regional image. Structural metadata copied from the sibling
roster remains explicitly `provisional`.

| Language | Releases | Provisional printings |
|---|---:|---:|
| German (`de`) | 23 | 606 |
| Spanish (`es`) | 30 | 1,582 |
| Italian (`it`) | 47 | 2,296 |
| Dutch (`nl`) | 3 | 228 |
| Polish (`pl`) | 1 | 130 |
| Portuguese, region unresolved (`pt`/`INTL`) | 8 | 718 |
| Russian (`ru`) | 2 | 185 |
| **Total** | **114** | **5,745** |

The staging database passed `pragma integrity_check` and `pragma foreign_key_check` after import.
No skeleton was created for a count mismatch, for a release without explicit local-set evidence, or
for an official Asia locale currently being collected card-by-card.
