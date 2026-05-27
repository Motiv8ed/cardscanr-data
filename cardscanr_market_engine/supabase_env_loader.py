import os
import json
from pathlib import Path

def load_supabase_env(local_path: str = "supabase_env.local.json") -> None:
    """
    Loads SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY from a local JSON file if not already set in the environment.
    Does not overwrite existing env vars. Never prints secrets. Safe for local dev only.
    """
    if os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_ROLE_KEY"):
        return  # Already set, do nothing
    path = Path(local_path)
    if not path.exists():
        return  # No local config, skip
    try:
        with path.open("r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception:
        return  # Invalid JSON, skip
    if not os.getenv("SUPABASE_URL") and config.get("SUPABASE_URL"):
        os.environ["SUPABASE_URL"] = config["SUPABASE_URL"]
    if not os.getenv("SUPABASE_SERVICE_ROLE_KEY") and config.get("SUPABASE_SERVICE_ROLE_KEY"):
        os.environ["SUPABASE_SERVICE_ROLE_KEY"] = config["SUPABASE_SERVICE_ROLE_KEY"]
    # Never print or log secrets
