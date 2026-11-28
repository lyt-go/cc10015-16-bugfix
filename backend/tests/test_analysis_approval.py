# -*- coding: utf-8 -*-
"""分析申请批准 × 样品入库凭据的库存资格链路测试。

覆盖：
1. 修复“查无样品编号时空查询被当通过”的根因，区分两种可补办缺项；
2. 本航次库存为零时，其他航次在库的同号样品不能补足；
3. 入库凭据（库存资格回执）与审批回执的关联、重查与答复历史；
4. 申请人批准与库管补报入库凭据交错提交时的并发一致性；
5. 回执丢失后重复提交只记一次领用资格，返回真实入库关联；
6. 只有真实入库且归属明确的样品进入获批待领取队列。
"""
from __future__ import annotations

import threading

import pytest

from app.services.analysis_request import AnalysisRequestService
from app.services.sample_registry import (
    CRUISE_MISMATCH,
    NOT_CHECKED_IN,
    SAMPLE_NOT_FOUND,
    SampleRegistryService,
)
from app.store import store

CRUISE_A = "CRUI-NW-A"
CRUISE_B = "CRUI-NW-B"


@pytest.fixture(autouse=True)
def reset_store():
    store.reset()
    yield


def _add_registry(sample_no: str, cruise_no: str, status: str = "待登记") -> dict:
    rows = store.rows("sample_registry")
    entry = {
        "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
        "样品编号": sample_no,
        "航次编号": cruise_no,
        "样品类型": "海水样品",
        "采集站位": f"STAT-{sample_no}",
        "保存方式": "冷藏",
        "存放位置": "甲舱冷库",
        "登记人": "库管",
        "登记日期": "2026-11-28",
        "备注说明": "",
        "status": status,
        "pending": status in ("待登记", "已登记"),
        "abnormal": False,
    }
    rows.append(entry)
    return entry


def _add_request(sample_no: str, cruise_no: str, status: str = "已提交") -> dict:
    rows = store.rows("analysis_request")
    entry = {
        "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
        "申请样品": sample_no,
        "航次编号": cruise_no,
        "分析项目": "营养盐分析",
        "分析方法": "分光光度法",
        "预期周期": "5天",
        "申请人": "申请人甲",
        "审批人": "审批人乙",
        "申请日期": "2026-11-28",
        "备注说明": "",
        "status": status,
        "pending": status in ("草稿", "已提交"),
        "abnormal": False,
    }
    rows.append(entry)
    return entry


# ============================================================
# 场景一：缺失 NW22 —— 登记簿查无记录，必须拒绝并提示重新选样
# ============================================================
def test_missing_sample_nw22_rejected_and_hint_reselect():
    svc = AnalysisRequestService()
    req = _add_request("NW22", CRUISE_A)

    entry, msg = svc.run_action(req["id"], "批准分析")

    assert entry is None, "查无登记记录时不得批准"
    assert "重新选择" in msg or "重新选样" in msg, f"应提示重新选样：{msg}"

    # 申请状态保持已提交，未混入待领取队列
    refreshed = store.find("analysis_request", req["id"])
    assert refreshed["status"] == "已提交"
    assert svc.approved_pickup_queue() == []

    # 失败也有回执，重查能看到当时的答复与补办原因码
    receipt = svc.get_approval_receipt(req["id"])
    assert receipt is not None
    assert receipt["latest"]["qualified"] is False
    assert receipt["latest"]["reason_code"] == SAMPLE_NOT_FOUND
    assert len(receipt["history"]) == 1


