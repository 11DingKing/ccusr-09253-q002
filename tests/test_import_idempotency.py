"""批量导入幂等判定的贯通测试。

覆盖：完全相同重放的幂等成功、同号不同内容冲突的整批原子拒绝、
冲突审计与信息脱敏、批内重复、并发重试、跨重启冲突、回滚行为，
以及冻结/进度查询读不到未提交结果。
"""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app import repository, services
from app.core.fingerprint import event_fingerprint
from app.models import Event as EventModel
from app.models import ImportConflict
from tests.conftest import SHANGHAI_PLAN, TestSessionLocal, test_engine


def _create_plan(client):
    resp = client.post("/api/plans", json=SHANGHAI_PLAN)
    assert resp.status_code == 201, resp.text
    return SHANGHAI_PLAN["plan_version"]


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


def _post(client, pv, events):
    return client.post(f"/api/plans/{pv}/events", json={"events": events})


def _stored_events(student_filter=None):
    session = TestSessionLocal()
    try:
        stmt = select(EventModel).order_by(EventModel.event_id)
        rows = session.execute(stmt).scalars().all()
        if student_filter is not None:
            rows = [r for r in rows if r.student_id == student_filter]
        return rows
    finally:
        session.close()


def _stored_conflicts():
    session = TestSessionLocal()
    try:
        stmt = select(ImportConflict).order_by(ImportConflict.id)
        return session.execute(stmt).scalars().all()
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 完全相同重放 -> 幂等重复成功
# ---------------------------------------------------------------------------


def test_identical_replay_returns_duplicate_success(client):
    pv = _create_plan(client)
    event = _checkin(
        "E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"
    )
    first = _post(client, pv, [event])
    assert first.status_code == 201, first.text
    assert first.json()["accepted"] == 1

    for _ in range(3):
        replay = _post(client, pv, [event])
        assert replay.status_code == 201, replay.text
        body = replay.json()
        assert body["accepted"] == 0
        assert body["duplicates"] == ["E-01"]
        assert body["rejected"] == []

    assert len(_stored_events()) == 1
    progress = client.get(f"/api/plans/{pv}/students/S1/progress").json()
    assert progress["total_seconds"] == 7200


# ---------------------------------------------------------------------------
# 同号不同内容 -> 409 整批原子拒绝
# ---------------------------------------------------------------------------


def test_conflicting_event_id_rejects_entire_batch_atomically(client):
    pv = _create_plan(client)
    original = _checkin(
        "E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"
    )
    assert _post(client, pv, [original]).status_code == 201

    # 源系统“更正”后重试：同号 E-01 但学员与时间都不同，且批内夹带两条新事件。
    corrected = _checkin(
        "E-01", "S2", "2024-03-16T09:00:00+08:00", "2024-03-16T11:00:00+08:00"
    )
    new_1 = _checkin(
        "E-02", "S3", "2024-03-15T08:00:00+08:00", "2024-03-15T09:00:00+08:00"
    )
    new_2 = _checkin(
        "E-03", "S4", "2024-03-15T08:00:00+08:00", "2024-03-15T09:00:00+08:00"
    )
    resp = _post(client, pv, [new_1, corrected, new_2])
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["batch_id"]
    assert [c["event_id"] for c in body["conflicts"]] == ["E-01"]
    conflict = body["conflicts"][0]
    assert conflict["conflict_source"] == "stored"
    assert conflict["mismatched_fields"] == ["student_id", "payload"]
    assert conflict["incoming_fingerprint"] != conflict["stored_fingerprint"]

    # 整批原子拒绝：新事件 E-02/E-03 不得留下半批数据。
    assert [r.event_id for r in _stored_events()] == ["E-01"]
    assert client.get(f"/api/plans/{pv}/students/S3/progress").status_code == 404
    assert client.get(f"/api/plans/{pv}/students/S4/progress").status_code == 404

    # 已存储内容保持原样，未被“更正”覆盖。
    stored = _stored_events()[0]
    assert stored.student_id == "S1"
    assert stored.payload["check_in_at"] == "2024-03-15T08:00:00+08:00"
    progress = client.get(f"/api/plans/{pv}/students/S1/progress").json()
    assert progress["total_seconds"] == 7200

    # 冲突可重复复现：源系统再次重试仍被拒绝，而不是静默成功。
    again = _post(client, pv, [corrected])
    assert again.status_code == 409

    # 原始内容的重放仍然是幂等重复成功。
    replay = _post(client, pv, [original])
    assert replay.status_code == 201
    assert replay.json()["duplicates"] == ["E-01"]


