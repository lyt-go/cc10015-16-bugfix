"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
增加幂等检查、并发锁、跨模块状态同步、重试恢复和回滚能力。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {}
        self._idempotent_keys: set[str] = set()
        self._locks: dict[str, threading.Lock] = {}
        self._global_lock = threading.Lock()
        # 回滚日志：{transaction_id: {module, entry_id, before_snapshot}}
        self._rollback_log: dict[str, dict] = {}
        # 库存资格链路专用存储：入库凭据、审批回执、获批待领取队列
        self._stock_credentials: list[dict[str, Any]] = []
        self._approval_receipts: list[dict[str, Any]] = []
        self._pickup_queue: list[dict[str, Any]] = []
        # 重试配置
        self._retry_max: int = 3
        self._retry_delay: float = 0.01
        self._seed()

    def _seed(self) -> None:
        self._tables = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._idempotent_keys.clear()
        self._rollback_log.clear()
        self._stock_credentials.clear()
        self._approval_receipts.clear()
        self._pickup_queue.clear()
        self._seed_credentials_for_checked_in()

    def _seed_credentials_for_checked_in(self) -> None:
        """种子数据中预置为已入库/已移交的样品，补签与其状态一致的入库凭据。"""
        for row in self._tables.get("sample_registry", []):
            if row.get("status") not in ("已入库", "已移交"):
                continue
            cruise_no = str(row.get("航次编号") or "").strip()
            if not cruise_no:
                continue
            credential = self.issue_stock_credential({
                "credential_no": f"SC-{int(row['id']):04d}",
                "registry_id": int(row["id"]),
                "sample_no": row.get("样品编号", ""),
                "cruise_no": cruise_no,
                "sample_type": row.get("样品类型", ""),
                "station": row.get("采集站位", ""),
                "location": row.get("存放位置", ""),
                "status": row.get("status", "已入库"),
            })
            row["入库凭据号"] = credential["credential_no"]
            row["stock_credential_id"] = credential["id"]
            row["库存资格"] = "有效"

    def reset(self) -> None:
        """重置所有数据回到初始种子状态。"""
        self._seed()
        self._locks.clear()

    def named_lock(self, key: str) -> threading.Lock:
        """按业务键取锁，供跨模块动作（入库/批准交错提交）串行化。"""
        return self._get_lock(key)

    def _get_lock(self, key: str) -> threading.Lock:
        with self._global_lock:
            if key not in self._locks:
                self._locks[key] = threading.Lock()
            return self._locks[key]

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def atomic_update(self, module: str, entry_id: int, updates: dict[str, Any]) -> dict[str, Any] | None:
        """原子更新一条记录，返回更新后的记录或None。"""
        lock = self._get_lock(f"{module}:{entry_id}")
        with lock:
            row = self.find(module, entry_id)
            if row is None:
                return None
            row.update(updates)
            return dict(row)

    def atomic_append(self, module: str, entry: dict[str, Any]) -> dict[str, Any]:
        """原子追加一条记录。"""
        lock = self._get_lock(f"{module}:append")
        with lock:
            rows = self.rows(module)
            entry_copy = dict(entry)
            rows.append(entry_copy)
            return entry_copy

    def is_idempotent(self, key: str) -> bool:
        """检查幂等键是否已存在（不写入）。"""
        with self._global_lock:
            return key in self._idempotent_keys

    def mark_idempotent(self, key: str) -> None:
        """标记幂等键为已执行。"""
        with self._global_lock:
            self._idempotent_keys.add(key)

    def clear_idempotent_key(self, key: str) -> None:
        """清除幂等键（用于失败回滚）。"""
        with self._global_lock:
            self._idempotent_keys.discard(key)

    def find_by(self, module: str, field: str, value: Any) -> list[dict[str, Any]]:
        """按字段值查找记录。"""
        rows = self.rows(module)
        if value is None:
            return [row for row in rows if row.get(field) is None]
        return [row for row in rows if str(row.get(field, "")) == str(value)]

    def find_one_by(self, module: str, field: str, value: Any) -> dict[str, Any] | None:
        """按字段值查找第一条记录。"""
        results = self.find_by(module, field, value)
        return results[0] if results else None

    def snapshot(self, module: str) -> list[dict[str, Any]]:
        """返回模块的当前快照（浅拷贝）。"""
        return [dict(row) for row in self.rows(module)]

    def save_snapshot(self, transaction_id: str, module: str, entry_id: int) -> str:
        """保存操作前的快照，支持失败后回滚。"""
        row = self.find(module, entry_id)
        key = f"{module}:{entry_id}"
        if row is None:
            return transaction_id
        with self._global_lock:
            self._rollback_log[transaction_id] = {
                "module": module,
                "entry_id": entry_id,
                "before": dict(row),
            }
        return transaction_id

    def rollback(self, transaction_id: str) -> bool:
        """回滚到指定事务前的状态。"""
        with self._global_lock:
            log = self._rollback_log.pop(transaction_id, None)
            if log is None:
                return False
            row = self.find(log["module"], log["entry_id"])
            if row is not None:
                row.clear()
                row.update(log["before"])
        return True

    def retry(self, func, *args, max_retries: int | None = None, **kwargs) -> tuple[Any, bool]:
        """带重试机制执行操作。返回(结果, 是否成功)。"""
        retries = max_retries if max_retries is not None else self._retry_max
        last_error = None
        for attempt in range(retries):
            try:
                result = func(*args, **kwargs)
                return result, True
            except Exception as e:
                last_error = e
                if attempt < retries - 1:
                    time.sleep(self._retry_delay * (attempt + 1))
        return None, False

    # ------------------------------------------------------------------
    # 库存资格链路：入库凭据 / 审批回执 / 获批待领取队列
    # ------------------------------------------------------------------
    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    def issue_stock_credential(self, credential: dict[str, Any]) -> dict[str, Any]:
        """签发一份入库凭据。同一登记记录只允许一份凭据（重复入库直接返回旧凭据）。"""
        existing = self.find_stock_credential(credential["registry_id"])
        if existing is not None:
            return existing
        record = dict(credential)
        with self._global_lock:
            record.setdefault("id", max((int(r.get("id", 0)) for r in self._stock_credentials), default=0) + 1)
            record.setdefault("issued_at", self._now())
            self._stock_credentials.append(record)
        return dict(record)

    def find_stock_credential(self, registry_id: int) -> dict[str, Any] | None:
        for record in self._stock_credentials:
            if int(record.get("registry_id", 0)) == registry_id:
                return dict(record)
        return None

    def stock_credentials_for_sample(self, sample_no: str, cruise_no: str) -> list[dict[str, Any]]:
        """查同一样品编号在指定航次下的有效入库凭据；跨航次同号样品不会命中。"""
        return [
            dict(record)
            for record in self._stock_credentials
            if str(record.get("sample_no", "")) == str(sample_no)
            and str(record.get("cruise_no", "")) == str(cruise_no)
        ]

    def append_approval_receipt(self, receipt: dict[str, Any]) -> dict[str, Any]:
        """追加一条审批回执；同一申请单的回执按时间累积，重查时可看到历次答复。"""
        record = dict(receipt)
        with self._global_lock:
            record.setdefault("id", max((int(r.get("id", 0)) for r in self._approval_receipts), default=0) + 1)
            record.setdefault("replied_at", self._now())
            self._approval_receipts.append(record)
        return dict(record)

    def latest_approval_receipt(self, request_id: int) -> dict[str, Any] | None:
        for record in reversed(self._approval_receipts):
            if int(record.get("request_id", 0)) == request_id:
                return dict(record)
        return None

    def approval_receipts(self, request_id: int) -> list[dict[str, Any]]:
        return [
            dict(record)
            for record in self._approval_receipts
            if int(record.get("request_id", 0)) == request_id
        ]

    def enqueue_pickup(self, item: dict[str, Any]) -> dict[str, Any]:
        """进入获批待领取队列；同一申请单只入队一次。"""
        request_id = int(item.get("request_id", 0))
        with self._global_lock:
            for queued in self._pickup_queue:
                if int(queued.get("request_id", 0)) == request_id:
                    return dict(queued)
            record = dict(item)
            record.setdefault("queued_at", self._now())
            self._pickup_queue.append(record)
            return dict(record)

    def pickup_queue(self) -> list[dict[str, Any]]:
        return [dict(record) for record in self._pickup_queue]

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()