# ============================================================
# 场景二：NW23 有登记未入库 —— 必须拒绝并提示等待入库，入库后可补办通过
# ============================================================
def test_registered_not_checked_in_nw23_wait_then_pass():
    svc = AnalysisRequestService()
    reg_svc = SampleRegistryService()
    registry = _add_registry("NW23", CRUISE_A, status="已登记")
    req = _add_request("NW23", CRUISE_A)

    entry, msg = svc.run_action(req["id"], "批准分析")
    assert entry is None, "已登记未入库不得批准"
    assert "等待" in msg and "入库" in msg, f"应提示等待入库：{msg}"

    receipt = svc.get_approval_receipt(req["id"])
    assert receipt["latest"]["reason_code"] == NOT_CHECKED_IN

    # 库管执行入库，签收入库凭据
    checked_in, checkin_msg = reg_svc.run_action(registry["id"], "执行入库")
    assert checked_in is not None, checkin_msg
    credential_no = checked_in["入库凭据号"]
    assert credential_no

    # 补办：重新批准应当通过，并关联真实入库凭据
    entry, msg = svc.run_action(req["id"], "批准分析")
    assert entry is not None, f"入库后补办批准应成功：{msg}"
    assert entry["status"] == "已批准"
    assert entry["入库凭据号"] == credential_no
    assert entry["取样关联"]["sample_no"] == "NW23"
    assert entry["取样关联"]["cruise_no"] == CRUISE_A

    # 重查回执：能同时看到失败答复和成功答复两段历史
    receipt = svc.get_approval_receipt(req["id"])
    assert len(receipt["history"]) == 2
    assert receipt["history"][0]["qualified"] is False
    assert receipt["latest"]["qualified"] is True
    assert receipt["latest"]["credential_no"] == credential_no
    assert receipt["pickup_link"]["credential_no"] == credential_no


# ============================================================
# 场景三：乙航次在库的同号 NW22 不能补足甲航次库存为零的申请
# ============================================================
def test_same_sample_no_other_cruise_cannot_cover():
    svc = AnalysisRequestService()
    reg_svc = SampleRegistryService()

    # 乙航次的 NW22 真实在库
    registry_b = _add_registry("NW22", CRUISE_B, status="已登记")
    checked_in, msg = reg_svc.run_action(registry_b["id"], "执行入库")
    assert checked_in is not None, msg

    # 甲航次申请 NW22，甲航次库存为零
    req = _add_request("NW22", CRUISE_A)
    entry, msg = svc.run_action(req["id"], "批准分析")

    assert entry is None, "跨航次同号样品不得补足本航次申请"
    receipt = svc.get_approval_receipt(req["id"])
    assert receipt["latest"]["reason_code"] == CRUISE_MISMATCH
    assert "不能补足" in msg

    # 甲航次自己的 NW22 入库后才能批
    registry_a = _add_registry("NW22", CRUISE_A, status="已登记")
    checked_in_a, msg = reg_svc.run_action(registry_a["id"], "执行入库")
    assert checked_in_a is not None, msg
    entry, msg = svc.run_action(req["id"], "批准分析")
    assert entry is not None, msg
    assert entry["取样关联"]["cruise_no"] == CRUISE_A
    assert entry["取样关联"]["registry_id"] == registry_a["id"]


def test_missing_in_own_cruise_but_exists_other_cruise_is_mismatch():
    """甲航次连登记记录都没有，乙航次同号在库：按跨航次不补足处理，而非笼统的未入库。"""
    reg_svc = SampleRegistryService()
    svc = AnalysisRequestService()

    registry_b = _add_registry("NW22", CRUISE_B, status="已登记")
    checked_in, msg = reg_svc.run_action(registry_b["id"], "执行入库")
    assert checked_in is not None, msg

    req = _add_request("NW22", CRUISE_A)
    entry, msg = svc.run_action(req["id"], "批准分析")
    assert entry is None
    receipt = svc.get_approval_receipt(req["id"])
    assert receipt["latest"]["reason_code"] == CRUISE_MISMATCH
    assert "不能补足" in msg


