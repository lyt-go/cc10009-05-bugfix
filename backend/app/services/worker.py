"""采集节点业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

MODULE = "worker"
REQUIRED_FIELDS = ["节点编号", "主机地址", "并发槽位数"]
STATUS_ORDER = ["草稿", "注册中", "在线", "离线"]
ACTION_RULES = {"提交注册": "注册中", "标记在线": "在线", "标记离线": "离线"}
NEGATIVE_ACTIONS = []

# 集群节点侧清单字段：请求分配结果会按策略版本同步进每个节点记录。
LEDGER_FIELD = "分配清单"
SLOT_FIELD = "已占用槽位"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def online_workers() -> list[dict[str, Any]]:
    """当前在线、可占用槽位的节点。"""
    return [row for row in store.rows(MODULE) if row.get("status") == "在线"]


def active_slot_count(row: dict[str, Any]) -> int:
    return sum(1 for item in row.get(LEDGER_FIELD, []) if item.get("处理结果") == "已占用槽位")


def occupy_slots(result: dict[str, Any]) -> int:
    """读取请求分配结果中的策略版本，按该版本占用在线节点槽位。

    处理后的业务结果写入集群节点侧清单，请求分配结果保留在同一记录；
    同一幂等键重复占用只保留一次。返回本次覆盖的节点数。
    """
    key = str(result.get("幂等键") or "")
    if not key:
        return 0
    covered = 0
    for row in online_workers():
        ledger = row.setdefault(LEDGER_FIELD, [])
        if any(item.get("幂等键") == key and item.get("处理结果") == "已占用槽位" for item in ledger):
            covered += 1
            continue
        ledger.append({
            "幂等键": key,
            "策略编号": result.get("来源策略编号"),
            "策略版本": result.get("策略版本"),
            "目标域名": result.get("目标域名"),
            "处理结果": "已占用槽位",
            "占用槽位数": 1,
            "同步时间": _now(),
            "释放时间": None,
        })
        covered += 1
        row[SLOT_FIELD] = active_slot_count(row)
    return covered


def release_slots(result: dict[str, Any]) -> int:
    """按结果中的策略版本释放节点槽位，只释放该版本占用的槽位。返回释放的节点数。"""
    key = str(result.get("幂等键") or "")
    if not key:
        return 0
    released = 0
    for row in store.rows(MODULE):
        ledger = row.get(LEDGER_FIELD)
        if not ledger:
            continue
        for item in ledger:
            if item.get("幂等键") == key and item.get("处理结果") == "已占用槽位":
                item["处理结果"] = "已释放槽位"
                item["释放时间"] = _now()
                released += 1
        row[SLOT_FIELD] = active_slot_count(row)
    return released


class WorkerService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("节点编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"工作节点 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于采集节点可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"工作节点已{action}"