def test_conflict_response_is_auditable_but_does_not_leak_student_data(client):
    pv = _create_plan(client)
    secret_event = _checkin(
        "E-01",
        "S-SECRET",
        "2024-03-15T08:00:00+08:00",
        "2024-03-15T10:00:00+08:00",
    )
    assert _post(client, pv, [secret_event]).status_code == 201

    incoming = _checkin(
        "E-01", "S2", "2024-03-16T09:00:00+08:00", "2024-03-16T11:00:00+08:00"
    )
    resp = _post(client, pv, [incoming])
    assert resp.status_code == 409, resp.text

    # 响应不得泄露已存储事件的学员标识与负载内容。
    assert "S-SECRET" not in resp.text
    assert "2024-03-15T08:00:00" not in resp.text
    conflict = resp.json()["conflicts"][0]
    assert set(conflict) == {
        "event_id",
        "conflict_source",
        "mismatched_fields",
        "incoming_fingerprint",
        "stored_fingerprint",
    }
    # 指纹可用于审计对账：与请求方内容指纹一致，与已存储内容指纹不同。
    assert conflict["incoming_fingerprint"] == event_fingerprint(
        "checkin", "S2", incoming["payload"]
    )
    assert conflict["stored_fingerprint"] == event_fingerprint(
        "checkin", "S-SECRET", secret_event["payload"]
    )

    # 审计记录完整落库（仅供有权限的审计方在存储层查阅）。
    audits = _stored_conflicts()
    assert len(audits) == 1
    audit = audits[0]
    assert audit.batch_id == resp.json()["batch_id"]
    assert audit.plan_version == pv
    assert audit.event_id == "E-01"
    assert audit.conflict_source == "stored"
    assert audit.incoming_student_id == "S2"
    assert audit.incoming_payload == incoming["payload"]
    assert audit.mismatched_fields == ["student_id", "payload"]
    assert audit.incoming_fingerprint == conflict["incoming_fingerprint"]
    assert audit.stored_fingerprint == conflict["stored_fingerprint"]


# ---------------------------------------------------------------------------
# 批内重复
# ---------------------------------------------------------------------------


def test_in_batch_identical_duplicate_is_idempotent(client):
    pv = _create_plan(client)
    event = _checkin(
        "E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"
    )
    other = _checkin(
        "E-02", "S1", "2024-03-16T08:00:00+08:00", "2024-03-16T09:00:00+08:00"
    )
    resp = _post(client, pv, [event, other, dict(event)])
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["accepted"] == 2
    assert body["duplicates"] == ["E-01"]
    assert len(_stored_events()) == 2


def test_in_batch_conflicting_duplicate_rejects_whole_batch(client):
    pv = _create_plan(client)
    first = _checkin(
        "E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"
    )
    conflicting = _checkin(
        "E-01", "S1", "2024-03-15T09:00:00+08:00", "2024-03-15T11:00:00+08:00"
    )
    resp = _post(client, pv, [first, conflicting])
    assert resp.status_code == 409, resp.text
    conflict = resp.json()["conflicts"][0]
    assert conflict["event_id"] == "E-01"
    assert conflict["conflict_source"] == "in_batch"
    assert conflict["mismatched_fields"] == ["payload"]

    # 批内冲突同样原子回滚：E-01 的任何版本都不得落库。
    assert _stored_events() == []
    audits = _stored_conflicts()
    assert len(audits) == 1
    assert audits[0].conflict_source == "in_batch"


# ---------------------------------------------------------------------------
# 负载校验：逐条拒绝，不影响同批合法事件
# ---------------------------------------------------------------------------


def test_invalid_payloads_are_rejected_per_event(client):
    pv = _create_plan(client)
    valid = _checkin(
        "E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"
    )
    backwards = _checkin(
        "E-02", "S1", "2024-03-15T10:00:00+08:00", "2024-03-15T08:00:00+08:00"
    )
    naive = _checkin(
        "E-03", "S1", "2024-03-15T08:00:00", "2024-03-15T10:00:00"
    )
    bad_adjustment = {
        "event_id": "E-04",
        "event_type": "leave_correction",
        "student_id": "S1",
        "payload": {"adjustment_seconds": "not-a-number", "reason": "x"},
    }
    resp = _post(client, pv, [valid, backwards, naive, bad_adjustment])
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["accepted"] == 1
    assert body["duplicates"] == []
    rejected = {r["event_id"]: r["reason"] for r in body["rejected"]}
    assert set(rejected) == {"E-02", "E-03", "E-04"}
    assert all(reason for reason in rejected.values())

    assert [r.event_id for r in _stored_events()] == ["E-01"]
    # 被拒绝的事件重放时不会变成“重复”，而是再次被拒绝。
    replay = _post(client, pv, [backwards])
    assert replay.status_code == 201
    assert replay.json()["accepted"] == 0
    assert [r["event_id"] for r in replay.json()["rejected"]] == ["E-02"]


