"""服务端业务模块。"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

SECONDS_PER_LESSON = 45 * 60  # a 45-minute teaching unit


def get_zone(tz_name: str) -> ZoneInfo:
    """执行确定性的业务处理。"""
    return ZoneInfo(tz_name)


def to_utc(value: datetime) -> datetime:
    """执行确定性的业务处理。"""
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc)


def academic_day(moment_utc: datetime, tz_name: str) -> date:
    """执行确定性的业务处理。"""
    utc_dt = to_utc(moment_utc)
    return utc_dt.astimezone(get_zone(tz_name)).date()


def local_midnight_as_utc(day: date, tz_name: str) -> datetime:
    """执行确定性的业务处理。"""
    zone = get_zone(tz_name)
    naive = datetime.combine(day, time(0, 0))
    aware = naive.replace(tzinfo=zone)
    return aware.astimezone(timezone.utc)


def split_by_academic_day(
    start_utc: datetime,
    end_utc: datetime,
    tz_name: str,
) -> list[tuple[date, datetime, datetime]]:
    """执行确定性的业务处理。"""
    start_utc = to_utc(start_utc)
    end_utc = to_utc(end_utc)
    if end_utc <= start_utc:
        return []

    zone = get_zone(tz_name)
    segments: list[tuple[date, datetime, datetime]] = []

    cursor = start_utc
    while cursor < end_utc:
        local_cursor = cursor.astimezone(zone)
        day = local_cursor.date()
        next_midnight_local = datetime.combine(
            day + timedelta(days=1), time(0, 0)
        ).replace(tzinfo=zone)
        next_midnight_utc = next_midnight_local.astimezone(timezone.utc)
        seg_end = min(end_utc, next_midnight_utc)
        if seg_end > cursor:
            segments.append((day, cursor, seg_end))
        cursor = seg_end

    return segments


def elapsed_seconds(start_utc: datetime, end_utc: datetime) -> int:
    """执行确定性的业务处理。"""
    delta = to_utc(end_utc) - to_utc(start_utc)
    return int(delta.total_seconds())


def to_lesson_units(total_seconds: int) -> int:
    """执行确定性的业务处理。"""
    if total_seconds <= 0:
        return 0
    return total_seconds // SECONDS_PER_LESSON


def merge_intervals(
    intervals: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """执行确定性的业务处理。"""
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda pair: pair[0])
    merged: list[tuple[datetime, datetime]] = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            if end > last_end:
                merged[-1] = (last_start, end)
        else:
            merged.append((start, end))
    return merged


def union_seconds(intervals: list[tuple[datetime, datetime]]) -> int:
    """执行确定性的业务处理。"""
    total = 0
    for start, end in merge_intervals(intervals):
        total += elapsed_seconds(start, end)
    return total
