"""服务端业务模块。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from .core.snapshot import Snapshot, build_snapshot, diff_snapshots, explain_student
from .repository import (
    EventConflict,
    get_freeze,
    get_plan,
    insert_freeze,
    load_events,
    load_events_up_to,
    max_event_id,
    record_import_conflicts,
    stage_events,
    upsert_plan,
)
from .schemas import (
    CheckinPayload,
    LeaveCorrectionPayload,
    MentorConfirmPayload,
)


class PlanNotFoundError(Exception):
    pass


class FreezeConflictError(Exception):
    pass


class FreezeNotFoundError(Exception):
    pass


class BatchConflictError(Exception):
    """同号事件内容冲突：整批已原子回滚，冲突已写入审计。"""

    def __init__(
        self,
        *,
        plan_version: str,
        batch_id: str,
        conflicts: list[EventConflict],
    ) -> None:
        super().__init__(
            f"batch {batch_id} rejected: {len(conflicts)} event id(s) "
            f"carry conflicting content for plan '{plan_version}'"
        )
        self.plan_version = plan_version
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


_PAYLOAD_MODELS = {
    "checkin": CheckinPayload,
    "mentor_confirm": MentorConfirmPayload,
    "leave_correction": LeaveCorrectionPayload,
}


def _validate_payloads(
    events: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按事件类型贯通校验负载模型，返回 (合法事件, 逐条拒绝原因)。"""
    valid: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for e in events:
        model = _PAYLOAD_MODELS.get(e["event_type"])
        if model is None:
            rejected.append(
                {
                    "event_id": e["event_id"],
                    "reason": f"unsupported event_type: {e['event_type']}",
                }
            )
            continue
        try:
            model.model_validate(e["payload"])
        except ValidationError as exc:
            reason = "; ".join(
                f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
                for err in exc.errors()
            )
            rejected.append({"event_id": e["event_id"], "reason": reason})
            continue
        valid.append(e)
    return valid, rejected


def import_events(
    db: Session, *, plan_version: str, events: list[dict[str, Any]]
) -> dict[str, Any]:
    """批量导入事件，保证幂等与原子性。

    - 内容完全相同的重放记为 duplicates（幂等成功）；
    - 负载校验失败的事件逐条列入 rejected，不阻断其余事件；
    - 同号但内容指纹不同的事件触发整批原子回滚（不留半批数据），
      冲突详情在回滚后单独事务写入审计，再抛出 BatchConflictError。
    """
    _require_plan(db, plan_version)
    valid, rejected = _validate_payloads(events)
    staged = stage_events(db, plan_version=plan_version, events=valid)
    if staged.conflicts:
        db.rollback()
        batch_id = uuid.uuid4().hex
        record_import_conflicts(
            db,
            plan_version=plan_version,
            batch_id=batch_id,
            conflicts=staged.conflicts,
        )
        db.commit()
        raise BatchConflictError(
            plan_version=plan_version,
            batch_id=batch_id,
            conflicts=staged.conflicts,
        )
    db.commit()
    return {
        "accepted": len(staged.accepted),
        "duplicates": staged.duplicates,
        "rejected": rejected,
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
