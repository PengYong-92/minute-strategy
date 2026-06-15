import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from app.backtest import load_klines_from_zip
from app.models import Kline


BINANCE_VISION_SPOT_BASE_URL = "https://data.binance.vision/data/spot"
Downloader = Callable[[str, Path, float], None]


@dataclass(frozen=True)
class WarmupConfig:
    symbol: str
    data_dir: Path = Path("data")
    interval: str = "1m"
    months: int = 3
    include_current_month_daily: bool = True
    today: date | None = None
    base_url: str = BINANCE_VISION_SPOT_BASE_URL
    timeout: float = 20.0
    downloader: Downloader | None = None


@dataclass(frozen=True)
class WarmupReport:
    status: str
    symbol: str
    interval: str
    data_dir: str
    loaded_klines: int = 0
    cached_files: list[str] = field(default_factory=list)
    downloaded_files: list[str] = field(default_factory=list)
    missing_files: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    from_time_ms: int | None = None
    to_time_ms: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class _WarmupTarget:
    url: str
    path: Path


def warmup_history(config: WarmupConfig) -> tuple[list[Kline], WarmupReport]:
    symbol = config.symbol.upper()
    interval = config.interval
    data_dir = Path(config.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    cached_files: list[str] = []
    downloaded_files: list[str] = []
    missing_files: list[str] = []
    errors: list[str] = []

    targets = _warmup_targets(config, symbol, interval, data_dir)
    downloader = config.downloader or _download_file

    for target in targets:
        if target.path.exists():
            cached_files.append(target.path.name)
            continue
        try:
            downloader(target.url, target.path, config.timeout)
            downloaded_files.append(target.path.name)
        except Exception as exc:  # noqa: BLE001 - 公开历史数据缺失不能中断实盘监控。
            missing_files.append(target.path.name)
            errors.append(f"{target.path.name}: {exc}")

    klines_by_open_time: dict[int, Kline] = {}
    for target in targets:
        if not target.path.exists():
            continue
        try:
            for item in load_klines_from_zip(target.path):
                klines_by_open_time[item.open_time] = item
        except Exception as exc:  # noqa: BLE001 - 单个缓存文件损坏不能影响其他缓存。
            errors.append(f"{target.path.name}: {exc}")

    klines = sorted(klines_by_open_time.values(), key=lambda item: item.open_time)
    status = _status(bool(klines), bool(errors))
    report = WarmupReport(
        status=status,
        symbol=symbol,
        interval=interval,
        data_dir=str(data_dir),
        loaded_klines=len(klines),
        cached_files=cached_files,
        downloaded_files=downloaded_files,
        missing_files=missing_files,
        errors=errors,
        from_time_ms=klines[0].open_time if klines else None,
        to_time_ms=klines[-1].close_time if klines else None,
    )
    return klines, report


def _warmup_targets(config: WarmupConfig, symbol: str, interval: str, data_dir: Path) -> list[_WarmupTarget]:
    anchor = config.today or datetime.now(timezone.utc).date()
    targets: list[_WarmupTarget] = []
    for year, month in _previous_months(anchor, max(config.months, 0)):
        filename = f"{symbol}-{interval}-{year:04d}-{month:02d}.zip"
        url = f"{config.base_url}/monthly/klines/{symbol}/{interval}/{filename}"
        targets.append(_WarmupTarget(url=url, path=data_dir / filename))

    if config.include_current_month_daily:
        day = anchor.replace(day=1)
        last_complete_day = anchor - timedelta(days=1)
        while day <= last_complete_day:
            filename = f"{symbol}-{interval}-{day:%Y-%m-%d}.zip"
            url = f"{config.base_url}/daily/klines/{symbol}/{interval}/{filename}"
            targets.append(_WarmupTarget(url=url, path=data_dir / filename))
            day += timedelta(days=1)
    return targets


def _previous_months(anchor: date, count: int) -> list[tuple[int, int]]:
    year = anchor.year
    month = anchor.month - 1
    months: list[tuple[int, int]] = []
    for _ in range(count):
        if month == 0:
            year -= 1
            month = 12
        months.append((year, month))
        month -= 1
    return list(reversed(months))


def _download_file(url: str, target: Path, timeout: float) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_suffix(target.suffix + ".tmp")
    request = urllib.request.Request(url, headers={"User-Agent": "codex-event-monitor/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            temp_path.write_bytes(response.read())
        temp_path.replace(target)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _status(has_klines: bool, has_errors: bool) -> str:
    if has_klines and has_errors:
        return "PARTIAL"
    if has_klines:
        return "READY"
    if has_errors:
        return "ERROR"
    return "EMPTY"
