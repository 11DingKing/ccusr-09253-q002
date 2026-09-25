"""同号事件内容冲突的幂等判定、原子回滚与审计测试。"""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import services
from app.models import Base, Event, EventConflictAudit
from tests.conftest import SHANGHAI_PLAN, TestSessionLocal


def _create_plan(client, plan=None):
    resp = client.post("/api/plans", json=plan or SHANGHAI_PLAN)
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


def _post_events(client, events, plan=None):
    plan = plan or SHANGHAI_PLAN
    return client.post(f"/api/plans/{plan['plan_version']}/events", json={"events": events})


def _event_count(db, plan_version):
    return len(db.execute(
        select(Event).where(Event.plan_version == plan_version)
    ).scalars().all())


def _audit_rows(db, plan_version):
    return db.execute(
        select(EventConflictAudit)
        .where(EventConflictAudit.plan_version == plan_version)
        .order_by(EventConflictAudit.id)
    ).scalars().all()


PV = SHANGHAI_PLAN["plan_version"]


def test_conflicting_event_id_rejects_whole_batch_and_audits(client, db):
    """同号不同内容：整批原子拒绝，审计落库，响应不泄露学员信息。"""
    _create_plan(client)
    resp = _post_events(client, [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00")
    ])
    assert resp.status_code == 201

    # 源系统“更正”E-01（换了学员与时间），并混入一个新事件 E-02。
    resp = _post_events(client, [
        _checkin("E-02", "S9", "2024-03-16T08:00:00+08:00", "2024-03-16T09:00:00+08:00"),
        _checkin("E-01", "S2", "2024-03-15T08:00:00+08:00", "2024-03-15T11:00:00+08:00"),
    ])
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "event_id_conflict"
    assert detail["batch_id"]
    assert [c["event_id"] for c in detail["conflicts"]] == ["E-01"]
    conflict = detail["conflicts"][0]
    assert conflict["reason"] == "content_mismatch"
    assert conflict["submitted_fingerprint"] != conflict["stored_fingerprint"]
    # 响应不得泄露任何学员标识（无论已存还是新提交的）。
    assert "S1" not in resp.text
    assert "S2" not in resp.text
    assert "S9" not in resp.text

    # 原子性：E-02 不得落库，E-01 保持原内容，库里仍只有 1 条事件。
    assert _event_count(db, PV) == 1
    stored = db.execute(select(Event).where(Event.event_id == "E-01")).scalar_one()
    assert stored.student_id == "S1"
    progress = client.get(f"/api/plans/{PV}/students/S1/progress").json()
    assert progress["total_seconds"] == 7200
    assert client.get(f"/api/plans/{PV}/students/S9/progress").status_code == 404

    # 审计：完整细节（含双方学员与指纹）已持久化，可用 batch_id 关联。
    audits = _audit_rows(db, PV)
    assert len(audits) == 1
    audit = audits[0]
    assert audit.batch_id == detail["batch_id"]
    assert audit.event_id == "E-01"
    assert audit.reason == "content_mismatch"
    assert audit.submitted_student_id == "S2"
    assert audit.stored_student_id == "S1"
    assert audit.submitted_fingerprint == conflict["submitted_fingerprint"]
    assert audit.stored_fingerprint == conflict["stored_fingerprint"]


def test_identical_replay_remains_duplicate_success(client, db):
    """完全相同的重放仍是幂等成功，且不产生审计记录。"""
    _create_plan(client)
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00")
    ]
    assert _post_events(client, events).status_code == 201

    resp = _post_events(client, events)
    assert resp.status_code == 201
    body = resp.json()
    assert body["accepted"] == 0
    assert body["duplicates"] == ["E-01"]
    assert body["rejected"] == []
    assert _event_count(db, PV) == 1
    assert _audit_rows(db, PV) == []


def test_intra_batch_identical_duplicates(client, db):
    """批内完全相同的重复：首条受理，其余按重复处理。"""
    _create_plan(client)
    event = _checkin(
        "E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"
    )
    resp = _post_events(client, [event, dict(event), _checkin(
        "E-02", "S1", "2024-03-16T08:00:00+08:00", "2024-03-16T09:00:00+08:00"
    )])
    assert resp.status_code == 201
    body = resp.json()
    assert body["accepted"] == 2
    assert body["duplicates"] == ["E-01"]
    assert _event_count(db, PV) == 2


def test_intra_batch_conflicting_duplicates_rejected(client, db):
    """批内同号不同内容：整批拒绝，一个事件都不落库。"""
    _create_plan(client)
    resp = _post_events(client, [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _checkin("E-02", "S1", "2024-03-16T08:00:00+08:00", "2024-03-16T09:00:00+08:00"),
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T12:00:00+08:00"),
    ])
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert [c["event_id"] for c in detail["conflicts"]] == ["E-01"]
    assert detail["conflicts"][0]["reason"] == "intra_batch_mismatch"

    # 整批回滚：E-01/E-02 都不存在，进度查询读不到半批数据。
    assert _event_count(db, PV) == 0
    assert client.get(f"/api/plans/{PV}/students/S1/progress").status_code == 404
    audits = _audit_rows(db, PV)
    assert len(audits) == 1
    assert audits[0].reason == "intra_batch_mismatch"


