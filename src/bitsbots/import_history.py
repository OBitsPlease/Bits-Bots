import argparse
import csv
from datetime import datetime, timezone
from pathlib import Path

from .config import SETTINGS, SUPPORTED_PERIODS
from .models import Candle
from .storage import Storage


def parse_timestamp(value: str) -> int:
    try:
        numeric = float(value)
        return int(numeric if numeric >= 10_000_000_000 else numeric * 1000)
    except ValueError:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)


def import_csv(symbol: str, period: int, path: Path, storage: Storage) -> int:
    if period not in SUPPORTED_PERIODS:
        raise ValueError(f"Period must be one of {SUPPORTED_PERIODS}")
    candles: list[Candle] = []
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required = {"timestamp", "open", "high", "low", "close", "volume"}
        if not required.issubset(reader.fieldnames or []):
            missing = sorted(required.difference(reader.fieldnames or []))
            raise ValueError(f"Missing required CSV columns: {', '.join(missing)}")
        for row in reader:
            candles.append(
                Candle(
                    symbol=symbol.upper(),
                    period=period,
                    timestamp=parse_timestamp(row["timestamp"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                )
            )
    storage.save_candles(candles)
    return len(candles)


def run() -> None:
    parser = argparse.ArgumentParser(description="Import OHLCV history into BitsBots")
    parser.add_argument("symbol", help="Market symbol, for example BTC/USDT")
    parser.add_argument("period", type=int, help="Candle period in minutes")
    parser.add_argument("csv_path", type=Path)
    args = parser.parse_args()
    storage = Storage(SETTINGS.database_path)
    try:
        count = import_csv(args.symbol, args.period, args.csv_path, storage)
    finally:
        storage.close()
    print(f"Imported {count} candles for {args.symbol.upper()} ({args.period}m)")


if __name__ == "__main__":
    run()

