"""Configuration loader: YAML + environment variable overrides."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field


class MarketFilterConfig(BaseModel):
    min_volume: int = 100
    min_open_interest: int = 50
    max_days_to_close: int = 30
    status: str = "open"


class ApiConfig(BaseModel):
    production_base_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    demo_base_url: str = "https://demo-api.kalshi.co/trade-api/v2"
    production_ws_url: str = "wss://api.elections.kalshi.com/trade-api/ws/v2"
    demo_ws_url: str = "wss://demo-api.kalshi.co/trade-api/ws/v2"
    read_rate_limit: int = 20
    write_rate_limit: int = 10
    request_timeout: int = 10
    max_retries: int = 3
    retry_backoff_base: float = 1.0


class EngineConfig(BaseModel):
    tick_interval: float = 1.0
    markets: list[str] = Field(default_factory=list)
    market_filter: MarketFilterConfig = Field(default_factory=MarketFilterConfig)


class StrategyParams(BaseModel):
    enabled: bool = False
    markets: list[str] = Field(default_factory=list)
    params: dict = Field(default_factory=dict)


class StrategiesConfig(BaseModel):
    market_maker: StrategyParams = Field(default_factory=StrategyParams)
    mean_reversion: StrategyParams = Field(default_factory=StrategyParams)
    value: StrategyParams = Field(default_factory=StrategyParams)


class RiskConfig(BaseModel):
    max_position_per_market: int = 100
    max_total_exposure_cents: int = 500000
    daily_loss_limit_pct: float = 5.0
    max_drawdown_pct: float = 15.0
    max_open_orders: int = 50
    min_balance_reserve_cents: int = 10000


class DatabaseConfig(BaseModel):
    path: str = "kalshi_bot.db"


class LoggingConfig(BaseModel):
    level: str = "INFO"
    format: Literal["json", "console"] = "console"
    file: str | None = None


class BotConfig(BaseModel):
    environment: Literal["demo", "production"] = "demo"
    api: ApiConfig = Field(default_factory=ApiConfig)
    engine: EngineConfig = Field(default_factory=EngineConfig)
    strategies: StrategiesConfig = Field(default_factory=StrategiesConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    @property
    def base_url(self) -> str:
        if self.environment == "production":
            return self.api.production_base_url
        return self.api.demo_base_url

    @property
    def ws_url(self) -> str:
        if self.environment == "production":
            return self.api.production_ws_url
        return self.api.demo_ws_url

    @property
    def api_key_id(self) -> str:
        val = os.environ.get("KALSHI_API_KEY_ID", "")
        if not val:
            raise ValueError("KALSHI_API_KEY_ID environment variable is required")
        return val

    @property
    def private_key_path(self) -> str:
        val = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
        if not val:
            raise ValueError("KALSHI_PRIVATE_KEY_PATH environment variable is required")
        return val


def load_config(config_path: str = "config/default.yaml") -> BotConfig:
    """Load config from YAML, then overlay environment variable overrides."""
    load_dotenv()

    path = Path(config_path)
    if path.exists():
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
    else:
        raw = {}

    # Environment variable overrides
    env_override = os.environ.get("KALSHI_ENV")
    if env_override and env_override in ("demo", "production"):
        raw["environment"] = env_override

    return BotConfig(**raw)