# ---------------------------------------------------------------------------
# 回滚行为：拒绝的批次对进度与冻结不可见
# ---------------------------------------------------------------------------


def test_rolled_back_batch_invisible_to_progress_and_freeze(client):
    pv = _create_plan(client)
    original = _checkin(
        "E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"
    )
    assert _post(client, pv, [original]).status_code == 201

    doomed = _checkin(
        "E-02", "S2", "2024-03-15T08:00:00+08:00", "2024-03-15T11:00:00+08:00"
    )
    conflict = _checkin(
        "E-01", "S1", "2024-03-15T07:00:00+08:00", "2024-03-15T12:00:00+08:00"
    )
    resp = _post(client, pv, [doomed, conflict])
    assert resp.status_code == 409

    # 进度查询读不到回滚结果。
    assert client.get(f"/api/plans/{pv}/students/S2/progress").status_code == 404
    live = client.get(f"/api/plans/{pv}/snapshot").json()
    assert [s["student_id"] for s in live["students"]] == ["S1"]

    # 冻结快照也不包含回滚事件，cutoff 仍指向已提交的 E-01。
    freeze = client.post(f"/api/plans/{pv}/freezes/F-01", json={})
    assert freeze.status_code == 201, freeze.text
    frozen = freeze.json()
    assert frozen["event_cutoff_id"] == "E-01"
    assert [s["student_id"] for s in frozen["students"]] == ["S1"]
    assert frozen["students"][0]["total_seconds"] == 7200

    # 冲突审计在回滚后依然可查（独立事务提交）。
    assert len(_stored_conflicts()) == 1


def test_uncommitted_import_invisible_to_other_sessions(client):
    pv = _create_plan(client)
    committed = _checkin(
        "E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"
    )
    assert _post(client, pv, [committed]).status_code == 201

    pending = _checkin(
        "E-02", "S2", "2024-03-15T08:00:00+08:00", "2024-03-15T11:00:00+08:00"
    )
    other = TestSessionLocal()
    try:
        staged = repository.stage_events(other, plan_version=pv, events=[pending])
        assert staged.accepted == ["E-02"]

        # 另一个连接在提交前读不到暂存事件（进度与快照都看不到）。
        assert (
            client.get(f"/api/plans/{pv}/students/S2/progress").status_code == 404
        )
        live = client.get(f"/api/plans/{pv}/snapshot").json()
        assert [s["student_id"] for s in live["students"]] == ["S1"]

        other.rollback()
    finally:
        other.close()

    # 回滚后依然不存在；正式提交后才可见。
    assert client.get(f"/api/plans/{pv}/students/S2/progress").status_code == 404
    assert _post(client, pv, [pending]).status_code == 201
    assert client.get(f"/api/plans/{pv}/students/S2/progress").status_code == 200


# ---------------------------------------------------------------------------
# 跨重启冲突：判定完全基于持久化状态
# ---------------------------------------------------------------------------


def test_conflict_detected_across_restart(client):
    pv = _create_plan(client)
    original = _checkin(
        "E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"
    )
    assert _post(client, pv, [original]).status_code == 201

    # 模拟服务重启：同一数据库文件上的全新引擎与会话。
    restart_engine = create_engine(
        str(test_engine.url),
        connect_args={"check_same_thread": False, "timeout": 30},
        poolclass=NullPool,
    )
    RestartSession = sessionmaker(
        bind=restart_engine, autoflush=False, autocommit=False, future=True
    )
    session = RestartSession()
    try:
        conflicting = _checkin(
            "E-01", "S9", "2024-03-17T08:00:00+08:00", "2024-03-17T10:00:00+08:00"
        )
        with pytest.raises(services.BatchConflictError) as excinfo:
            services.import_events(session, plan_version=pv, events=[conflicting])
        assert excinfo.value.conflicts[0].event_id == "E-01"

        replay = services.import_events(session, plan_version=pv, events=[original])
        assert replay["accepted"] == 0
        assert replay["duplicates"] == ["E-01"]
    finally:
        session.close()
        restart_engine.dispose()

    # 重启期间的冲突审计同样持久化。
    assert len(_stored_conflicts()) == 1
    assert _stored_events()[0].student_id == "S1"


