# Direct image security audit

Classification: **PASS**

Audited 117,665 SQLite rows. Validated public direct: 92,811; missing: 24,848; permanent failures: 6.

No API keys, signed/private URLs, secret query parameters, localhost URLs, non-HTTPS URLs, PokéWallet authenticated hosts, or HTML endpoints reach Flutter. Only validated public direct URLs are populated; other rows render placeholders.
