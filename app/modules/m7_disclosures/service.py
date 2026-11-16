"""M7 公示与封存服务层。

角色模型（演示用请求身份，非生产认证）：

- ``operator``：未显式声明角色时的本地经办角色，可封存、公示、撤回。
- ``viewer``：普通角色，只能读取公示。
- ``auditor``：审计角色，凭理由查阅封存原件并留痕。
- ``admin``：管理角色，可执行经办动作并查阅原件。

权限边界只收紧、不放开：显式声明的普通角色不能执行经办或审计动作。
"""
from __future__ import annotations

from app.errors import DomainError
from . import core

OPERATOR = "operator"
VIEWER = "viewer"
AUDITOR = "auditor"
ADMIN = "admin"
KNOWN_ROLES = {OPERATOR, VIEWER, AUDITOR, ADMIN}

ROLE_ALIASES = {
    "审计": AUDITOR, "审计员": AUDITOR, "审计角色": AUDITOR, "audit": AUDITOR,
    "auditor": AUDITOR, "auditor_role": AUDITOR, "internal_audit": AUDITOR,
    "普通": VIEWER, "普通角色": VIEWER, "普通用户": VIEWER, "user": VIEWER,
    "viewer": VIEWER, "public": VIEWER, "normal": VIEWER, "regular": VIEWER,
    "管理员": ADMIN, "admin": ADMIN, "管理者": ADMIN, "administrator": ADMIN,
    "经办": OPERATOR, "经办人": OPERATOR, "operator": OPERATOR, "": OPERATOR,
}

# 公示内容公开可读；原件与留痕只对审计角色开放；经办动作不向显式普通角色开放。
ORIGINAL_ROLES = {AUDITOR, ADMIN}
AUDIT_ROLES = {AUDITOR, ADMIN}
OPERATE_ROLES = {OPERATOR, ADMIN}


def normalize_role(role: str | None) -> str:
    value = (role or "").strip().lower()
    if value in ROLE_ALIASES:
        return ROLE_ALIASES[value]
    if value in KNOWN_ROLES:
        return value
    # 未登记角色一律按普通角色处理，避免拼写错误放大权限。
    return VIEWER


def require_role(role: str, allowed: set[str], action: str) -> None:
    if role not in allowed:
        raise DomainError("FORBIDDEN", f"当前角色无权执行：{action}", 403)


# ------------------------------------------------------------------- 封存
def seal(store, payload: dict | None, rule_version: str, actor: str, role: str) -> dict:
    require_role(role, OPERATE_ROLES, "封存资格材料")
    built = core.build_sealed(payload, rule_version)
    stored, created = store.save_seal(built)
    view = seal_view(stored)
    return {"seal": view, "created": created, "actor": actor, **view}


def seal_view(seal: dict, include_original: bool = False) -> dict:
    view = {
        "batch_id": seal["batch_id"],
        "digest": seal["digest"],
        "rule_version": seal["rule_version"],
        "project_count": seal["project_count"],
        "decision_count": seal["decision_count"],
        "eligible_count": seal["eligible_count"],
        "rejected_count": seal["rejected_count"],
        "status": seal["status"],
        "created_at": seal["created_at"],
    }
    if include_original:
        view["original"] = seal["payload"]
    return view


def get_seal(store, batch_id: str) -> dict:
    seal = store.get_seal(batch_id)
    if seal is None:
        raise DomainError("SEAL_NOT_FOUND", "封存证据不存在", 404)
    view = seal_view(seal)
    return {"seal": view, **view}


def list_seals(store) -> dict:
    """封存清单：只给摘要与计数，原件仍需审计角色凭理由查阅。"""
    return {"seals": [seal_view(seal) for seal in store.list_seals()]}


