from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone


SHANGHAI_TIMEZONE = timezone(timedelta(hours=8))
SHADOW_ONLY_START_HOUR = 12
SHADOW_ONLY_END_HOUR = 18


@dataclass(frozen=True)
class TimePeriodGuardConfig:
    enabled: bool = False


@dataclass(frozen=True)
class TimePeriodGuardDecision:
    enabled: bool
    blocked: bool
    code: str
    local_hour: int
    window: str
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def evaluate_time_period_guard(
    current_time_ms: int,
    config: TimePeriodGuardConfig,
) -> TimePeriodGuardDecision:
    local_hour = datetime.fromtimestamp(
        current_time_ms / 1000,
        SHANGHAI_TIMEZONE,
    ).hour
    window = "12:00-18:00"
    if not config.enabled:
        return TimePeriodGuardDecision(
            enabled=False,
            blocked=False,
            code="DISABLED",
            local_hour=local_hour,
            window=window,
            reason="北京时间段影子守卫已关闭",
        )
    blocked = SHADOW_ONLY_START_HOUR <= local_hour < SHADOW_ONLY_END_HOUR
    return TimePeriodGuardDecision(
        enabled=True,
        blocked=blocked,
        code="TIME_PERIOD_SHADOW_ONLY" if blocked else "TIME_PERIOD_ALLOWED",
        local_hour=local_hour,
        window=window,
        reason=(
            "北京时间 12:00-18:00 胜率优先暂停真实开单，仅记录影子观察"
            if blocked
            else "当前不在北京时间 12:00-18:00 影子观察时段"
        ),
    )
