from dataclasses import dataclass, field


@dataclass(frozen=True)
class User:
    id: int
    email: str
    display_name: str
    created_at: int


@dataclass(frozen=True)
class PaperBot:
    id: int
    name: str
    symbol: str
    strategy: str
    risk_level: str
    status: str
    entry_score: float
    max_allocation: float
    created_at: int
    lower_price: float | None = None
    upper_price: float | None = None
    grid_slices: int | None = None
    grid_stop_price: float | None = None
    last_price: float | None = None


@dataclass(frozen=True)
class Market:
    symbol: str
    last_price: float
    high_price: float
    low_price: float
    base_volume: float
    quote_volume: float
    last_trade_at: int
    active: bool


@dataclass(frozen=True)
class Candle:
    symbol: str
    period: int
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class TimeframeScore:
    period: int
    score: float
    reasons: tuple[str, ...]
    candle_count: int


@dataclass(frozen=True)
class Signal:
    symbol: str
    score: float
    price: float
    action: str
    reasons: tuple[str, ...]
    timeframe_scores: tuple[TimeframeScore, ...]
    atr: float
    generated_at: int
    history_quality: str = "limited"


@dataclass
class PaperPosition:
    id: int | None
    symbol: str
    quantity: float
    entry_price: float
    stop_price: float
    target_price: float
    highest_price: float
    opened_at: int
    status: str = "open"
    closed_at: int | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    realized_pnl: float | None = None
    entry_reasons: list[str] = field(default_factory=lambda: list[str]())
    bot_id: int | None = None
    strategy: str = "Multi-TF"
    grid_level: float | None = None
