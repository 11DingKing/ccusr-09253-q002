"""事件内容指纹：用于幂等判定与冲突检测的确定性哈希。"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    """生成与键序无关、可跨进程复现的规范化 JSON 串。"""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def event_fingerprint(
    *, event_type: str, student_id: str, payload: dict[str, Any]
) -> str:
    """计算事件内容指纹。

    指纹覆盖事件类型、学员与业务载荷，但不包含 event_id / plan_version
    （它们是身份键而非内容）。同一事件编号携带不同学员或时间时，指纹
    必然不同，从而可被幂等层识别为冲突而非普通重复。
    """
    canonical = canonical_json(
        {
            "event_type": event_type,
            "student_id": student_id,
            "payload": payload,
        }
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
