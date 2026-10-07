# Daily owned pricing — paced enable gate

**Verdict:** `PARTIAL`  
**Flag:** `reports/runtime/owned_daily_full_enable.flag` = **false** (not enabled)

## Gate (real browser attempts)

| Check | Target | Result |
| --- | --- | --- |
| CHALLENGE_REQUIRED | 0 | 0 PASS |
| eBay-primary overwrites | 0 | 0 PASS |
| Healthy market checks | ≥85% | 46.2% FAIL |
| Temporary browser failure | ≤15% | 46.2% FAIL |
| Last-good retained on failure | always | PASS |
| Paced capacity vs due | capacity > due | 215 > 172 PASS |

## Pacing (after one evidence-based tighten)

- Normal delay: **90s**
- Failure backoff: **180 → 270 → 360s** (bounded)
- Session rest: every **3** checks + **180s**
- Concurrency: **1**

## Why withheld

Adjusted pacing cut probe browser-fail from 75% → 37.5%, but the final 25-job paced pilot stayed at **46% healthy / 46% browser SORRY** on real attempts — too unstable for unattended full enable.
