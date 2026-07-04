import os
import json
from pathlib import Path

def load_supabase_env(local_path: str = "supabase_env.local.json") -> None:
    """
    Loads SUPABASE_URL and SUPABASE_SECRET_KEY from a local JSON file if not already set in the environment.
    SUPABASE_SERVICE_ROLE_KEY remains supported as a deprecated compatibility fallback.
    Does not overwrite existing env vars. Never prints secrets. Safe for local dev only.
    """
    if os.getenv("SUPABASE_URL") and (os.getenv("SUPABASE_SECRET_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY")):
        return  # Already set, do nothing
    path = Path(local_path)
    if not path.exists():
        return  # No local config, skip
    try:
        with path.open("r", encoding="utf-8-sig") as f:
            config = json.load(f)
    except Exception:
        return  # Invalid JSON, skip
    if not os.getenv("SUPABASE_URL") and config.get("SUPABASE_URL"):
        os.environ["SUPABASE_URL"] = config["SUPABASE_URL"]
    configured_secret = config.get("SUPABASE_SECRET_KEY") or config.get("SUPABASE_SERVICE_ROLE_KEY")
    if configured_secret:
        if not os.getenv("SUPABASE_SECRET_KEY"):
            os.environ["SUPABASE_SECRET_KEY"] = configured_secret
        if not os.getenv("SUPABASE_SERVICE_ROLE_KEY"):
            os.environ["SUPABASE_SERVICE_ROLE_KEY"] = configured_secret
    # Never print or log secrets
