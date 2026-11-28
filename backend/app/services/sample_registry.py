"""样品登记领域服务：样品全生命周期管理，接受采样、拖网、沉积物等模块自动登记。

「执行入库」会签发一份库存资格回执所用的入库凭据（stock credential）：
凭据记录样品编号与航次归属，是分析申请批准时唯一认可的入库关联。
同一登记记录重复入库只返回原凭据，不会重复签发。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 库存资格评估结论码：区分两种可补办的缺项
ELIGIBLE = "eligible"
SAMPLE_NOT_FOUND = "sample_not_found"          # 查无登记记录：请重新选样
NOT_CHECKED_IN = "not_checked_in"              # 有登记但尚未入库：请等待入库
CRUISE_MISMATCH = "cruise_mismatch"            # 同号样品在其他航次在库，本航次库存为零

REASON_MESSAGES = {
    SAMPLE_NOT_FOUND: "样品编号「{sample}」在样品登记簿中查无记录，请重新选择已登记样品后再提交审批",
    NOT_CHECKED_IN: "样品「{sample}」已登记但尚未入库（当前状态：{status}），请等待库管完成入库后再提交审批",
    CRUISE_MISMATCH: (
        "本航次「{cruise}」库存中没有样品「{sample}」的入库凭据；"
        "其他航次在库的同号样品不能补足本航次申请，请等待本航次入库或重新选样"
    ),
}


class SampleRegistryService(BaseModuleService):
    MODULE = "sample_registry"
    ENTITY = "样品登记簿"
    OPERATION_TERM = "确认登记"
    LIST_FIELDS = ["样品编号", "航次编号", "样品类型", "采集站位", "保存方式", "存放位置", "登记人", "登记日期", "备注说明"]
    REQUIRED_FIELDS = ["样品编号", "样品类型", "采集站位"]
    STATUS_ORDER = ["待登记", "已登记", "已入库", "已移交"]
    ACTION_RULES = {"确认登记": "已登记", "执行入库": "已入库", "移交样品": "已移交"}
    NEGATIVE_ACTIONS = []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        target = self.ACTION_RULES.get(action)
        if target is None:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

        # 幂等检查
        idempotent_key = f"{self.MODULE}:{entry_id}:{action}"
        if store.is_idempotent(idempotent_key):
            return None, f"动作「{action}」已执行，请勿重复操作"

        if action == "执行入库":
            cruise_no = str(entry.get("航次编号") or "").strip()
            if not cruise_no:
                return None, "入库必须登记航次编号，归属明确的样品才能签收入库凭据"
            # 登记记录级锁：与分析批准交错提交时，凭据签发整体串行
            with store.named_lock(f"sample_registry:checkin:{entry_id}"):
                return self._do_check_in(entry_id, entry, action, idempotent_key)

        old_status = entry.get("status")
        entry["status"] = target
        entry["pending"] = target in ("待登记", "已登记")
        entry["abnormal"] = action in self.NEGATIVE_ACTIONS

        store.mark_idempotent(idempotent_key)

        return entry, f"{self.ENTITY}已{action}"

    def _do_check_in(
        self,
        entry_id: int,
        entry: dict[str, Any],
        action: str,
        idempotent_key: str,
    ) -> tuple[dict[str, Any] | None, str]:
        """执行入库并签收入库凭据（持登记记录级锁调用）。"""
        entry["status"] = "已入库"
        entry["pending"] = False
        entry["abnormal"] = False

        store.mark_idempotent(idempotent_key)

        credential = self._issue_credential(entry)
        entry["入库凭据号"] = credential["credential_no"]
        entry["stock_credential_id"] = credential["id"]
        entry["库存资格"] = "有效"

        return entry, f"{self.ENTITY}已{action}"

    def _issue_credential(self, entry: dict[str, Any]) -> dict[str, Any]:
        """为登记记录签收入库凭据；重复入库幂等返回原凭据。"""
        registry_id = int(entry["id"])
        existing = store.find_stock_credential(registry_id)
        if existing is not None:
            return existing
        credential_no = f"SC-{registry_id:04d}"
        return store.issue_stock_credential({
            "credential_no": credential_no,
            "registry_id": registry_id,
            "sample_no": entry.get("样品编号", ""),
            "cruise_no": entry.get("航次编号", ""),
            "sample_type": entry.get("样品类型", ""),
            "station": entry.get("采集站位", ""),
            "location": entry.get("存放位置", ""),
            "status": "已入库",
        })

    # ------------------------------------------------------------------
    # 库存资格评估：分析申请审批的唯一入口
    # ------------------------------------------------------------------
    def evaluate_stock_eligibility(
        self, sample_no: str, cruise_no: str
    ) -> tuple[str, str, dict[str, Any] | None]:
        """评估样品在指定航次下是否具备可用于取样的库存资格。

        返回 (结论码, 面向申请人的提示, 命中的入库凭据或None)。
        结论码取值：eligible / sample_not_found / not_checked_in / cruise_mismatch。
        """
        sample_no = str(sample_no or "").strip()
        cruise_no = str(cruise_no or "").strip()

        registry_rows = store.find_by(self.MODULE, "样品编号", sample_no)
        if not registry_rows:
            return (
                SAMPLE_NOT_FOUND,
                REASON_MESSAGES[SAMPLE_NOT_FOUND].format(sample=sample_no),
                None,
            )

        # 只认本航次的登记记录；其他航次的同号样品不参与本航次资格判断
        own_rows = [
            row for row in registry_rows
            if str(row.get("航次编号") or "").strip() == cruise_no
        ]
        other_cruise_rows = [
            row for row in registry_rows
            if str(row.get("航次编号") or "").strip() not in ("", cruise_no)
        ]

        if not own_rows:
            # 本航次查无该样品登记：若其他航次有同号样品，明确不能跨航次补足
            if other_cruise_rows:
                return (
                    CRUISE_MISMATCH,
                    REASON_MESSAGES[CRUISE_MISMATCH].format(
                        cruise=cruise_no or "（未填写）", sample=sample_no
                    ),
                    None,
                )
            return (
                SAMPLE_NOT_FOUND,
                REASON_MESSAGES[SAMPLE_NOT_FOUND].format(sample=sample_no),
                None,
            )

        sample = own_rows[0]
        if sample.get("status") not in ("已入库", "已移交"):
            return (
                NOT_CHECKED_IN,
                REASON_MESSAGES[NOT_CHECKED_IN].format(
                    sample=sample_no, status=sample.get("status", "未知")
                ),
                None,
            )

        # 状态已入库还不够：必须挂着真实签发的入库凭据，且航次归属一致
        credentials = store.stock_credentials_for_sample(sample_no, cruise_no)
        if not credentials:
            # 本航次登记虽在，但凭据缺失（如库管正在补报）：等待入库，不能用他航次凭据
            if other_cruise_rows and any(
                row.get("status") in ("已入库", "已移交") for row in other_cruise_rows
            ):
                return (
                    CRUISE_MISMATCH,
                    REASON_MESSAGES[CRUISE_MISMATCH].format(
                        cruise=cruise_no or "（未填写）", sample=sample_no
                    ),
                    None,
                )
            return (
                NOT_CHECKED_IN,
                REASON_MESSAGES[NOT_CHECKED_IN].format(
                    sample=sample_no, status=sample.get("status", "未知")
                ),
                None,
            )

        credential = credentials[0]
        linked = store.find(self.MODULE, int(credential["registry_id"]))
        if linked is None or linked.get("status") not in ("已入库", "已移交"):
            return (
                NOT_CHECKED_IN,
                REASON_MESSAGES[NOT_CHECKED_IN].format(
                    sample=sample_no, status=(linked or {}).get("status", "未知")
                ),
                None,
            )
        return ELIGIBLE, "库存资格有效", credential

    def is_sample_checked_in(self, sample_no: str) -> bool:
        """检查样品是否已入库（不区分航次，保留给旧调用方）。"""
        existing = store.find_one_by(self.MODULE, "样品编号", sample_no)
        if existing and existing.get("status") == "已入库":
            return True
        return False

    def get_samples_by_station(self, station: str) -> list[dict[str, Any]]:
        """获取某站位下的所有样品。"""
        return store.find_by(self.MODULE, "采集站位", station)
