from dataclasses import asdict, dataclass
from typing import Optional


@dataclass(frozen=True)
class Kline:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class FearGreedContext:
    value: int
    classification: str
    average_30d: float = 0.0
    trend: str = "unknown"
    updated_at_ms: int = 0
    source: str = "feargreed"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Signal:
    direction: str
    timeframe_minutes: int
    level: str
    reason: str
    price: float
    open_time: int
    volume_ratio: float = 0.0
    price_position: float = 0.5
    price_change_pct: float = 0.0
    score: float = 0.0
    threshold: float = 0.0
    volume_threshold: float = 1.5
    move_threshold_pct: float = 0.0
    close_strength: float = 0.5
    analysis_window_minutes: int = 0
    threshold_window_minutes: int = 0
    threshold_segment: str = "GLOBAL"
    mtf_10m_bias: float = 0.0
    mtf_30m_bias: float = 0.0
    macd_histogram: float = 0.0
    macd_histogram_delta: float = 0.0
    rsi: float = 50.0
    bollinger_position: float = 0.5
    bollinger_width: float = 0.0
    indicator_profile_segment: str = "GLOBAL"
    indicator_profile_sample_size: int = 0
    rsi_lower_threshold: float = 35.0
    rsi_upper_threshold: float = 70.0
    bollinger_lower_threshold: float = 0.35
    bollinger_upper_threshold: float = 0.85
    macd_histogram_threshold: float = 0.0
    macd_delta_threshold: float = 0.0
    fear_greed_value: Optional[int] = None
    fear_greed_classification: str = ""
    fear_greed_trend: str = ""
    fear_greed_average_30d: float = 0.0
    fear_greed_adjustment: float = 0.0
    session_allowed: bool = False
    session_sample_size: int = 0
    session_win_rate: float = 0.0
    session_ev: float = 0.0
    session_edge_min: float = 0.0
    regime: str = "UNKNOWN"
    risk_flags: str = ""
    strategy_family: str = "unknown"
    strategy_tag: str = "unknown"
    observe_direction: str = ""
    observe_only: bool = False
    profile_key: str = ""
    daily_profile_selected: bool = False
    daily_profile_version: str = ""
    wave_state: str = "UNKNOWN"
    wave_raw_state: str = "UNKNOWN"
    wave_window: int = 0
    wave_efficiency: float = 0.0
    wave_direction_ratio: float = 0.0
    wave_atr_strength: float = 0.0
    wave_confirmations: int = 0
    wave_confirmed_at: int = 0
    wave_batch_id: str = ""
    wave_guard_mode: str = "NORMAL"
    wave_guard_status: str = "UNKNOWN"
    wave_guard_reason: str = ""

    @property
    def actionable(self) -> bool:
        return self.direction in {"LONG", "SHORT"} and abs(self.score) >= self.threshold

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SimulatedOrder:
    id: int
    direction: str
    timeframe_minutes: int
    level: str
    reason: str
    entry_price: float
    opened_at: int
    expires_at: int
    threshold_segment: str = "GLOBAL"
    score: float = 0.0
    threshold: float = 0.0
    session_allowed: bool = False
    session_sample_size: int = 0
    session_win_rate: float = 0.0
    session_ev: float = 0.0
    session_edge_min: float = 0.0
    regime: str = "UNKNOWN"
    strategy_family: str = "unknown"
    strategy_tag: str = "unknown"
    profile_key: str = ""
    daily_profile_selected: bool = False
    daily_profile_version: str = ""
    stake: float = 10.0
    win_return: float = 18.0
    stake_progression_step: int = 1
    status: str = "OPEN"
    result: Optional[str] = None
    exit_price: Optional[float] = None
    settled_at: Optional[int] = None
    pnl: float = 0.0
    stake_progression_source_order_id: Optional[int] = None
    stake_progression_version: str = ""
    wave_state: str = "UNKNOWN"
    wave_raw_state: str = "UNKNOWN"
    wave_window: int = 0
    wave_efficiency: float = 0.0
    wave_direction_ratio: float = 0.0
    wave_atr_strength: float = 0.0
    wave_confirmations: int = 0
    wave_confirmed_at: int = 0
    wave_batch_id: str = ""
    wave_guard_mode: str = "NORMAL"
    wave_guard_status: str = "UNKNOWN"
    wave_guard_reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ObservationSignal:
    observation_key: str
    strategy_family: str
    strategy_tag: str
    direction: str
    timeframe_minutes: int
    level: str
    reason: str
    entry_price: float
    opened_at: int
    expires_at: int
    threshold_segment: str = "GLOBAL"
    score: float = 0.0
    threshold: float = 0.0
    edge: float = 0.0
    regime: str = "UNKNOWN"
    source_decision: str = ""
    observe_only: bool = True
    status: str = "OPEN"
    result: Optional[str] = None
    exit_price: Optional[float] = None
    settled_at: Optional[int] = None
    pnl: float = 0.0
    wave_state: str = "UNKNOWN"
    wave_raw_state: str = "UNKNOWN"
    wave_window: int = 0
    wave_efficiency: float = 0.0
    wave_direction_ratio: float = 0.0
    wave_atr_strength: float = 0.0
    wave_confirmations: int = 0
    wave_confirmed_at: int = 0
    wave_batch_id: str = ""
    wave_guard_mode: str = "NORMAL"
    wave_guard_status: str = "UNKNOWN"
    wave_guard_reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)