# ============================================================
# 交错提交：甲的 NW22 入库与批准并发，核对最终状态与取样关联
# ============================================================
def test_interleaved_checkin_and_approval_nw22():
    reg_svc = SampleRegistryService()
    svc = AnalysisRequestService()
    registry = _add_registry("NW22", CRUISE_A, status="已登记")
    req = _add_request("NW22", CRUISE_A)

    approve_started = threading.Event()
    release_checkin = threading.Event()
    outcomes: list[tuple[str, object, str]] = []

    def approve() -> None:
        approve_started.set()
        entry, msg = svc.run_action(req["id"], "批准分析")
        outcomes.append(("approve", entry, msg))

    def checkin() -> None:
        # 等批准线程先进入校验，再放行入库，制造交错
        approve_started.wait(timeout=2)
        release_checkin.wait(timeout=2)
        entry, msg = reg_svc.run_action(registry["id"], "执行入库")
        outcomes.append(("checkin", entry, msg))

    t_approve = threading.Thread(target=approve)
    t_checkin = threading.Thread(target=checkin)
    t_approve.start()
    t_checkin.start()
    release_checkin.set()
    t_approve.join(timeout=5)
    t_checkin.join(timeout=5)

    checkin_result = next(item for item in outcomes if item[0] == "checkin")
    approve_result = next(item for item in outcomes if item[0] == "approve")
    assert checkin_result[1] is not None, "入库应成功"

    refreshed = store.find("analysis_request", req["id"])
    credential = store.find_stock_credential(registry["id"])
    assert credential is not None

    if approve_result[1] is None:
        # 批准先于入库完成：本次拒绝并留 NOT_CHECKED_IN 回执，入库后补办必须成功
        assert refreshed["status"] == "已提交"
        receipt = svc.get_approval_receipt(req["id"])
        assert receipt["latest"]["reason_code"] == NOT_CHECKED_IN

        entry, msg = svc.run_action(req["id"], "批准分析")
        assert entry is not None, f"补办批准应成功：{msg}"
        refreshed = store.find("analysis_request", req["id"])
    else:
        # 入库先于批准校验完成：本次直接批准
        assert approve_result[1]["status"] == "已批准"

    # 无论交错顺序如何，最终只签发一份凭据、只产生一次领用资格
    assert refreshed["status"] == "已批准"
    assert refreshed["入库凭据号"] == credential["credential_no"]
    assert refreshed["取样关联"] == {
        "sample_no": "NW22",
        "cruise_no": CRUISE_A,
        "credential_no": credential["credential_no"],
        "registry_id": registry["id"],
    }
    queue = svc.approved_pickup_queue()
    queued = [item for item in queue if item["request_id"] == req["id"]]
    assert len(queued) == 1
    assert queued[0]["credential_no"] == credential["credential_no"]


