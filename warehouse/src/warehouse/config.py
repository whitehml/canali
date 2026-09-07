"""Runtime configuration and credentialing.

Everything reaches Postgres over the network protocol, even on localhost via URL.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Read from the environment or a ``.env`` file."""

    model_config = SettingsConfigDict(
        env_prefix="WAREHOUSE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+psycopg://localhost/warehouse"

    payload_root: Path = Path("var/payloads")

    ftc_events_base_url: str = "https://ftc-api.firstinspires.org/v2.0"
    ftc_events_username: str = ""
    ftc_events_token: str = ""

    ftc_events_min_interval_s: float = 0.5

    ftc_events_max_retries: int = 5
    ftc_events_timeout_s: float = 30.0

    ftcscout_url: str = "https://api.ftcscout.org/graphql"
    ftcscout_bulk_timeout_s: float = 300.0

    default_timezone: str = "America/New_York"

    def ftc_events_configured(self) -> bool:
        return bool(self.ftc_events_username and self.ftc_events_token)


def load_settings() -> Settings:
    return Settings()