def test_rejected_batch_invisible_to_freeze_and_progress(client, db):
    """冻结快照与进度查询在冲突批次回滚后保持原状。"""
    _create_plan(client)
    _post_events(client, [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00")
    ])
    frozen = client.post(f"/api/plans/{PV}/freezes/F-01", json={}).json()
    assert frozen["students"][0]["total_seconds"] == 7200

    resp = _post_events(client, [
        _checkin("E-03", "S3", "2024-03-17T08:00:00+08:00", "2024-03-17T10:00:00+08:00"),
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T11:30:00+08:00"),
    ])
    assert resp.status_code == 409

    # 实时快照、冻结快照与进度均未变化；新冻结也不包含被拒批次。
    live = client.get(f"/api/plans/{PV}/snapshot").json()
    assert [s["student_id"] for s in live["students"]] == ["S1"]
    assert live["students"][0]["total_seconds"] == 7200
    again = client.get(f"/api/plans/{PV}/freezes/F-01").json()
    assert again["students"][0]["total_seconds"] == 7200
    progress = client.get(f"/api/plans/{PV}/students/S1/progress").json()
    assert progress["total_seconds"] == 7200
    f2 = client.post(f"/api/plans/{PV}/freezes/F-02", json={}).json()
    assert f2["event_cutoff_id"] == "E-01"
    assert f2["students"][0]["total_seconds"] == 7200


def test_concurrent_identical_retries_accept_once(client):
    """并发重放同一事件：恰好一次受理，其余按重复成功。"""
    _create_plan(client)
    event = _checkin(
        "E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"
    )
    outcomes: list[dict] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def _worker():
        session = TestSessionLocal()
        try:
            result = services.import_events(session, plan_version=PV, events=[event])
            with lock:
                outcomes.append(result)
        except services.EventConflictError as exc:
            with lock:
                errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert sum(o["accepted"] for o in outcomes) == 1
    assert sum(len(o["duplicates"]) for o in outcomes) == 3
    progress = client.get(f"/api/plans/{PV}/students/S1/progress").json()
    assert progress["total_seconds"] == 7200


def test_concurrent_conflicting_retries_exactly_one_wins(client, db):
    """并发同号不同内容：恰好一个批次成功，其余整批拒绝，无半批数据。"""
    _create_plan(client)
    outcomes: list[dict] = []
    conflicts: list[services.EventConflictError] = []
    lock = threading.Lock()

    def _worker(hour: int):
        event = _checkin(
            "E-01", "S1",
            f"2024-03-15T0{hour}:00:00+08:00", f"2024-03-15T0{hour + 1}:00:00+08:00",
        )
        session = TestSessionLocal()
        try:
            result = services.import_events(session, plan_version=PV, events=[event])
            with lock:
                outcomes.append(result)
        except services.EventConflictError as exc:
            with lock:
                conflicts.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=_worker, args=(h,)) for h in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 恰好一个获胜者；其余全部因指纹冲突被原子拒绝。
    assert len(outcomes) == 1
    assert outcomes[0]["accepted"] == 1
    assert len(conflicts) == 3
    assert all(c.batch_id for c in conflicts)

    # 数据库中只有获胜版本，审计完整记录 3 次冲突。
    rows = db.execute(select(Event).where(Event.plan_version == PV)).scalars().all()
    assert len(rows) == 1
    audits = _audit_rows(db, PV)
    assert len(audits) == 3
    assert {a.stored_student_id for a in audits} == {"S1"}
    assert all(a.stored_fingerprint == rows[0].content_hash for a in audits)


def test_conflict_detection_survives_restart(tmp_path):
    """指纹持久化在库中：跨进程重启后仍能识别同号冲突与幂等重放。"""
    db_file = tmp_path / "restart.db"
    url = f"sqlite:///{db_file}"

    engine_a = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine_a)
    session_a = sessionmaker(bind=engine_a, autoflush=False, autocommit=False)()
    services.ensure_plan(session_a, **SHANGHAI_PLAN)
    event = _checkin(
        "E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"
    )
    first = services.import_events(session_a, plan_version=PV, events=[event])
    assert first["accepted"] == 1
    session_a.close()
    engine_a.dispose()

    # “重启”：全新引擎与会话，不共享任何内存状态。
    engine_b = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
    session_b = sessionmaker(bind=engine_b, autoflush=False, autocommit=False)()
    try:
        replay = services.import_events(session_b, plan_version=PV, events=[event])
        assert replay["accepted"] == 0
        assert replay["duplicates"] == ["E-01"]

        changed = _checkin(
            "E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T11:00:00+08:00"
        )
        with pytest.raises(services.EventConflictError):
            services.import_events(session_b, plan_version=PV, events=[changed])

        # 冲突后库中仍是原始版本。
        rows = session_b.execute(select(Event)).scalars().all()
        assert len(rows) == 1
        assert rows[0].payload["check_out_at"] == "2024-03-15T10:00:00+08:00"
    finally:
        session_b.close()
        engine_b.dispose()


def test_malformed_payload_rejected_before_persistence(client, db):
    """校验模型贯通：畸形载荷在持久化前以 422 拒绝。"""
    _create_plan(client)
    # 结束时间不晚于开始时间。
    resp = _post_events(client, [
        _checkin("E-01", "S1", "2024-03-15T10:00:00+08:00", "2024-03-15T08:00:00+08:00")
    ])
    assert resp.status_code == 422
    # 无时区时间戳。
    resp = _post_events(client, [
        _checkin("E-02", "S1", "2024-03-15T08:00:00", "2024-03-15T10:00:00")
    ])
    assert resp.status_code == 422
    # mentor_confirm 缺少必填字段。
    resp = _post_events(client, [
        {"event_id": "E-03", "event_type": "mentor_confirm", "student_id": "S1", "payload": {}}
    ])
    assert resp.status_code == 422
    assert _event_count(db, PV) == 0
