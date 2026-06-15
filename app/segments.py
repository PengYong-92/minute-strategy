def threshold_segment(timestamp_ms: int) -> str:
    hour = (timestamp_ms // 3_600_000) % 24
    day = timestamp_ms // 86_400_000
    weekday = (day + 3) % 7
    day_type = "WE" if weekday >= 5 else "WD"
    return f"{day_type}-{hour:02d}"
