"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.core.clock import (
    SECONDS_PER_LESSON,
    academic_day,
    elapsed_seconds,
    merge_intervals,
    split_by_academic_day,
    to_lesson_units,
    union_seconds,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
NEW_YORK = ZoneInfo("America/New_York")


def test_shanghai_overlapping_checkins_merge_to_10800_seconds_and_4_units():
    # 08:00-10:00 and 09:30-11:00 on the same Shanghai day.
    start1 = datetime(2024, 3, 15, 8, 0, tzinfo=SHANGHAI)
    end1 = datetime(2024, 3, 15, 10, 0, tzinfo=SHANGHAI)
    start2 = datetime(2024, 3, 15, 9, 30, tzinfo=SHANGHAI)
    end2 = datetime(2024, 3, 15, 11, 0, tzinfo=SHANGHAI)

    intervals = [
        (start1.astimezone(timezone.utc), end1.astimezone(timezone.utc)),
        (start2.astimezone(timezone.utc), end2.astimezone(timezone.utc)),
    ]

    merged = merge_intervals(intervals)
    assert len(merged) == 1
    total = union_seconds(intervals)
    assert total == 10800  # 3 hours
    assert to_lesson_units(total) == 4  # 10800 / 2700


def test_new_york_dst_fallback_counts_real_elapsed_time():
    # 2024-11-03 is the DST fall-back Sunday in America/New_York.
    # 01:30 EDT (-04:00) -> 02:30 EST (-05:00) spans TWO real hours because
    # the clock falls back from 02:00 EDT to 01:00 EST in between.
    start = datetime(2024, 11, 3, 1, 30, tzinfo=timezone(timedelta(hours=-4)))
    end = datetime(2024, 11, 3, 2, 30, tzinfo=timezone(timedelta(hours=-5)))

    start_utc = start.astimezone(timezone.utc)
    end_utc = end.astimezone(timezone.utc)

    # 01:30 EDT = 05:30 UTC ; 02:30 EST = 07:30 UTC -> 7200 real seconds.
    assert start_utc.hour == 5 and start_utc.minute == 30
    assert end_utc.hour == 7 and end_utc.minute == 30
    assert elapsed_seconds(start_utc, end_utc) == 7200
    assert to_lesson_units(7200) == 2  # 7200 / 2700 = 2 (floor)


def test_new_york_dst_fallback_same_offset_only_one_hour():
    # If both timestamps are expressed in EDT (-04:00), only one real hour
    # passes.  This proves we honour the offset on the wire.
    start = datetime(2024, 11, 3, 1, 30, tzinfo=timezone(timedelta(hours=-4)))
    end = datetime(2024, 11, 3, 2, 30, tzinfo=timezone(timedelta(hours=-4)))
    assert elapsed_seconds(
        start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    ) == 3600


def test_academic_day_uses_plan_timezone():
    # 2024-03-15 00:30 Shanghai is still 2024-03-14 in UTC.
    shanghai_moment = datetime(2024, 3, 15, 0, 30, tzinfo=SHANGHAI)
    utc_moment = shanghai_moment.astimezone(timezone.utc)
    assert utc_moment.date().isoformat() == "2024-03-14"
    assert academic_day(utc_moment, "Asia/Shanghai").isoformat() == "2024-03-15"


def test_split_across_midnight_in_shanghai():
    # 22:00-02:00 next day local time -> two academic-day segments.
    start = datetime(2024, 3, 15, 22, 0, tzinfo=SHANGHAI).astimezone(timezone.utc)
    end = datetime(2024, 3, 16, 2, 0, tzinfo=SHANGHAI).astimezone(timezone.utc)

    segments = split_by_academic_day(start, end, "Asia/Shanghai")
    assert len(segments) == 2
    days = [seg[0].isoformat() for seg in segments]
    assert days == ["2024-03-15", "2024-03-16"]
    assert sum(elapsed_seconds(s, e) for _, s, e in segments) == 14400


def test_split_dst_fallback_night_does_not_double_count():
    # The fall-back night contains a duplicated local hour, but the interval
    # should split cleanly by local date and sum to the real elapsed time.
    start = datetime(2024, 11, 3, 0, 30, tzinfo=NEW_YORK).astimezone(timezone.utc)
    end = datetime(2024, 11, 3, 3, 30, tzinfo=NEW_YORK).astimezone(timezone.utc)

    segments = split_by_academic_day(start, end, "America/New_York")
    assert len(segments) == 1  # both moments are Nov 3 local
    total = sum(elapsed_seconds(s, e) for _, s, e in segments)
    # 00:30 EDT to 03:30 EST = 4 real hours (the 02:00 repeat adds an hour).
    assert total == 4 * 3600


def test_lesson_units_floor_never_uses_floats():
    assert to_lesson_units(0) == 0
    assert to_lesson_units(2699) == 0
    assert to_lesson_units(2700) == 1
    assert to_lesson_units(5399) == 1
    assert to_lesson_units(5400) == 2
    assert isinstance(to_lesson_units(10800), int)


def test_adjacent_intervals_are_coalesced():
    t0 = datetime(2024, 1, 1, 8, 0, tzinfo=timezone.utc)
    a = (t0, t0.replace(hour=9))
    b = (t0.replace(hour=9), t0.replace(hour=10))
    merged = merge_intervals([a, b])
    assert len(merged) == 1
    assert union_seconds([a, b]) == 7200
