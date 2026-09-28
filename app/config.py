from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    litellm_master_key: str | None = None
    dashboard_password: str | None = None
    dashboard_session_hours: int = 8
    litellm_port: int = 4000
    litellm_host: str = "0.0.0.0"
    database_url: str = "sqlite:///data/gateway.db"
    global_rate_limit: int = 1000
    user_rate_limit: int = 100
    request_timeout: int = 60
    config_path: str = "config/litellm_config.yaml"
    cors_origins: str = "*"
    
    # Browser Automation Settings
    browser_enabled: bool = True
    browser_headless: bool = False
    chrome_user_data_dir: str = "data/chrome_profile"
    browser_cdp_url: str | None = None
    
    # Tunnel & Fallback Settings
    ngrok_authtoken: str | None = None
    enable_api_keys: bool = False
    fallback_enabled: bool = True

    @property
    def database_path(self) -> Path:
        prefix = "sqlite:///"
        if not self.database_url.startswith(prefix):
            raise ValueError("This starter supports SQLite only; use sqlite:///path.db")
        return Path(self.database_url.removeprefix(prefix))

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def operator_password(self) -> str | None:
        return self.dashboard_password or self.litellm_master_key


ENV_VALUE = re.compile(r"^os\.environ/([A-Z0-9_]+)$")


def _resolve_env(value: Any) -> Any:
    if isinstance(value, str):
        match = ENV_VALUE.match(value)
        return os.environ.get(match.group(1), "") if match else value
    if isinstance(value, dict):
        return {key: _resolve_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_env(item) for item in value]
    return value


def load_gateway_config(path: str) -> dict[str, Any]:
    config_file = Path(path)
    if not config_file.exists():
        raise FileNotFoundError(f"Gateway configuration not found: {config_file}")
    return _resolve_env(yaml.safe_load(config_file.read_text(encoding="utf-8")) or {})


@lru_cache
def get_settings() -> Settings:
    return Settings()
