"""请求分配业务规则：状态流转、字段校验与筛选口径都收在这里。

生命周期：草稿 → 策略验证中 → 已启用 → 已停用；停用后只能重新提交策略验证，
不能越级直接恢复为已启用。同一目标域名（规范化后）同一时刻只允许一个生效版本，
请求分配结果按「域名@版本」幂等保留，集群节点侧按结果中的策略版本占用/释放槽位。
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from app.services.worker import occupy_slots, online_workers, release_slots
from app.store import store

MODULE = "dispatch"
RESULT_MODULE = "dispatchresult"
REQUIRED_FIELDS = ["策略编号", "目标域名", "分配算法"]
STATUS_ORDER = ["草稿", "策略验证中", "已启用", "已停用"]
# 动作 -> (允许的来源状态集合, 目标状态)；不允许越级流转。
ACTION_RULES = {
    "提交策略验证": ({"草稿", "已停用"}, "策略验证中"),
    "启用策略": ({"策略验证中"}, "已启用"),
    "停用策略": ({"已启用"}, "已停用"),
}
NEGATIVE_ACTIONS = ["停用策略"]
NO_ONLINE_WORKER_REASON = "没有在线的采集节点，无法按策略版本占用节点槽位"

# 并发启用同一目标域名时串行化裁决，保证只留一个生效版本。
_lock = threading.RLock()


def normalize_domain(value: Any) -> str:
    """规范化目标域名：去空白、转小写、去协议头、去路径、去端口、去尾部根点。"""
    text = str(value or "").strip().lower()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0]
    text = text.split(":", 1)[0]
    return text.strip().rstrip(".")


def result_key(domain: str, entry: dict[str, Any], version: int) -> str:
    """请求分配结果的幂等键：同一域名同一来源策略同一版本只保留一条。"""
    return f"{domain}#p{entry.get('id')}@v{version}"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class DispatchService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        domain: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("策略编号", ""))]
        if domain:
            needle = normalize_domain(domain)
            rows = [row for row in rows if needle and needle in normalize_domain(row.get("目标域名"))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def list_results(
        self,
        *,
        domain: str | None = None,
        page: int = 1,
        size: int = 200,
    ) -> tuple[list[dict[str, Any]], int]:
        """唯一请求分配结果数据集：保留来源策略与处理结果，策略版本可追溯。"""
        rows = store.peek(RESULT_MODULE)
        if domain:
            needle = normalize_domain(domain)
            rows = [row for row in rows if normalize_domain(row.get("目标域名")) == needle]
        rows = sorted(rows, key=lambda row: int(row.get("id", 0)))
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
        entry["目标域名"] = normalize_domain(entry["目标域名"])
        entry["策略版本"] = 0
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        with _lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"分配策略 {entry_id} 不存在或已归档"
            rule = ACTION_RULES.get(action)
            if rule is None:
                return None, f"动作「{action}」不属于请求分配可执行范围"
            sources, target = rule
            if target not in STATUS_ORDER:
                return None, f"目标状态「{target}」不在允许的状态序列里"
            current = str(entry.get("status") or "")
            if action == "启用策略" and current == "已启用":
                # 幂等：重复启用同一版本不生成第二条生效记录。
                version = int(entry.get("策略版本") or 0)
                return entry, f"分配策略已启用（策略版本 v{version}），重复启用同一版本已幂等忽略"
            if current not in sources:
                return None, self._transition_error(action, current)
            if action == "提交策略验证":
                entry["策略版本"] = int(entry.get("策略版本") or 0) + 1
                self._apply_status(entry, target, action)
                return entry, f"分配策略已提交策略验证，当前策略版本 v{entry['策略版本']}"
            if action == "启用策略":
                return self._enable(entry)
            return self._disable(entry)

    def _transition_error(self, action: str, current: str) -> str:
        if action == "启用策略":
            if current == "草稿":
                return "分配策略仍是草稿，不能跨状态直接启用，请先提交策略验证"
            if current == "已停用":
                return "停用策略不能越级恢复，请先提交策略验证并等待验证通过"
        if action == "提交策略验证":
            if current == "策略验证中":
                return "分配策略已在策略验证中，请勿重复提交"
            if current == "已启用":
                return "分配策略已启用，如需变更请先停用策略再重新验证"
        if action == "停用策略":
            return f"分配策略当前状态「{current}」未启用，不能执行停用策略"
        return f"当前状态「{current}」不允许执行「{action}」"

    def _enable(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        domain = normalize_domain(entry.get("目标域名"))
        version = int(entry.get("策略版本") or 0)
        # 域名裁决：同一目标域名并发启用时只能留下一个生效版本，另一条必须拒绝。
        for other in store.rows(MODULE):
            if other is entry:
                continue
            if other.get("status") == "已启用" and normalize_domain(other.get("目标域名")) == domain:
                reason = f"目标域名「{domain}」已存在生效策略「{other.get('策略编号')}」，本次启用被拒绝，原生效策略保留"
                self._record_result(entry, domain, version, "已拒绝", reason, 0)
                entry["abnormal"] = True
                return None, reason
        # 集群节点侧裁决：节点同时离线时只留一个可核验的失败原因。
        if not online_workers():
            self._record_result(entry, domain, version, "启用失败", NO_ONLINE_WORKER_REASON, 0)
            entry["abnormal"] = True
            return None, NO_ONLINE_WORKER_REASON
        # 结果先同步到集群节点侧清单，再由其更新自身记录。
        result = self._record_result(entry, domain, version, "已生效", None, 0)
        occupied = occupy_slots(result)
        if occupied <= 0:
            self._record_result(entry, domain, version, "启用失败", NO_ONLINE_WORKER_REASON, 0)
            entry["abnormal"] = True
            return None, NO_ONLINE_WORKER_REASON
        result["占用节点数"] = occupied
        result["更新时间"] = _now()
        self._apply_status(entry, "已启用", "启用策略")
        return entry, f"分配策略已启用，策略版本 v{version} 已在 {occupied} 个在线节点占用槽位"

    def _disable(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        domain = normalize_domain(entry.get("目标域名"))
        version = int(entry.get("策略版本") or 0)
        result = self._record_result(entry, domain, version, "已停用", None, 0)
        # 结果先同步到集群节点侧清单释放槽位，再由其更新自身记录。
        released = release_slots(result)
        self._apply_status(entry, "已停用", "停用策略")
        return entry, f"分配策略已停用，已按策略版本 v{version} 释放 {released} 个节点槽位"

    def _record_result(
        self,
        entry: dict[str, Any],
        domain: str,
        version: int,
        outcome: str,
        reason: str | None,
        node_count: int,
    ) -> dict[str, Any]:
        """写入唯一请求分配结果：按幂等键去重，重复提交只保留一次。"""
        rows = store.rows(RESULT_MODULE)
        key = result_key(domain, entry, version)
        now = _now()
        for row in rows:
            if row.get("幂等键") == key:
                row.update({
                    "来源策略编号": entry.get("策略编号"),
                    "来源策略ID": entry.get("id"),
                    "处理结果": outcome,
                    "失败原因": reason,
                    "占用节点数": node_count,
                    "更新时间": now,
                })
                return row
        record = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "结果编号": "",
            "目标域名": domain,
            "来源策略编号": entry.get("策略编号"),
            "来源策略ID": entry.get("id"),
            "策略版本": version,
            "处理结果": outcome,
            "失败原因": reason,
            "占用节点数": node_count,
            "幂等键": key,
            "生成时间": now,
            "更新时间": now,
        }
        record["结果编号"] = f"DISR-{record['id']:04d}"
        rows.append(record)
        return record

    def _apply_status(self, entry: dict[str, Any], target: str, action: str) -> None:
        entry["status"] = target
        entry["pending"] = target in {"草稿", "策略验证中"}
        entry["abnormal"] = action in NEGATIVE_ACTIONS
