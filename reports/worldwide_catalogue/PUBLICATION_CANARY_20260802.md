# Worldwide publication canary

An acquisition-stage catalogue snapshot was exported through the complete publication contract and
then built into the application search database. This is validation evidence, not a production release.

- Staging database SHA-256: `4ccba7607007a8d369aaed03bbe603449913a2b50e0135b533c95f958bbac588`
- Staging database bytes: 1,658,204,160
- Printings exported/indexed: 206,445
- Card variants exported: 343,530
- Product variants exported: 3,702
- Product image candidates exported: 5,859
- Explicit unresolved items exported: 109,066
- Search database bytes: 499,146,752
- Search database SHA-256: `013672815d0caa93d3e668f2a38190371ea23f96e2943ac5bf47077fab6ae139`
- SQLite integrity: PASS
- Foreign keys: PASS (0 failures)
- Search FTS row parity: PASS (206,445 / 206,445)
- Duplicate canonical printing IDs: 0
- Authenticated or secret-bearing image URLs: 0
- Search verification: PASS

The canary intentionally exported zero app-eligible direct images because no staging image candidate
yet satisfies both the technical-verification and rights gates. Candidate and blocked image evidence
remains in the acquisition database and product-image artifact rather than being promoted unsafely.

This canary is superseded by a fresh version after the active official regional collectors and image
reconciliation gates complete.
