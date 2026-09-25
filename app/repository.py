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
from .models import Freeze, ImportConflict, Plan


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


@dataclass(frozen=True)
class EventConflict:
    """同号事件的内容冲突详情（仅供服务层审计与脱敏响应使用）。"""

    event_id: str
    conflict_source: str  # "stored"：与已存储事件冲突；"in_batch"：批内同号冲突
    mismatched_fields: list[str]
    incoming_fingerprint: str
    stored_fingerprint: str
    incoming_student_id: str
    incoming_payload: dict[str, Any]


@dataclass
class StagedImport:
    """一次批量导入的暂存结果（尚未提交）。"""

    accepted: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    conflicts: list[EventConflict] = field(default_factory=list)


def _mismatched_fields(existing: EventModel, incoming: dict[str, Any]) -> list[str]:
    """比较字段名（绝不返回字段值，避免泄露已存储学员信息）。"""
    fields: list[str] = []
    if existing.event_type != incoming["event_type"]:
        fields.append("event_type")
    if existing.student_id != incoming["student_id"]:
        fields.append("student_id")
    if existing.payload != incoming["payload"]:
        fields.append("payload")
    return fields


def _stored_fingerprint(row: EventModel) -> str:
    # 兼容历史数据：缺失指纹时按已存储内容现算。
    return row.content_hash or event_fingerprint(
        row.event_type, row.student_id, dict(row.payload)
    )


def stage_events(
    db: Session,
    *,
    plan_version: str,
    events: list[dict[str, Any]],
) -> StagedImport:
    """在同一事务内暂存整批事件，调用方负责提交或回滚。

    采用“先插入、后判定”：插入命中唯一约束时回查已存行的内容指纹——
    指纹一致记为幂等重复；指纹不同记为冲突。这样并发重试在数据库
    锁序列化后总能看到已提交的结果，不会出现半批数据。
    一旦发现冲突，剩余事件只做分类不再写入，由调用方整体回滚。
    """
    result = StagedImport()
    own_inserts: dict[str, str] = {}
    for e in events:
        fingerprint = event_fingerprint(e["event_type"], e["student_id"], e["payload"])
        if not result.conflicts:
            stmt = sqlite_insert(EventModel).values(
                event_id=e["event_id"],
                plan_version=plan_version,
                student_id=e["student_id"],
                event_type=e["event_type"],
                payload=e["payload"],
                content_hash=fingerprint,
            )
            stmt = stmt.on_conflict_do_nothing(
                index_elements=["event_id", "plan_version"]
            ).returning(EventModel.id)
            inserted_id = db.execute(stmt).scalar_one_or_none()
            if inserted_id is not None:
                result.accepted.append(e["event_id"])
                own_inserts[e["event_id"]] = fingerprint
                continue
        existing = db.execute(
            select(EventModel).where(
                EventModel.event_id == e["event_id"],
                EventModel.plan_version == plan_version,
            )
        ).scalar_one_or_none()
        if existing is None:
            # 冲突判定模式下遇到批内新编号：整批将回滚，无需再写入。
            continue
        stored_fp = _stored_fingerprint(existing)
        if stored_fp == fingerprint:
            result.duplicates.append(e["event_id"])
            continue
        source = "in_batch" if own_inserts.get(e["event_id"]) == stored_fp else "stored"
        result.conflicts.append(
            EventConflict(
                event_id=e["event_id"],
                conflict_source=source,
                mismatched_fields=_mismatched_fields(existing, e),
                incoming_fingerprint=fingerprint,
                stored_fingerprint=stored_fp,
                incoming_student_id=e["student_id"],
                incoming_payload=e["payload"],
            )
        )
    return result


def record_import_conflicts(
    db: Session,
    *,
    plan_version: str,
    batch_id: str,
    conflicts: list[EventConflict],
) -> None:
    """写入冲突审计记录；由调用方在批次回滚后单独提交。"""
    for c in conflicts:
        db.add(
            ImportConflict(
                batch_id=batch_id,
                plan_version=plan_version,
                event_id=c.event_id,
                conflict_source=c.conflict_source,
                incoming_student_id=c.incoming_student_id,
                incoming_payload=c.incoming_payload,
                incoming_fingerprint=c.incoming_fingerprint,
                stored_fingerprint=c.stored_fingerprint,
                mismatched_fields=c.mismatched_fields,
            )
        )
    db.flush()


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
