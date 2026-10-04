# EXACT SOLD CONTROL TARGETING CLOSURE

**Task:** CARDSCANR-EXACT-SOLD-CONTROL-TARGETING-CLOSURE
**Verdict:** PASS
**Mode:** OFFLINE ONLY

## Historical Meowth mis-target

- intended: Sold items
- actual: LH_PrefLoc=2
- click: [114, 307]
- exact old mis-target provable: PARTIAL (pixels cannot OCR; URL+fixture prove location-class)
- find shot: linux_sold_nav_1791064288_find.png

## Old locator

`json
{
  "algorithm": [
    "Ctrl+F Sold items (diagnostic only now)",
    "screenshot orange pixel scan left rail",
    "bucket densest cluster centroid",
    "sold_click_coords_valid",
    "xdotool click",
    "wait lh_sold=1"
  ],
  "preClickEvidenceWas": "orange Ctrl+F highlight inside left rail ONLY",
  "uniquelyIdentifiesSoldItems": false,
  "meowthClick": [
    114,
    307
  ],
  "meowthPostUrl": "LH_PrefLoc=2 without LH_Sold=1",
  "findScreenshot": "reports/artifacts/linux_sold_nav_1791064288_find.png"
}
`

Orange highlight in left rail does NOT uniquely identify Sold items.

## New locator

- identity: read-only CDP/DOM exact label
- label: Sold items
- click: element centre via DOM->X11
- pixel fallback: forbidden
- uniqueness required

## Meowth-class replay

`json
{
  "oldHit": "Australia Only",
  "oldUrl": "http://127.0.0.1/local/sch?_nkw=Meowth+56+jungle+Pokemon&LH_PrefLoc=2",
  "newHit": "Sold items",
  "newUrl": "http://127.0.0.1/local/sch?_nkw=Meowth+56+jungle+Pokemon&LH_Sold=1"
}
`

## Readiness

`json
{
  "soldControlIdentityPositive": true,
  "pixelHighlightAuthoritative": false,
  "exactVisibleControlRequired": true,
  "boundingRectCurrentPage": true,
  "x11CoordinateTransformProven": true,
  "locationFilterMisclickRegression": true,
  "singlePhysicalClick": true,
  "postClickSoldVerification": true,
  "unexpectedFilterFailClosed": true,
  "phaseTimeoutRegression": true,
  "controlPlanePersistenceRegression": true,
  "demandSchedulerRegression": true,
  "ok": true,
  "meowthClass": {
    "oldHit": "Australia Only",
    "oldUrl": "http://127.0.0.1/local/sch?_nkw=Meowth+56+jungle+Pokemon&LH_PrefLoc=2",
    "newHit": "Sold items",
    "newUrl": "http://127.0.0.1/local/sch?_nkw=Meowth+56+jungle+Pokemon&LH_Sold=1"
  }
}
`

## Next gate

READY_TO_RETRY_CONTROLLED_25_JOB_ROLLOUT — do not start in this task.
