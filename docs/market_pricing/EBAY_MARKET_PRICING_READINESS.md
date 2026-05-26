# eBay Market Pricing Readiness

This document defines planning-only requirements for future sold-listing pricing.
No live scraping/fetching implementation is included here.

## Scope

- Data-side planning only.
- Build contracts/config/reports before any worker implementation.
- Keep legal/terms review as a hard gate before any live collection.

## Planned Sold-Listing Workflow

1. Build market-aware query templates per card and market.
2. Run a planned worker pipeline (future) that fetches only after legal review.
3. Parse sold-listing evidence into normalized records.
4. Score evidence quality and apply outlier filtering.
5. Produce aggregate market statistics and references for app display.

## Market-Specific Query Generation

Inputs for query generation:

- market id and locale
- marketplace source definition
- card identity fields
- card condition/graded filters
- excluded noise terms

Planned output:

- query text
- optional filter params
- expected currency
- market routing hints

## Required Card Identity Fields

- name
- set name
- set id
- collector number
- language
- variant
- condition
- graded/ungraded

## Planned Exclusion Terms

- proxy
- custom
- fake
- digital
- lot
- bundle
- damaged (unless damaged condition is explicitly selected)

## Planned Price Parsing Requirements

For each sold listing record, capture:

- sold price
- shipping
- currency
- date sold
- listing URL
- title
- confidence score

## Future Aggregated Data Model

For a card + market + condition + variant slice, derive:

- average
- median
- low/high
- sample count
- outlier filtering details
- evidence links

## Legal and Terms Caution

Do not add live scraping/fetching until the approach is reviewed against marketplace terms, legal constraints, and operational risk.

## Suggested Next Milestones

1. Finalize query/template schema and confidence model.
2. Finalize data contracts for evidence rows and aggregates.
3. Implement a dry-run parser test harness against synthetic fixtures.
4. Run legal/terms review before enabling any live worker.