# ------------------------------------------------------------------- 公示
def publish(store, payload: dict | None, actor: str, role: str) -> dict:
    require_role(role, OPERATE_ROLES, "公示分摊结果")
    payload = payload or {}
    items = core.build_public_items(payload)
    batch_id = core.batch_id_of(payload)
    if not batch_id:
        batch_id = "batch-" + core.canonical_digest(items)[:12]
    digest = core.canonical_digest(items)
    total = core.public_total(items)
    stored, created = store.create_disclosure(batch_id, items, digest, total)
    view = _public_view(stored, store.get_seal(batch_id))
    return {
        "disclosure": view,
        "created": created,
        "actor": actor,
        "download_url": view["download_url"],
        **view,
    }


def _public_view(record: dict, seal: dict | None) -> dict:
    return {
        "disclosure_id": record["disclosure_id"],
        "batch_id": record["batch_id"],
        "seq": record["seq"],
        "total": record["total"],
        "public_total": record["total"],
        "item_count": len(record["items"]),
        "items": record["items"],
        "public_items": record["items"],
        "status": record["status"],
        "created_at": record["created_at"],
        "seal_status": (seal or {}).get("status", "UNSEALED"),
        "download_available": True,
        "download_url": f"/api/disclosures/{record['disclosure_id']}/download.csv",
    }


def read_public(store, disclosure_id: str) -> dict:
    """读取公示。已撤回的公示只保留总额、批次和封存状态，不再返回明细。"""
    record = store.get_disclosure(disclosure_id)
    if record is None:
        raise DomainError("DISCLOSURE_NOT_FOUND", "公示不存在", 404)
    seal = store.get_seal(record["batch_id"])
    view = _public_view(record, seal) if record["status"] == core.PUBLISHED \
        else _summary_view(record, seal)
    return {"disclosure": view, **view}


def read_public_csv(store, disclosure_id: str) -> tuple[str, str]:
    """下载入口只对公示期内的公示有效；撤回后立即失效。"""
    record = store.get_disclosure(disclosure_id)
    if record is None:
        raise DomainError("DISCLOSURE_NOT_FOUND", "公示不存在", 404)
    if record["status"] != core.PUBLISHED:
        raise DomainError("DISCLOSURE_RETRACTED", "公示已撤回，旧下载入口失效", 410)
    return core.csv_text(record["items"]), f"{disclosure_id}.csv"


def _summary_view(record: dict, seal: dict | None) -> dict:
    """公开摘要：总额、批次、封存状态；撤回后不含明细也不给下载入口。"""
    published = record["status"] == core.PUBLISHED
    return {
        "disclosure_id": record["disclosure_id"],
        "batch_id": record["batch_id"],
        "seq": record["seq"],
        "total": record["total"],
        "public_total": record["total"],
        "item_count": len(record["items"]),
        "status": record["status"],
        "created_at": record["created_at"],
        "retracted_at": record["retracted_at"],
        "seal_status": (seal or {}).get("status", "UNSEALED"),
        "seal_digest": (seal or {}).get("digest"),
        "download_available": published,
        "download_url": (f"/api/disclosures/{record['disclosure_id']}/download.csv"
                         if published else None),
    }


def read_summary(store, disclosure_id: str) -> dict:
    record = store.get_disclosure(disclosure_id)
    if record is None:
        raise DomainError("DISCLOSURE_NOT_FOUND", "公示不存在", 404)
    view = _summary_view(record, store.get_seal(record["batch_id"]))
    return {"summary": view, **view}


def read_status(store, disclosure_id: str) -> dict:
    """只回读状态：公开总额、批次、公示状态与封存状态。"""
    view = read_summary(store, disclosure_id)["summary"]
    return {
        "disclosure_id": view["disclosure_id"],
        "batch_id": view["batch_id"],
        "public_total": view["public_total"],
        "total": view["total"],
        "status": view["status"],
        "seal_status": view["seal_status"],
        "seal_digest": view["seal_digest"],
    }


