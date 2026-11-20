
set -e
mkdir -p /tmp/tmpaf500rsf
python3 - <<'PY'
import json
from pathlib import Path
p = Path('/tmp/tmpaf500rsf') / "wsl-shared-dir-proof-1.SEARCH_SUBMISSION_STARTED.json"
p.write_text(json.dumps({
  "event": "SEARCH_SUBMISSION_STARTED",
  "attemptId": "wsl-shared-dir-proof-1",
  "timestamp": "2026-10-02T00:00:00Z",
  "queryFingerprint": "deadbeef",
  "query": "offline-proof",
  "pid": 1,
  "priceKeyId": "pk-proof",
}, indent=2) + "\n", encoding="utf-8")
print("WROTE", p)
PY
