"""M7 公示与封存：资格材料封存、分摊公示、审计查阅。

本模块只读取既有资格判定结果并原样封存，不重新裁决、不改变 M2 的资格结论，
也不提供任何申诉或裁决入口。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from typing import Any

from app.errors import DomainError
from app.manifest import canonical_digest
from app.modules.m2_eligibility.core import SAMPLE as ELIGIBILITY_SAMPLE, evaluate

SEALED = "SEALED"
PUBLISHED = "PUBLISHED"
RETRACTED = "RETRACTED"

CENT = Decimal("0.01")


def batch_id_of(payload: dict) -> str:
    for key in ("batch_id", "run_id", "batch"):
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _plain(value: Any) -> Any:
    """只保留可 JSON 化的原始值，不改变证据内容。"""
    try:
        json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise DomainError("INVALID_EVIDENCE", f"资格材料无法序列化：{exc}") from exc
    return value


def _as_records(value: Any, field: str) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(x, dict) for x in value):
        raise DomainError("INVALID_EVIDENCE", f"{field} 必须是对象数组")
    return [_plain(x) for x in value]


def _evaluate_readonly(project: dict) -> dict:
    """沿用 M2 既有规则取得判定；只补一个用于判定的 ``id``，不改动原项目。

    这是封存的只读兜底：调用方已提交判定时绝不走这里，也不会回写 M2。
    """
    if "id" in project:
        return evaluate(project)
    identifier = _project_id(project) if any(
        key in project for key in ("project_id", "project", "key")) else None
    if identifier is None:
        raise DomainError("INVALID_EVIDENCE", "资格材料缺少项目编号")
    return evaluate({**project, "id": identifier})


def build_sealed(payload: dict | None, rule_version: str) -> dict:
    """把资格材料整理为不可变封存件。

    优先原样采信调用方提交的 ``decisions``；只有缺少判定时才用既有 M2
    规则对项目做一次性计算。封存不会写回或修改 M2 的任何结果。
    """
    payload = payload if isinstance(payload, dict) else {}
    projects = payload.get("projects")
    if projects is None:
        projects = payload.get("eligibility")
    if isinstance(projects, dict):
        projects = projects.get("projects")
    decisions = payload.get("decisions")

    if projects is None and decisions is None:
        projects = ELIGIBILITY_SAMPLE["projects"]
    projects = _as_records(projects, "projects")
    if decisions is None:
        decisions = [_evaluate_readonly(project) for project in projects]
    decisions = _as_records(decisions, "decisions")

    if not projects and not decisions:
        raise DomainError("EMPTY_EVIDENCE", "资格材料为空，无法封存")

    # 判定结论原样保留；这里只做只读校验，不做重排或重算。
    for decision in decisions:
        if "id" not in decision:
            raise DomainError("INVALID_EVIDENCE", "资格判定缺少项目编号")
        if "eligible" not in decision:
            raise DomainError("INVALID_EVIDENCE", "资格判定缺少结论")

    provided_version = payload.get("rule_version")
    if provided_version is not None:
        rule_version = str(provided_version)
    material = {
        "projects": projects,
        "decisions": decisions,
        "rule_version": rule_version,
    }
    digest = canonical_digest(material)
    batch_id = batch_id_of(payload) or ("seal-" + digest[:12])
    eligible_count = sum(1 for d in decisions if d.get("eligible") is True)
    return {
        "batch_id": batch_id,
        "digest": digest,
        "payload": material,
        "rule_version": rule_version,
        "project_count": len(projects),
        "decision_count": len(decisions),
        "eligible_count": eligible_count,
        "rejected_count": len(decisions) - eligible_count,
        "status": SEALED,
    }


def _amount(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise DomainError("INVALID_AMOUNT", "金额不能为空")
    try:
        amount = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError) as exc:
        raise DomainError("INVALID_AMOUNT", f"金额不合法：{value}") from exc
    if not amount.is_finite() or amount < 0:
        raise DomainError("INVALID_AMOUNT", f"金额不合法：{value}")
    try:
        return amount.quantize(CENT)
    except InvalidOperation as exc:
        raise DomainError("INVALID_AMOUNT", f"金额超出范围：{value}") from exc


def _project_id(item: dict) -> str:
    for key in ("project_id", "id", "project", "key", "项目编号", "项目"):
        if key in item and item[key] is not None:
            value = str(item[key])
            if value == "":
                raise DomainError("INVALID_PROJECT", "项目编号为空")
            return value
    raise DomainError("INVALID_PROJECT", "分摊记录缺少项目编号")


def _amount_of(item: dict) -> Any:
    for key in ("amount", "allocated", "funding", "grant", "value", "final_amount",
                "awarded", "金额", "获资金额"):
        if key in item:
            return item[key]
    raise DomainError("INVALID_AMOUNT", "分摊记录缺少金额")


def build_public_items(payload: dict | None) -> list[dict]:
    """从分摊结果中只提取“项目 + 获资金额”，其余字段一律丢弃。"""
    payload = payload if isinstance(payload, dict) else {}
    if not payload:
        # 与其它模块一致：空请求使用随仓样例分摊结果。
        from app.modules.m4_allocations.core import run as m4_run
        payload = {"allocations": m4_run({})["allocated"]}
    raw = None
    for key in ("allocations", "items", "allocated", "amounts", "rows", "results"):
        if key in payload and payload[key] is not None:
            raw = payload[key]
            break
    if raw is None:
        raise DomainError("EMPTY_DISCLOSURE", "没有可公示的分摊结果")

    pairs: list[tuple[str, Decimal]] = []
    if isinstance(raw, dict):
        for project_id, amount in raw.items():
            pairs.append((str(project_id), _amount(amount)))
    elif isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                raise DomainError("INVALID_DISCLOSURE", "分摊记录必须是对象")
            pairs.append((_project_id(entry), _amount(_amount_of(entry))))
    else:
        raise DomainError("INVALID_DISCLOSURE", "分摊结果格式不正确")

    if not pairs:
        raise DomainError("EMPTY_DISCLOSURE", "没有可公示的分摊结果")
    seen: set[str] = set()
    for project_id, _ in pairs:
        if project_id in seen:
            raise DomainError("DUPLICATE_PROJECT", f"项目重复：{project_id}")
        seen.add(project_id)

    return [
        {"project_id": project_id, "amount": f"{amount:.2f}"}
        for project_id, amount in sorted(pairs, key=lambda pair: pair[0])
    ]


def public_total(items: list[dict]) -> str:
    total = sum((Decimal(item["amount"]) for item in items), Decimal("0.00"))
    return f"{total:.2f}"


def request_fingerprint(batch_id: str, actor: str, role: str, reason: str,
                        request_id: str | None = None) -> str:
    """查阅请求指纹。

    携带请求编号时按其幂等去重；未携带时按批次、操作人、角色与理由派生，
    保证同一请求中断后重试不会重复留痕。
    """
    material = {"action": "original", "batch_id": batch_id, "actor": actor,
                "role": role, "reason": reason}
    if request_id:
        material["request_id"] = request_id
    return sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        .encode("utf-8")
    ).hexdigest()


def csv_text(items: list[dict]) -> str:
    import csv
    import io

    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=["project_id", "amount"], lineterminator="\n")
    writer.writeheader()
    writer.writerows(items)
    return out.getvalue()
