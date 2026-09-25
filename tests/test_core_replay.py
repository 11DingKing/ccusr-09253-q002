"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.clock import to_lesson_units
from app.core.replay import (
    CheckinStatus,
    Event,
    EventType,
    replay,
)


def _event(
    event_id: str,
    event_type: EventType,
    student_id: str,
    payload: dict,
    plan_version: str = "P1",
) -> Event:
    return Event(
        event_id=event_id,
        plan_version=plan_version,
        event_type=event_type,
        student_id=student_id,
        payload=payload,
        created_at=datetime.now(timezone.utc),
    )


def _checkin(
    eid: str,
    student: str,
    start: str,
    end: str,
    *,
    activity_type: str = "regular",
    activity_id: str = "A1",
    plan_version: str = "P1",
) -> Event:
    return _event(
        eid,
        EventType.CHECKIN,
        student,
        {
            "activity_id": activity_id,
            "activity_type": activity_type,
            "check_in_at": start,
            "check_out_at": end,
        },
        plan_version=plan_version,
    )


def test_internship_checkin_is_pending_until_mentor_confirms():
    events = [
        _checkin(
            "E-01",
            "S1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T12:00:00+08:00",
            activity_type="internship",
        ),
    ]
    state = replay(
        events, plan_version="P1", timezone_name="Asia/Shanghai", required_seconds=3600
    )
    progress = state.students["S1"]
    assert progress.confirmed_seconds == 0
    assert progress.pending_seconds == 4 * 3600
    assert progress.total_seconds == 0
    assert progress.meets_requirement is False

    # Now the mentor confirms.
    events.append(
        _event(
            "E-02",
            EventType.MENTOR_CONFIRM,
            "S1",
            {"checkin_event_id": "E-01"},
        )
    )
    state = replay(
        events, plan_version="P1", timezone_name="Asia/Shanghai", required_seconds=3600
    )
    progress = state.students["S1"]
    assert progress.confirmed_seconds == 4 * 3600
    assert progress.pending_seconds == 0
    assert progress.total_seconds == 4 * 3600
    assert progress.lesson_units == to_lesson_units(4 * 3600)
    assert progress.meets_requirement is True


def test_overlapping_regular_checkins_are_unioned():
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _checkin("E-02", "S1", "2024-03-15T09:30:00+08:00", "2024-03-15T11:00:00+08:00"),
    ]
    state = replay(
        events, plan_version="P1", timezone_name="Asia/Shanghai", required_seconds=10800
    )
    progress = state.students["S1"]
    assert progress.confirmed_seconds == 10800
    assert progress.lesson_units == 4
    assert progress.meets_requirement is True


def test_out_of_order_correction_converges():
    # A correction arriving before/after other events must not change the
    # final total because replay sorts by event_id.
    base = [
        _checkin("E-03", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _event(
            "E-01",
            EventType.LEAVE_CORRECTION,
            "S1",
            {"adjustment_seconds": -1800, "reason": "late arrival"},
        ),
        _checkin("E-02", "S1", "2024-03-15T10:00:00+08:00", "2024-03-15T11:00:00+08:00"),
    ]
    state_a = replay(
        base, plan_version="P1", timezone_name="Asia/Shanghai", required_seconds=3600
    )
    state_b = replay(
        list(reversed(base)),
        plan_version="P1",
        timezone_name="Asia/Shanghai",
        required_seconds=3600,
    )
    assert (
        state_a.students["S1"].total_seconds
        == state_b.students["S1"].total_seconds
        == 3 * 3600 - 1800
    )


def test_mentor_confirm_for_wrong_student_is_ignored():
    events = [
        _checkin(
            "E-01",
            "S1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T10:00:00+08:00",
            activity_type="internship",
        ),
        _event(
            "E-02",
            EventType.MENTOR_CONFIRM,
            "S9",
            {"checkin_event_id": "E-01"},
        ),
    ]
    state = replay(
        events, plan_version="P1", timezone_name="Asia/Shanghai", required_seconds=3600
    )
    assert state.students["S1"].checkins[0].status == CheckinStatus.PENDING


def test_negative_total_is_clamped_to_zero():
    events = [
        _event(
            "E-01",
            EventType.LEAVE_CORRECTION,
            "S1",
            {"adjustment_seconds": -7200, "reason": "unapproved absence"},
        ),
    ]
    state = replay(
        events, plan_version="P1", timezone_name="Asia/Shanghai", required_seconds=0
    )
    assert state.students["S1"].total_seconds == 0
    assert state.students["S1"].lesson_units == 0


def test_replay_only_considers_requested_plan():
    events = [
        _checkin(
            "E-01",
            "S1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T10:00:00+08:00",
            plan_version="P1",
        ),
        _checkin(
            "E-02",
            "S1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T11:00:00+08:00",
            plan_version="P2",
        ),
    ]
    state = replay(
        events, plan_version="P1", timezone_name="Asia/Shanghai", required_seconds=0
    )
    assert state.students["S1"].confirmed_seconds == 7200


def test_replay_up_to_event_id_reconstructs_past_state():
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T09:00:00+08:00"),
        _checkin("E-02", "S1", "2024-03-15T09:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _checkin("E-03", "S1", "2024-03-15T10:00:00+08:00", "2024-03-15T11:00:00+08:00"),
    ]
    full = replay(
        events, plan_version="P1", timezone_name="Asia/Shanghai", required_seconds=0
    )
    assert full.students["S1"].confirmed_seconds == 3 * 3600

    past = replay(
        events,
        plan_version="P1",
        timezone_name="Asia/Shanghai",
        required_seconds=0,
        up_to_event_id="E-02",
    )
    assert past.students["S1"].confirmed_seconds == 2 * 3600


def test_10000_event_replay_is_deterministic():
    base = datetime(2024, 3, 15, 8, 0, tzinfo=timezone(timedelta(hours=8)))
    events: list[Event] = []
    for i in range(10000):
        eid = f"E-{i:05d}"
        start = base + timedelta(minutes=30 * i)
        end = start + timedelta(minutes=30)
        events.append(
            _event(
                eid,
                EventType.CHECKIN,
                f"S{i % 50}",
                {
                    "activity_id": "A",
                    "activity_type": "regular",
                    "check_in_at": start.isoformat(),
                    "check_out_at": end.isoformat(),
                },
            )
        )

    state_a = replay(
        events, plan_version="P1", timezone_name="Asia/Shanghai", required_seconds=0
    )
    # Shuffle to prove insertion order does not matter.
    import random

    rng = random.Random(42)
    shuffled = list(events)
    rng.shuffle(shuffled)
    state_b = replay(
        shuffled, plan_version="P1", timezone_name="Asia/Shanghai", required_seconds=0
    )

    for sid in state_a.students:
        a = state_a.students[sid]
        b = state_b.students[sid]
        assert a.total_seconds == b.total_seconds
        assert a.lesson_units == b.lesson_units
        assert a.confirmed_seconds == b.confirmed_seconds

    total_a = sum(s.total_seconds for s in state_a.students.values())
    total_b = sum(s.total_seconds for s in state_b.students.values())
    assert total_a == total_b == 10000 * 1800
