"""Settings loaded from YAML."""
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class Settings:
    database_path: str
    oauth2_client_id: str
    oauth2_redirect_uri: str
    session_ttl_seconds: int = 1800


def load_config(path: str | Path) -> Settings:
    """Read settings.yaml and build a Settings object."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    oauth2 = data.get("oauth2", {})
    return Settings(
        database_path=data.get("database_path", "ledger.db"),
        oauth2_client_id=oauth2.get("client_id", ""),
        oauth2_redirect_uri=oauth2.get("redirect_uri", ""),
        session_ttl_seconds=int(data.get("session_ttl_seconds", 1800)),
    )