# ============================================================
# 高并发：多线程同时批准同一申请 + 同时补报入库，最多记一次领用资格
# ============================================================
def test_concurrent_approval_and_checkin_single_qualification():
    reg_svc = SampleRegistryService()
    svc = AnalysisRequestService()
    registry = _add_registry("NW22", CRUISE_A, status="已登记")
    req = _add_request("NW22", CRUISE_A)

    barrier = threading.Barrier(6)
    errors: list[str] = []

    def worker(is_checkin: bool) -> None:
        barrier.wait()
        try:
            if is_checkin:
                reg_svc.run_action(registry["id"], "执行入库")
            else:
                svc.run_action(req["id"], "批准分析")
        except Exception as exc:  # pragma: no cover - 并发下不应抛异常
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(i == 0,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    assert store.find_stock_credential(registry["id"]) is not None
    # 入库完成后再补一次批准，保证最终获批
    entry, msg = svc.run_action(req["id"], "批准分析")
    assert entry is not None, f"最终应可批准：{msg}"

    queue = [item for item in svc.approved_pickup_queue() if item["request_id"] == req["id"]]
    assert len(queue) == 1, "同一申请的领用资格只能记一次"
    receipts = svc.get_approval_receipt(req["id"])["history"]
    success = [r for r in receipts if r["qualified"]]
    assert len(success) == 1, "只应有一条取得库存资格的成功回执"


# ============================================================
# 回执丢失后重复提交：返回真实入库关联，不重复记领用资格
# ============================================================
def test_resubmit_after_receipt_lost_returns_real_link_once():
    reg_svc = SampleRegistryService()
    svc = AnalysisRequestService()
    registry = _add_registry("NW22", CRUISE_A, status="已登记")
    checked_in, msg = reg_svc.run_action(registry["id"], "执行入库")
    assert checked_in is not None, msg
    req = _add_request("NW22", CRUISE_A)

    entry, msg = svc.run_action(req["id"], "批准分析")
    assert entry is not None, msg
    assert len(svc.approved_pickup_queue()) == 1

    # 模拟前端未收到批准回执（回执丢失），申请人重复点“批准分析”
    entry2, msg2 = svc.run_action(req["id"], "批准分析")
    assert entry2 is not None, f"重复提交应幂等返回：{msg2}"
    assert entry2["入库凭据号"] == checked_in["入库凭据号"]
    assert "未重复记领用资格" in msg2
    assert len(svc.approved_pickup_queue()) == 1, "队列不得出现第二条领用资格"

    # 返回的关联必须指向真实签发的入库凭据
    credential = store.find_stock_credential(registry["id"])
    assert entry2["取样关联"]["credential_no"] == credential["credential_no"]
    assert entry2["取样关联"]["registry_id"] == registry["id"]


# ============================================================
# 待领取队列只收真实入库且归属明确的样品
# ============================================================
def test_pickup_queue_only_contains_real_checked_in_samples():
    reg_svc = SampleRegistryService()
    svc = AnalysisRequestService()

    # 缺项申请（NW22 查无记录、NW23 未入库）均被拒绝
    req_missing = _add_request("NW22", CRUISE_A)
    nw23_registry = _add_registry("NW23", CRUISE_A, status="已登记")
    req_waiting = _add_request("NW23", CRUISE_A)

    assert svc.run_action(req_missing["id"], "批准分析")[0] is None
    assert svc.run_action(req_waiting["id"], "批准分析")[0] is None
    assert svc.approved_pickup_queue() == []

    # NW23 入库后获批，才进入队列
    checked_in, msg = reg_svc.run_action(nw23_registry["id"], "执行入库")
    assert checked_in is not None, msg
    approved, msg = svc.run_action(req_waiting["id"], "批准分析")
    assert approved is not None, msg

    queue = svc.approved_pickup_queue()
    assert len(queue) == 1
    item = queue[0]
    assert item["request_id"] == req_waiting["id"]
    assert item["sample_no"] == "NW23"
    assert item["cruise_no"] == CRUISE_A
    assert item["status"] == "待领取"
    # 队列项绑定的凭据在凭据表中真实存在
    assert store.find_stock_credential(item["registry_id"])["credential_no"] == item["credential_no"]


# ============================================================
# 入库必须登记航次编号：归属不明不得签收入库凭据
# ============================================================
def test_checkin_requires_cruise_ownership():
    reg_svc = SampleRegistryService()
    registry = _add_registry("NW24", "", status="已登记")

    entry, msg = reg_svc.run_action(registry["id"], "执行入库")
    assert entry is None, "缺少航次归属不得入库"
    assert "航次编号" in msg
    assert store.find_stock_credential(registry["id"]) is None

    registry["航次编号"] = CRUISE_A
    entry, msg = reg_svc.run_action(registry["id"], "执行入库")
    assert entry is not None, msg
    assert store.find_stock_credential(registry["id"]) is not None


# ============================================================
# 重查接口：从未审批过的申请也能查，history 为空
# ============================================================
def test_receipt_recheck_before_any_approval():
    svc = AnalysisRequestService()
    req = _add_request("NW25", CRUISE_A)

    receipt = svc.get_approval_receipt(req["id"])
    assert receipt["request_id"] == req["id"]
    assert receipt["latest"] is None
    assert receipt["history"] == []
    assert receipt["pickup_link"] is None
