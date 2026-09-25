"""服务端业务模块。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from .core.snapshot import Snapshot, build_snapshot, diff_snapshots, explain_student
from .repository import (
    get_freeze,
    get_plan,
    insert_events,
    insert_freeze,
    load_events,
    load_events_up_to,
    max_event_id,
    upsert_plan,
)


class PlanNotFoundError(Exception):
    pass


class FreezeConflictError(Exception):
    pass


class FreezeNotFoundError(Exception):
    pass


class EventConflictError(Exception):
    """同号事件内容冲突：整批已被原子拒绝。

    携带的 conflicts 为脱敏后的冲突详情（仅事件编号与内容指纹），
    不包含任何学员标识，可安全返回给调用方；完整细节已写入审计表。
    """

    def __init__(self, batch_id: str, conflicts: list[dict[str, Any]]) -> None:
        super().__init__("event id reused with different content; batch rejected")
        self.batch_id = batch_id
        self.conflicts = conflicts


def get_plan_plain(db: Session, plan_version: str) -> dict[str, Any] | None:
    plan = get_plan(db, plan_version)
    if plan is None:
        return None
    return {
        "plan_version": plan.plan_version,
        "iana_timezone": plan.iana_timezone,
        "required_seconds": plan.required_seconds,
    }


def ensure_plan(
    db: Session,
    *,
    plan_version: str,
    iana_timezone: str,
    required_seconds: int,
) -> dict[str, Any]:
    plan = upsert_plan(
        db,
        plan_version=plan_version,
        iana_timezone=iana_timezone,
        required_seconds=required_seconds,
    )
    return {
        "plan_version": plan.plan_version,
        "iana_timezone": plan.iana_timezone,
        "required_seconds": plan.required_seconds,
    }


def _require_plan(db: Session, plan_version: str):
    plan = get_plan(db, plan_version)
    if plan is None:
        raise PlanNotFoundError(f"plan version '{plan_version}' is not registered")
    return plan


def import_events(
    db: Session, *, plan_version: str, events: list[dict[str, Any]]
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    # 结束只读事务，避免在持有读快照的情况下进入写批次：并发导入时
    # 各连接直接在写锁上排队，而不会因读锁升级相互阻塞。
    db.commit()
    batch_id = uuid.uuid4().hex
    outcome = insert_events(
        db, plan_version=plan_version, events=events, batch_id=batch_id
    )
    if outcome.conflicts:
        # 整批已回滚且审计已落库；向调用方只暴露脱敏的冲突详情。
        sanitized = [
            {
                "event_id": c.event_id,
                "reason": c.reason,
                "submitted_fingerprint": c.submitted_fingerprint,
                "stored_fingerprint": c.stored_fingerprint,
            }
            for c in outcome.conflicts
        ]
        raise EventConflictError(batch_id, sanitized)
    return {
        "accepted": len(outcome.accepted),
        "duplicates": outcome.duplicates,
        "rejected": [],
    }


def current_snapshot(db: Session, plan_version: str) -> Snapshot:
    plan = _require_plan(db, plan_version)
    events = load_events(db, plan_version)
    return build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
    )


def student_progress(
    db: Session, plan_version: str, student_id: str
) -> dict[str, Any] | None:
    snap = current_snapshot(db, plan_version)
    return explain_student(snap, student_id)


def freeze_semester(
    db: Session, *, plan_version: str, freeze_id: str
) -> tuple[Snapshot, bool]:
    """执行确定性的业务处理。"""
    plan = _require_plan(db, plan_version)
    existing = get_freeze(db, plan_version, freeze_id)
    if existing is not None:
        return Snapshot.from_dict(existing.snapshot), False

    cutoff = max_event_id(db, plan_version)
    events = load_events(db, plan_version)
    snap = build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        freeze_id=freeze_id,
        event_cutoff_id=cutoff,
    )
    row = insert_freeze(
        db,
        plan_version=plan_version,
        freeze_id=freeze_id,
        snapshot=snap.to_dict(),
        event_cutoff_id=cutoff,
    )
    if row is None:
        existing = get_freeze(db, plan_version, freeze_id)
        assert existing is not None
        return Snapshot.from_dict(existing.snapshot), False
    return snap, True


def get_frozen_snapshot(
    db: Session, plan_version: str, freeze_id: str
) -> Snapshot:
    _require_plan(db, plan_version)
    row = get_freeze(db, plan_version, freeze_id)
    if row is None:
        raise FreezeNotFoundError(
            f"freeze '{freeze_id}' for plan '{plan_version}' does not exist"
        )
    return Snapshot.from_dict(row.snapshot)


def explain_frozen_student(
    db: Session, plan_version: str, freeze_id: str, student_id: str
) -> dict[str, Any] | None:
    snap = get_frozen_snapshot(db, plan_version, freeze_id)
    return explain_student(snap, student_id)


def diff_freezes(
    db: Session, plan_version: str, old_freeze_id: str, new_freeze_id: str
) -> dict[str, Any]:
    old = get_frozen_snapshot(db, plan_version, old_freeze_id)
    new = get_frozen_snapshot(db, plan_version, new_freeze_id)
    return diff_snapshots(old, new)
