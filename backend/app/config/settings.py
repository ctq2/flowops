"""Runtime settings, resolved from CLI flags and the environment.

Kept deliberately small: a settings object that needs its own documentation is a
settings object nobody reads.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_ROOT.parent

DEFAULT_POLICY = BACKEND_ROOT / "config" / "policies" / "default.json"
DEFAULT_CALENDAR = BACKEND_ROOT / "config" / "calendars" / "cn-holidays.json"
WEB_ROOT = REPO_ROOT / "frontend"


def _env_path(name: str, fallback: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else fallback


@dataclass(slots=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8787
    store: str = "sqlite"
    database: Path = field(default_factory=lambda: _env_path("FLOWOPS_DB", REPO_ROOT / "data" / "flowops.db"))
    policy_path: Path = field(default_factory=lambda: _env_path("FLOWOPS_POLICY", DEFAULT_POLICY))
    calendar_path: Path = field(default_factory=lambda: _env_path("FLOWOPS_CALENDAR", DEFAULT_CALENDAR))
    web_root: Path = field(default_factory=lambda: _env_path("FLOWOPS_WEB", WEB_ROOT))
    log_level: str = "info"
    cors_origin: str = "*"
    request_timeout_seconds: float = 30.0

    def describe(self) -> dict[str, object]:
        return {
            "host": self.host,
            "port": self.port,
            "store": self.store,
            "database": str(self.database),
            "policy": str(self.policy_path),
            "web_root": str(self.web_root),
        }
