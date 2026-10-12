from dataclasses import dataclass, field
import os
from pathlib import Path
import sys


def default_database_path() -> Path:
    if getattr(sys, "frozen", False) and sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            raise RuntimeError("LOCALAPPDATA is required for the packaged Windows application")
        return Path(local_app_data) / "BitsBots" / "bitsbots.db"
    return Path("data/bitsbots.db")


@dataclass(frozen=True)
class Settings:
    api_base_url: str = "https://api.nonkyc.io/api/v2"
    websocket_url: str = "wss://api.nonkyc.io"
    database_path: Path = field(default_factory=default_database_path)
    quote_asset: str = "USDT"
    quote_assets: tuple[str, ...] = ("USDT", "USDC")
    scan_market_limit: int = 20
    minimum_quote_volume: float = 50.0
    stale_market_minutes: int = 180
    candle_limit: int = 100
    scan_interval_seconds: int = 15 * 60
    websocket_timeout_seconds: float = 15.0
    starting_cash: float = 10_000.0
    risk_per_trade: float = 0.005
    max_position_fraction: float = 0.10
    max_open_positions: int = 5
    fee_rate: float = 0.002
    slippage_rate: float = 0.001
    entry_score: float = 68.0
    exit_score: float = 38.0
    session_lifetime_days: int = 14
    session_cookie_name: str = "bitsbots_session"


SETTINGS = Settings()
SUPPORTED_PERIODS = (15, 60, 240, 1440)