# ---------------------------------------------------------------------------
# 并发重试
# ---------------------------------------------------------------------------


def _run_threads(fn, count):
    threads = [threading.Thread(target=fn) for _ in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def test_concurrent_identical_retries_all_succeed(client):
    pv = _create_plan(client)
    batch = [
        _checkin(
            f"E-C{i}",
            f"S{i}",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T10:00:00+08:00",
        )
        for i in range(5)
    ]
    results: list[dict] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def _import():
        session = TestSessionLocal()
        try:
            outcome = services.import_events(session, plan_version=pv, events=batch)
            with lock:
                results.append(outcome)
        except services.BatchConflictError as exc:  # pragma: no cover - 防御
            with lock:
                errors.append(exc)
        finally:
            session.close()

    _run_threads(_import, 4)

    assert errors == []
    assert len(results) == 4
    # 全部事件合计只被接受一次，其余都是幂等重复。
    assert sum(r["accepted"] for r in results) == 5
    assert sum(len(r["duplicates"]) for r in results) == 5 * 3
    for r in results:
        assert r["accepted"] + len(r["duplicates"]) == 5

    assert len(_stored_events()) == 5
    snap = client.get(f"/api/plans/{pv}/snapshot").json()
    assert len(snap["students"]) == 5


def test_concurrent_conflicting_imports_have_exactly_one_winner(client):
    pv = _create_plan(client)
    variants = [
        _checkin(
            "E-RACE",
            "S1",
            f"2024-03-15T{8 + i:02d}:00:00+08:00",
            f"2024-03-15T{10 + i:02d}:00:00+08:00",
        )
        for i in range(4)
    ]
    outcomes: dict[int, object] = {}
    lock = threading.Lock()

    def _import(index):
        session = TestSessionLocal()
        try:
            outcome = services.import_events(
                session, plan_version=pv, events=[variants[index]]
            )
        except services.BatchConflictError as exc:
            outcome = exc
        finally:
            session.close()
        with lock:
            outcomes[index] = outcome

    threads = [threading.Thread(target=_import, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 恰好一方真正写入，其余全部因内容冲突被原子拒绝。
    won = [i for i, o in outcomes.items() if isinstance(o, dict)]
    lost = [i for i, o in outcomes.items() if isinstance(o, services.BatchConflictError)]
    assert len(won) == 1
    assert outcomes[won[0]]["accepted"] == 1
    assert sorted(lost) == sorted(set(range(4)) - set(won))

    stored = _stored_events()
    assert len(stored) == 1
    assert stored[0].payload == variants[won[0]]["payload"]
    assert stored[0].content_hash == event_fingerprint(
        "checkin", "S1", variants[won[0]]["payload"]
    )
    # 每个失败方都留下了审计记录。
    assert len(_stored_conflicts()) == 3


def test_concurrent_conflict_rolls_back_loser_entire_batch(client):
    pv = _create_plan(client)
    batch_a = [
        _checkin(
            "E-A1", "SA", "2024-03-15T08:00:00+08:00", "2024-03-15T09:00:00+08:00"
        ),
        _checkin(
            "E-SHARED",
            "SA",
            "2024-03-15T10:00:00+08:00",
            "2024-03-15T11:00:00+08:00",
        ),
    ]
    batch_b = [
        _checkin(
            "E-B1", "SB", "2024-03-15T08:00:00+08:00", "2024-03-15T09:00:00+08:00"
        ),
        _checkin(
            "E-SHARED",
            "SB",
            "2024-03-15T12:00:00+08:00",
            "2024-03-15T13:00:00+08:00",
        ),
    ]
    outcomes: dict[str, str] = {}
    lock = threading.Lock()

    def _import(name, batch):
        session = TestSessionLocal()
        try:
            services.import_events(session, plan_version=pv, events=batch)
            outcome = "accepted"
        except services.BatchConflictError:
            outcome = "conflict"
        finally:
            session.close()
        with lock:
            outcomes[name] = outcome

    threads = [
        threading.Thread(target=_import, args=("A", batch_a)),
        threading.Thread(target=_import, args=("B", batch_b)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 恰好一方整批成功，另一方整批回滚，不存在半批数据。
    assert sorted(outcomes.values()) == ["accepted", "conflict"]
    stored_ids = sorted(r.event_id for r in _stored_events())
    if outcomes["A"] == "accepted":
        assert stored_ids == ["E-A1", "E-SHARED"]
        assert _stored_events(student_filter="SB") == []
    else:
        assert stored_ids == ["E-B1", "E-SHARED"]
        assert _stored_events(student_filter="SA") == []
