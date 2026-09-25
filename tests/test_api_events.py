"""服务端业务模块。"""

from __future__ import annotations

from tests.conftest import SHANGHAI_PLAN


def _create_plan(client):
    resp = client.post("/api/plans", json=SHANGHAI_PLAN)
    assert resp.status_code == 201, resp.text


def test_import_events_and_reject_duplicates(client):
    _create_plan(client)
    payload = {
        "events": [
            {
                "event_id": "E-01",
                "event_type": "checkin",
                "student_id": "S1",
                "payload": {
                    "activity_id": "A1",
                    "activity_type": "regular",
                    "check_in_at": "2024-03-15T08:00:00+08:00",
                    "check_out_at": "2024-03-15T10:00:00+08:00",
                },
            },
            {
                "event_id": "E-02",
                "event_type": "checkin",
                "student_id": "S1",
                "payload": {
                    "activity_id": "A1",
                    "activity_type": "regular",
                    "check_in_at": "2024-03-15T09:30:00+08:00",
                    "check_out_at": "2024-03-15T11:00:00+08:00",
                },
            },
        ]
    }
    resp = client.post(f"/api/plans/{SHANGHAI_PLAN['plan_version']}/events", json=payload)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["accepted"] == 2
    assert body["duplicates"] == []

    # Re-import the same events -> both become duplicates, state unchanged.
    resp2 = client.post(
        f"/api/plans/{SHANGHAI_PLAN['plan_version']}/events", json=payload
    )
    assert resp2.status_code == 201
    body2 = resp2.json()
    assert body2["accepted"] == 0
    assert set(body2["duplicates"]) == {"E-01", "E-02"}

    progress = client.get(
        f"/api/plans/{SHANGHAI_PLAN['plan_version']}/students/S1/progress"
    ).json()
    assert progress["total_seconds"] == 10800
    assert progress["lesson_units"] == 4


def test_event_import_requires_existing_plan(client):
    resp = client.post(
        "/api/plans/NOPE/events",
        json={"events": []},
    )
    assert resp.status_code == 404


def test_mentor_confirm_via_api_promotes_pending(client):
    _create_plan(client)
    client.post(
        f"/api/plans/{SHANGHAI_PLAN['plan_version']}/events",
        json={
            "events": [
                {
                    "event_id": "E-01",
                    "event_type": "checkin",
                    "student_id": "S1",
                    "payload": {
                        "activity_id": "A1",
                        "activity_type": "internship",
                        "check_in_at": "2024-03-15T08:00:00+08:00",
                        "check_out_at": "2024-03-15T12:00:00+08:00",
                    },
                }
            ]
        },
    )
    before = client.get(
        f"/api/plans/{SHANGHAI_PLAN['plan_version']}/students/S1/progress"
    ).json()
    assert before["pending_seconds"] == 4 * 3600
    assert before["confirmed_seconds"] == 0

    client.post(
        f"/api/plans/{SHANGHAI_PLAN['plan_version']}/events",
        json={
            "events": [
                {
                    "event_id": "E-02",
                    "event_type": "mentor_confirm",
                    "student_id": "S1",
                    "payload": {"checkin_event_id": "E-01"},
                }
            ]
        },
    )
    after = client.get(
        f"/api/plans/{SHANGHAI_PLAN['plan_version']}/students/S1/progress"
    ).json()
    assert after["confirmed_seconds"] == 4 * 3600
    assert after["pending_seconds"] == 0
