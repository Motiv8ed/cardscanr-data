# PokéCardex WebP disjoint audit

Generated: 2026-10-14T07:18:41Z

totalWebp(8932) = standardCanonical(8920) + nonStandard(0) + orphanExtras(12)

standardMeta(8931) = onDiskRows(8922) + 404(9) + missing(0); uniqueCanonicalPaths=8920; sharedPaths=2

- Balanced on-disk partition: **True**
- Balanced standard inventory: **True**
- Shared canonical paths: **2**

totalWebp 8932 = uniqueStd 8920 + nonStd 0 + orphan 12. Row-level std on-disk 8922 can exceed uniqueStd when 2 paths are shared by multiple metadata rows.

Canonical standard display path uses collector_staging_token (numeric collector or s{sort} for energies). Orphans are leftover folders from earlier path normalisations (e.g. __v1, gra_v1) and are disjoint from canonical standard paths. If onDiskCanonicalRows > unique paths, multiple metadata rows share one display file.