def list_public(store, batch_id: str) -> dict:
    """公示列表：已撤回条目降级为摘要，不返回明细分摊。"""
    records = store.list_disclosures(batch_id) if batch_id else store.list_disclosures()
    items = []
    for record in records:
        seal = store.get_seal(record["batch_id"])
        if record["status"] == core.PUBLISHED:
            items.append(_public_view(record, seal))
        else:
            items.append(_summary_view(record, seal))
    return {"batch_id": batch_id or None, "disclosures": items, "items": items}


def list_audit(store, batch_id: str | None, role: str) -> dict:
    require_role(role, AUDIT_ROLES, "查阅审计留痕")
    records = store.list_audit(batch_id)
    return {"batch_id": batch_id, "audit": records, "records": records,
            "count": len(records)}


def retract(store, disclosure_id: str, payload: dict | None,
            actor: str, role: str) -> dict:
    require_role(role, OPERATE_ROLES, "撤回公示")
    payload = payload or {}
    reason = ""
    for key in ("reason", "justification", "purpose"):
        if payload.get(key):
            reason = str(payload[key]).strip()
            break
    if not reason:
        reason = "未填写撤回理由"
    record, changed = store.retract_disclosure(disclosure_id, actor or OPERATOR, reason)
    return {"disclosure_id": record["disclosure_id"], "batch_id": record["batch_id"],
            "status": record["status"], "retracted_at": record["retracted_at"],
            "changed": changed, "download_url": None, "download_available": False}


# --------------------------------------------------------------- 审计查阅
def read_original(store, batch_id: str, actor: str, role: str, reason: str,
                  request_id: str | None = None) -> dict:
    """审计角色凭理由读取封存原件；先留痕，留痕失败则拒绝返回原文。"""
    require_role(role, ORIGINAL_ROLES, "查阅封存原件")
    reason = (reason or "").strip()
    if not reason:
        raise DomainError("REASON_REQUIRED", "查阅原件必须填写理由")
    # 未声明操作人时以角色标注，留痕仍完整但不冒充具体人员。
    actor = (actor or "").strip() or f"{role}:unspecified"

    seal = store.get_seal(batch_id)
    if seal is None:
        raise DomainError("SEAL_NOT_FOUND", "封存证据不存在", 404)

    fingerprint = core.request_fingerprint(batch_id, actor, role, reason,
                                           (request_id or "").strip() or None)
    try:
        # 阶段一：落 PENDING 留痕；写入失败则立即停止，绝不返回原文。
        audit, created = store.record_audit(
            fingerprint=fingerprint,
            batch_id=batch_id,
            disclosure_id=None,
            actor=actor,
            role=role,
            reason=reason,
            action="READ_ORIGINAL",
            content_digest=seal["digest"],
        )
        if audit["content_digest"] != seal["digest"]:
            raise DomainError("SEAL_CONFLICT", "封存证据摘要与留痕不一致，拒绝返回原件", 409)
        # 阶段二：补完留痕。中断后重试同一指纹只会命中已有记录并继续，不重复留痕。
        audit = store.complete_audit(fingerprint)
    except DomainError:
        raise
    except Exception as exc:  # 留痕失败必须拒绝返回原文
        raise DomainError("AUDIT_WRITE_FAILED", "审计留痕写入失败，拒绝返回原件", 503) from exc

    if audit["status"] != "RECORDED":
        raise DomainError("AUDIT_WRITE_FAILED", "审计留痕未完成，拒绝返回原件", 503)

    # 撤回只改变公示状态，封存证据保持可审计。
    view = seal_view(seal, include_original=True)
    return {
        "batch_id": batch_id,
        "seal": view,
        **view,
        "audit": {"record_id": audit["record_id"], "fingerprint": audit["fingerprint"],
                  "created": created, "created_at": audit["created_at"]},
    }


def read_audit(store, batch_id: str | None, role: str) -> dict:
    return list_audit(store, batch_id, role)
