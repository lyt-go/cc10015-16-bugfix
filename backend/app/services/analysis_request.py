"""分析申请领域服务：批准分析前必须取得可用于取样的库存资格。

审批人从申请详情出发，凭样品登记模块签发的入库凭据（库存资格回执）判断
申请样品是否真实入库、航次归属是否匹配：

* 登记簿查无样品编号：不能批准，提示申请人重新选样；
* 样品已登记但尚未入库：不能批准，提示等待库管完成入库；
* 本航次库存为零时，其他航次在库的同号样品不能补足本航次申请。

每次批准尝试都会留下审批回执（含失败答复），重查时可看到当时的答复；
批准成功后申请进入获批待领取队列，重复提交只记一次领用资格并返回真实入库关联。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.sample_registry import (
    CRUISE_MISMATCH,
    ELIGIBLE,
    NOT_CHECKED_IN,
    SAMPLE_NOT_FOUND,
    SampleRegistryService,
)
from app.store import store

# 两种可补办缺项 -> 申请人侧的补办指引
REMEDY_HINTS = {
    SAMPLE_NOT_FOUND: "请重新选择已登记的样品编号后再发起批准",
    NOT_CHECKED_IN: "请等待库管补报本批入库凭据、样品完成入库后再重新提交",
    CRUISE_MISMATCH: "请等待本航次样品入库，或重新选择本航次在库样品",
}


class AnalysisRequestService(BaseModuleService):
    MODULE = "analysis_request"
    ENTITY = "分析申请单"
    OPERATION_TERM = "提交申请"
    LIST_FIELDS = ["申请样品", "航次编号", "分析项目", "分析方法", "预期周期", "申请人", "审批人", "申请日期", "备注说明"]
    REQUIRED_FIELDS = ["申请样品", "航次编号", "分析项目", "分析方法"]
    STATUS_ORDER = ["草稿", "已提交", "已批准", "已驳回"]
    ACTION_RULES = {"提交申请": "已提交", "批准分析": "已批准", "驳回申请": "已驳回"}
    NEGATIVE_ACTIONS = ["驳回申请"]

    def __init__(self) -> None:
        self._registry_service = SampleRegistryService()

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        target = self.ACTION_RULES.get(action)
        if target is None:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

        # 已批准的申请重复提交：不重复记领用资格，直接返回既有入库关联
        if action == "批准分析" and entry.get("status") == "已批准":
            return self._replay_approved(entry_id)

        # 幂等检查
        idempotent_key = f"{self.MODULE}:{entry_id}:{action}"
        if store.is_idempotent(idempotent_key):
            return None, f"动作「{action}」已执行，请勿重复操作"

        if action == "批准分析":
            # 申请单维度加锁：与库管补报入库凭据交错提交时，校验与批准整体串行
            with store.named_lock(f"analysis_request:approve:{entry_id}"):
                return self._approve(entry_id, entry, idempotent_key)

        entry["status"] = target
        entry["pending"] = target in ("草稿", "已提交")
        entry["abnormal"] = action in self.NEGATIVE_ACTIONS

        store.mark_idempotent(idempotent_key)

        return entry, f"{self.ENTITY}已{action}"

    # ------------------------------------------------------------------
    # 批准分析：库存资格校验 + 审批回执 + 待领取队列
    # ------------------------------------------------------------------
    def _approve(
        self, entry_id: int, entry: dict[str, Any], idempotent_key: str
    ) -> tuple[dict[str, Any] | None, str]:
        sample_no = str(entry.get("申请样品") or "").strip()
        cruise_no = str(entry.get("航次编号") or "").strip()

        reason_code, message, credential = self._registry_service.evaluate_stock_eligibility(
            sample_no, cruise_no
        )

        if reason_code != ELIGIBLE:
            # 失败也要留回执：重查时能看到当时为什么不能批
            receipt = self._record_receipt(
                entry_id, entry, qualified=False, reason_code=reason_code,
                message=message, credential=None,
            )
            entry["补办原因码"] = reason_code
            entry["补办提示"] = REMEDY_HINTS[reason_code]
            entry["last_receipt_id"] = receipt["id"]
            return None, message

        # 取得真实入库关联后才允许批准
        entry["status"] = "已批准"
        entry["pending"] = False
        entry["abnormal"] = False
        entry["入库凭据号"] = credential["credential_no"]
        entry["库存资格回执号"] = f"AR-{entry_id:04d}"
        entry["关联登记记录"] = credential["registry_id"]
        entry["取样关联"] = {
            "sample_no": credential["sample_no"],
            "cruise_no": credential["cruise_no"],
            "credential_no": credential["credential_no"],
            "registry_id": credential["registry_id"],
        }
        entry.pop("补办原因码", None)
        entry.pop("补办提示", None)

        store.mark_idempotent(idempotent_key)

        receipt = self._record_receipt(
            entry_id, entry, qualified=True, reason_code=ELIGIBLE,
            message="已取得可用于取样的库存资格，批准通过",
            credential=credential,
        )
        entry["last_receipt_id"] = receipt["id"]

        # 只有真实入库且归属明确的样品才进入获批待领取队列；重复入队只记一次
        store.enqueue_pickup({
            "request_id": entry_id,
            "sample_no": credential["sample_no"],
            "cruise_no": credential["cruise_no"],
            "credential_no": credential["credential_no"],
            "registry_id": credential["registry_id"],
            "receipt_no": entry["库存资格回执号"],
            "status": "待领取",
        })

        return entry, f"{self.ENTITY}已批准分析，入库凭据「{credential['credential_no']}」可用于安排取样"

    def _replay_approved(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """批准回执丢失后的重复提交：返回真实入库关联，不重复记领用资格。"""
        receipt = store.latest_approval_receipt(entry_id)
        queue_before = len(store.pickup_queue())
        if receipt is not None and receipt.get("qualified"):
            credential = receipt.get("credential") or {}
            # 再次确认凭据仍真实存在
            fresh = store.find_stock_credential(int(receipt.get("registry_id", 0)))
            if fresh is None:
                return None, "原入库凭据已失效，请联系库管核实样品库存后重新提交"
            store.enqueue_pickup({
                "request_id": entry_id,
                "sample_no": fresh["sample_no"],
                "cruise_no": fresh["cruise_no"],
                "credential_no": fresh["credential_no"],
                "registry_id": fresh["registry_id"],
                "receipt_no": receipt.get("receipt_no"),
                "status": "待领取",
            })
            queue_after = len(store.pickup_queue())
            return store.find(self.MODULE, entry_id), (
                f"该申请已批准，入库凭据「{fresh['credential_no']}」仍然有效，"
                f"未重复记领用资格（队列记录数 {queue_before} → {queue_after}）"
            )
        return None, "该申请已处于已批准状态，但未找到有效审批回执，请联系审批人核实"

    def _record_receipt(
        self,
        entry_id: int,
        entry: dict[str, Any],
        *,
        qualified: bool,
        reason_code: str,
        message: str,
        credential: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """落一条审批回执，说明本次申请有没有取得能用于取样的库存资格。"""
        receipt_no = f"AR-{entry_id:04d}"
        return store.append_approval_receipt({
            "receipt_no": receipt_no,
            "request_id": entry_id,
            "sample_no": entry.get("申请样品", ""),
            "cruise_no": entry.get("航次编号", ""),
            "qualified": qualified,
            "reason_code": reason_code,
            "remedy_hint": REMEDY_HINTS.get(reason_code, ""),
            "message": message,
            "credential_no": credential["credential_no"] if credential else None,
            "registry_id": credential["registry_id"] if credential else None,
            "credential": credential,
        })

    def get_approval_receipt(self, entry_id: int) -> dict[str, Any] | None:
        """审批人从申请详情重查：返回最近一次库存资格答复及完整答复历史。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        history = store.approval_receipts(entry_id)
        latest = history[-1] if history else None
        return {
            "request_id": entry_id,
            "status": entry.get("status"),
            "sample_no": entry.get("申请样品", ""),
            "cruise_no": entry.get("航次编号", ""),
            "latest": latest,
            "history": history,
            "pickup_link": entry.get("取样关联"),
        }

    def approved_pickup_queue(self) -> list[dict[str, Any]]:
        """获批待领取队列：只包含持有真实入库凭据、归属明确的样品申请。"""
        return store.pickup_queue()
