"""服务端业务模块。"""

from __future__ import annotations

from tests.conftest import SHANGHAI_PLAN


def _create_plan(client):
    resp = client.post("/api/plans", json=SHANGHAI_PLAN)
    assert resp.status_code == 201, resp.text


def _checkin(eid, student, start, end, activity_type="regular"):
    return {
        "event_id": eid,
        "event_type": "checkin",
        "student_id": student,
        "payload": {
            "activity_id": "A1",
            "activity_type": activity_type,
            "check_in_at": start,
            "check_out_at": end,
        },
    }


def test_cross_midnight_activity_split_into_two_academic_days(client):
    _create_plan(client)
    # 22:00-02:00 next day in Shanghai.
    client.post(
        f"/api/plans/{SHANGHAI_PLAN['plan_version']}/events",
        json={
            "events": [
                _checkin(
                    "E-01",
                    "S1",
                    "2024-03-15T22:00:00+08:00",
                    "2024-03-16T02:00:00+08:00",
                )
            ]
        },
    )
    progress = client.get(
        f"/api/plans/{SHANGHAI_PLAN['plan_version']}/students/S1/progress"
    ).json()
    assert progress["total_seconds"] == 4 * 3600
    days = {d["academic_day"]: d["seconds"] for d in progress["daily"]}
    assert days == {"2024-03-15": 2 * 3600, "2024-03-16": 2 * 3600}
    # The check-in explanation also lists both segments.
    assert len(progress["checkins"][0]["academic_days"]) == 2


def test_snapshot_contains_all_students(client):
    _create_plan(client)
    client.post(
        f"/api/plans/{SHANGHAI_PLAN['plan_version']}/events",
        json={
            "events": [
                _checkin(
                    "E-01",
                    "S1",
                    "2024-03-15T08:00:00+08:00",
                    "2024-03-15T10:00:00+08:00",
                ),
                _checkin(
                    "E-02",
                    "S2",
                    "2024-03-15T08:00:00+08:00",
                    "2024-03-15T11:00:00+08:00",
                ),
            ]
        },
    )
    snap = client.get(
        f"/api/plans/{SHANGHAI_PLAN['plan_version']}/snapshot"
    ).json()
    ids = {s["student_id"] for s in snap["students"]}
    assert ids == {"S1", "S2"}
    by_id = {s["student_id"]: s for s in snap["students"]}
    assert by_id["S1"]["total_seconds"] == 7200
    assert by_id["S2"]["total_seconds"] == 3 * 3600


def test_progress_unknown_student_returns_404(client):
    _create_plan(client)
    resp = client.get(
        f"/api/plans/{SHANGHAI_PLAN['plan_version']}/students/NOBODY/progress"
    )
    assert resp.status_code == 404


def test_pending_internship_does_not_count_toward_compliance(client):
    plan = dict(SHANGHAI_PLAN)
    plan["required_seconds"] = 3600
    client.post("/api/plans", json=plan)
    client.post(
        f"/api/plans/{plan['plan_version']}/events",
        json={
            "events": [
                _checkin(
                    "E-01",
                    "S1",
                    "2024-03-15T08:00:00+08:00",
                    "2024-03-15T12:00:00+08:00",
                    activity_type="internship",
                )
            ]
        },
    )
    progress = client.get(
        f"/api/plans/{plan['plan_version']}/students/S1/progress"
    ).json()
    assert progress["total_seconds"] == 0
    assert progress["meets_requirement"] is False
    assert progress["pending_seconds"] == 4 * 3600
