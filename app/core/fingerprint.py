"""事件内容指纹：用于幂等判定的确定性哈希。

同一事件编号（event_id）的重放只有在内容完全一致时才视为幂等重复；
内容指纹不同即视为冲突。指纹只覆盖业务内容（类型、学员、负载），
不包含落库时间等非确定性字段。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_content(event_type: str, student_id: str, payload: dict[str, Any]) -> str:
    """生成事件内容的规范化 JSON 表示（键排序、紧凑分隔符）。"""
    return json.dumps(
        {"event_type": event_type, "student_id": student_id, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def event_fingerprint(event_type: str, student_id: str, payload: dict[str, Any]) -> str:
    """计算事件内容的 SHA-256 指纹（十六进制，64 字符）。"""
    canonical = canonical_content(event_type, student_id, payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
