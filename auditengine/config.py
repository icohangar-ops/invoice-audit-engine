"""Environment-based configuration. Secrets come from env vars or a local .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CONFIG_DIR = ROOT / "config"


def _load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()


@dataclass(frozen=True)
class Settings:
    precoro_token: str = field(default_factory=lambda: os.environ.get("PRECORO_TOKEN", ""))
    precoro_email: str = field(default_factory=lambda: os.environ.get("PRECORO_EMAIL", ""))
    precoro_base_url: str = "https://api.precoro.com"
    # Precoro enforces a route-based limit of ~1 request/minute.
    precoro_min_interval_s: float = 62.0
    db_path: Path = DATA_DIR / "audit.db"


settings = Settings()
DATA_DIR.mkdir(exist_ok=True)
