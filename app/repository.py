"""服务端业务模块。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .core.fingerprint import event_fingerprint
from .core.replay import Event as CoreEvent
from .core.replay import EventType
from .models import Event as EventModel
from .models import EventConflictAudit, Freeze, Plan


def get_plan(db: Session, plan_version: str) -> Plan | None:
    return db.get(Plan, plan_version)


def upsert_plan(
    db: Session,
    *,
    plan_version: str,
    iana_timezone: str,
    required_seconds: int,
) -> Plan:
    stmt = sqlite_insert(Plan).values(
        plan_version=plan_version,
        iana_timezone=iana_timezone,
        required_seconds=required_seconds,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["plan_version"],
        set_={
            "iana_timezone": iana_timezone,
            "required_seconds": required_seconds,
        },
    )
    db.execute(stmt)
    db.commit()
    plan = db.get(Plan, plan_version)
    assert plan is not None
    return plan


def _to_core_event(row: EventModel) -> CoreEvent:
    return CoreEvent(
        event_id=row.event_id,
        plan_version=row.plan_version,
        event_type=EventType(row.event_type),
        student_id=row.student_id,
        payload=dict(row.payload),
        created_at=row.created_at,
    )


@dataclass
class ConflictDetail:
    """一次同号事件内容冲突的完整内部细节（含学员信息，仅供审计）。"""

    event_id: str
    reason: str
    submitted_student_id: str
    submitted_fingerprint: str
    stored_student_id: str | None
    stored_fingerprint: str | None


@dataclass
class ImportOutcome:
    """批量导入结果：accepted/duplicates 仅在无冲突并已提交时有效。"""

    accepted: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    conflicts: list[ConflictDetail] = field(default_factory=list)


def _record_conflicts(
    db: Session,
    *,
    batch_id: str,
    plan_version: str,
    conflicts: list[ConflictDetail],
) -> None:
    """在独立事务中持久化冲突审计，使其在整批回滚后仍然可查。"""
    for c in conflicts:
        db.add(
            EventConflictAudit(
                batch_id=batch_id,
                plan_version=plan_version,
                event_id=c.event_id,
                reason=c.reason,
                submitted_student_id=c.submitted_student_id,
                stored_student_id=c.stored_student_id,
                submitted_fingerprint=c.submitted_fingerprint,
                stored_fingerprint=c.stored_fingerprint,
            )
        )
    db.commit()


def insert_events(
    db: Session,
    *,
    plan_version: str,
    events: list[dict[str, Any]],
    batch_id: str,
) -> ImportOutcome:
    """原子导入一批事件，按内容指纹做幂等判定。

    - 内容与已存事件完全一致的同号事件：幂等重放，计入 duplicates；
    - 同号但内容指纹不同（含批内自相矛盾）：整批回滚，一个事件都不落库，
      并在独立事务中写入冲突审计，保证不会留下半批数据；
    - 全部成功时才提交，冻结与进度查询永远读不到未提交的中间结果。
    """
    outcome = ImportOutcome()
    seen_in_batch: dict[str, tuple[str, str]] = {}  # event_id -> (fingerprint, student_id)

    for e in events:
        fingerprint = event_fingerprint(
            event_type=e["event_type"],
            student_id=e["student_id"],
            payload=e["payload"],
        )
        event_id = e["event_id"]

        prior = seen_in_batch.get(event_id)
        if prior is not None:
            prior_fingerprint, prior_student = prior
            if prior_fingerprint == fingerprint:
                # 批内完全相同的重放：与跨批重放同等对待。
                outcome.duplicates.append(event_id)
            else:
                outcome.conflicts.append(
                    ConflictDetail(
                        event_id=event_id,
                        reason="intra_batch_mismatch",
                        submitted_student_id=e["student_id"],
                        submitted_fingerprint=fingerprint,
                        stored_student_id=prior_student,
                        stored_fingerprint=prior_fingerprint,
                    )
                )
            continue

        stmt = (
            sqlite_insert(EventModel)
            .values(
                event_id=event_id,
                plan_version=plan_version,
                student_id=e["student_id"],
                event_type=e["event_type"],
                payload=e["payload"],
                content_hash=fingerprint,
            )
            .on_conflict_do_nothing(index_elements=["event_id", "plan_version"])
            .returning(EventModel.id)
        )
        inserted_id = db.execute(stmt).scalar_one_or_none()
        if inserted_id is not None:
            outcome.accepted.append(event_id)
            seen_in_batch[event_id] = (fingerprint, e["student_id"])
            continue

        stored = db.execute(
            select(EventModel.student_id, EventModel.content_hash).where(
                EventModel.plan_version == plan_version,
                EventModel.event_id == event_id,
            )
        ).first()
        if stored is not None and stored.content_hash == fingerprint:
            # 完全相同的跨批重放：幂等成功。
            outcome.duplicates.append(event_id)
            seen_in_batch[event_id] = (fingerprint, e["student_id"])
            continue

        # 同号不同内容：若读不到已存行（并发提交时序），同样按冲突失败安全处理。
        outcome.conflicts.append(
            ConflictDetail(
                event_id=event_id,
                reason="content_mismatch" if stored is not None else "concurrent_insert",
                submitted_student_id=e["student_id"],
                submitted_fingerprint=fingerprint,
                stored_student_id=stored.student_id if stored is not None else None,
                stored_fingerprint=stored.content_hash if stored is not None else None,
            )
        )

    if outcome.conflicts:
        # 原子拒绝：整批回滚，再单独提交审计，绝不留下半批数据。
        db.rollback()
        outcome.accepted = []
        outcome.duplicates = []
        _record_conflicts(
            db, batch_id=batch_id, plan_version=plan_version, conflicts=outcome.conflicts
        )
        return outcome

    db.commit()
    return outcome


def load_events(db: Session, plan_version: str) -> list[CoreEvent]:
    stmt = select(EventModel).where(EventModel.plan_version == plan_version)
    rows = db.execute(stmt).scalars().all()
    return [_to_core_event(r) for r in rows]


def load_events_up_to(
    db: Session, plan_version: str, max_event_id: str
) -> list[CoreEvent]:
    """执行确定性的业务处理。"""
    stmt = (
        select(EventModel)
        .where(EventModel.plan_version == plan_version)
        .where(EventModel.event_id <= max_event_id)
    )
    rows = db.execute(stmt).scalars().all()
    return [_to_core_event(r) for r in rows]


def max_event_id(db: Session, plan_version: str) -> str | None:
    stmt = (
        select(EventModel.event_id)
        .where(EventModel.plan_version == plan_version)
        .order_by(EventModel.event_id.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def get_freeze(
    db: Session, plan_version: str, freeze_id: str
) -> Freeze | None:
    return db.get(Freeze, (plan_version, freeze_id))


def insert_freeze(
    db: Session,
    *,
    plan_version: str,
    freeze_id: str,
    snapshot: dict[str, Any],
    event_cutoff_id: str | None,
) -> Freeze | None:
    """执行确定性的业务处理。"""
    stmt = sqlite_insert(Freeze).values(
        plan_version=plan_version,
        freeze_id=freeze_id,
        snapshot=snapshot,
        event_cutoff_id=event_cutoff_id,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_version", "freeze_id"]
    ).returning(Freeze.plan_version)
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is not None:
        return db.get(Freeze, (plan_version, freeze_id))
    return None
