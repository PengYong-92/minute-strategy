from collections.abc import Sequence

from app.models import Kline


def aggregate_klines(klines: Sequence[Kline], timeframe_minutes: int) -> list[Kline]:
    if timeframe_minutes <= 0:
        raise ValueError("timeframe_minutes must be positive")
    window_ms = timeframe_minutes * 60_000
    buckets: dict[int, list[Kline]] = {}
    for kline in klines:
        bucket_open = (kline.open_time // window_ms) * window_ms
        buckets.setdefault(bucket_open, []).append(kline)

    aggregated = []
    expected_rows = timeframe_minutes
    for bucket_open in sorted(buckets):
        rows = sorted(buckets[bucket_open], key=lambda item: item.open_time)
        if len(rows) < expected_rows:
            continue
        rows = rows[:expected_rows]
        if rows[-1].open_time - rows[0].open_time != (expected_rows - 1) * 60_000:
            continue
        aggregated.append(
            Kline(
                open_time=rows[0].open_time,
                open=rows[0].open,
                high=max(item.high for item in rows),
                low=min(item.low for item in rows),
                close=rows[-1].close,
                volume=sum(item.volume for item in rows),
                close_time=rows[-1].close_time,
            )
        )
    return aggregated


def trend_bias(klines: Sequence[Kline], lookback: int = 3) -> float:
    if len(klines) < 2:
        return 0.0
    rows = list(klines[-lookback:])
    if len(rows) < 2 or rows[0].open <= 0:
        return 0.0
    change = (rows[-1].close - rows[0].open) / rows[0].open
    up_closes = 0
    down_closes = 0
    for previous, current in zip(rows, rows[1:]):
        if current.close > previous.close:
            up_closes += 1
        elif current.close < previous.close:
            down_closes += 1
    consistency = (up_closes - down_closes) / max(up_closes + down_closes, 1)
    return change * 100.0 + consistency
