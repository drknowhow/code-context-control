"""Feature switches read from the environment."""
import os


def bulk_enabled() -> bool:
    """FEATURE_BULK is '1', 'true' or 'yes' (any case)."""
    return os.environ.get("FEATURE_BULK", "").strip().lower() in ("1", "true", "yes")
