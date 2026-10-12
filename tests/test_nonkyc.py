from bitsbots.nonkyc import NonKycClient


def test_parses_documented_websocket_candle_shape() -> None:
    payload = [
        {
            "datetime": "2026-09-16T23:00:00.000Z",
            "open": "0.12043",
            "close": "0.12050",
            "min": "0.11984",
            "max": "0.12100",
            "volume": "1.1266",
        }
    ]
    candle = NonKycClient._parse_candles("IMX/USDT", 60, payload)[0]
    assert candle.timestamp == 1789599600000
    assert candle.high == 0.121
    assert candle.low == 0.11984
    assert candle.close == 0.1205
