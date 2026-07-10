# TCGdex Metadata Execution Summary

Classification: **PARTIAL**

- Provider languages requested: 17
- Unique provider sets cached: 1,495
- Canonical printing groups: 117,665
- Network requests across resumable attempts: 1,516
- Downloaded metadata: 11,851,182 bytes
- Paid API cost: US$0
- R2 writes: 0
- Permanent provider failures: 0

The first attempt was safely resumed after a transient Windows report-file lock. Five apparent 404s for set IDs
containing `+` were traced to missing URL path encoding, fixed, and successfully retrieved. TCGdex also returned
the Simplified Chinese set ID `CSV1C` twice; it is deduplicated and quarantined as a source anomaly.

