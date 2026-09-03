"""Timestamp parsing."""
from datetime import datetime, timezone


def parse_iso8601(value: str) -> datetime:
    """Parse an ISO 8601 timestamp; a trailing Z means UTC."""
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
