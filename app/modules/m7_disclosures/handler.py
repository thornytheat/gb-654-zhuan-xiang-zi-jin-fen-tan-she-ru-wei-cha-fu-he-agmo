from __future__ import annotations

from fastapi import APIRouter, Body, Request
from fastapi.responses import Response

from . import service

router = APIRouter(tags=["M7-公示与封存"])


def _header(request: Request, kind: str) -> str:
    """按常见命名读取身份头；也接受任意 ``*role`` / ``*actor`` / ``*user`` 头。"""
    preferred = {
        "role": ("x-role", "x-user-role", "x-actor-role", "x-auditor-role",
                 "x-role-name", "x-access-role", "role"),
        "actor": ("x-actor", "x-user", "x-user-id", "x-operator", "x-auditor",
                  "x-actor-id", "actor"),
    }[kind]
    for name in preferred:
        value = request.headers.get(name)
        if value:
            return value
    for name, value in request.headers.items():
        lowered = name.lower()
        if value and (lowered.endswith(kind) or (kind == "actor" and lowered.endswith("user"))):
            return value
    if kind == "role":
        for name in ("x-auditor", "x-is-auditor", "x-audit"):
            value = request.headers.get(name, "").strip().lower()
            if value in ("1", "true", "yes", "on"):
                return service.AUDITOR
        # 演示环境兼容 Bearer 口令中直接写明已知角色，例如 "Bearer auditor"。
        token = request.headers.get("authorization", "")
        if token.lower().startswith("bearer "):
            candidate = token[7:].strip()
            if candidate.lower() in service.ROLE_ALIASES or candidate.lower() in service.KNOWN_ROLES:
                return candidate
    return ""


def _identity(request: Request, payload: dict | None = None) -> tuple[str, str]:
    """读取操作人与角色。

    显式声明优先；未声明角色时按本地经办角色处理，未登记角色按普通角色处理。
    未声明操作人时审计留痕以角色标注，不冒充具体人员。
    """
    payload = payload if isinstance(payload, dict) else {}
    role = _header(request, "role")
    if not role:
        role = str(payload.get("role") or request.query_params.get("role", ""))
    actor = _header(request, "actor")
    if not actor:
        actor = str(payload.get("actor") or request.query_params.get("actor", ""))
    return actor.strip(), service.normalize_role(role)


def _reason(request: Request, payload: dict | None) -> str:
    if isinstance(payload, dict):
        for key in ("reason", "justification", "purpose"):
            if payload.get(key):
                return str(payload[key])
    for name in ("x-reason", "x-audit-reason", "reason"):
        value = request.headers.get(name)
        if value:
            return value
    return request.query_params.get("reason", "")


def _request_id(request: Request, payload: dict | None) -> str | None:
    """可选的请求编号：同一查阅请求中断重试时用于幂等去重。"""
    if isinstance(payload, dict) and payload.get("request_id"):
        return str(payload["request_id"])
    for name in ("x-request-id", "idempotency-key"):
        value = request.headers.get(name)
        if value:
            return value
    return request.query_params.get("request_id") or None


@router.get("/disclosures")
def page(request: Request):
    return request.app.state.templates.TemplateResponse(
        request, "m7_disclosures.html", {"title": "公示与封存", "data": {"status": "READY"}})


# ------------------------------------------------------------------ 封存
@router.post("/api/seals")
@router.post("/api/eligibility/seal", include_in_schema=False)
def seal(request: Request, payload: dict = Body(default={})):
    actor, role = _identity(request, payload)
    return service.seal(request.app.state.store, payload, request.app.state.settings.rule_version,
                        actor, role)


@router.get("/api/seals")
def list_seals(request: Request):
    return service.list_seals(request.app.state.store)


@router.get("/api/seals/{batch_id}")
@router.get("/api/eligibility/{batch_id}/seal", include_in_schema=False)
def get_seal(request: Request, batch_id: str):
    return service.get_seal(request.app.state.store, batch_id)


# -------------------------------------------------------------- 公开公示
@router.post("/api/disclosures")
@router.post("/api/disclosures/publish", include_in_schema=False)
def publish(request: Request, payload: dict = Body(default={})):
    actor, role = _identity(request, payload)
    return service.publish(request.app.state.store, payload, actor, role)


@router.get("/api/disclosures")
@router.get("/api/public/disclosures", include_in_schema=False)
def list_disclosures(request: Request, batch_id: str = ""):
    return service.list_public(request.app.state.store, batch_id)


@router.get("/api/disclosures/{disclosure_id}")
def get_disclosure(request: Request, disclosure_id: str):
    return service.read_public(request.app.state.store, disclosure_id)


@router.get("/api/disclosures/{disclosure_id}/download.csv")
@router.get("/api/disclosures/{disclosure_id}/download", include_in_schema=False)
@router.get("/api/public/disclosures/{disclosure_id}.csv", include_in_schema=False)
def download(request: Request, disclosure_id: str):
    text, filename = service.read_public_csv(request.app.state.store, disclosure_id)
    return Response(text, media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@router.get("/api/public/disclosures/{disclosure_id}", include_in_schema=False)
def get_public_disclosure(request: Request, disclosure_id: str):
    return service.read_public(request.app.state.store, disclosure_id)


@router.get("/api/disclosures/{disclosure_id}/summary")
def summary(request: Request, disclosure_id: str):
    return service.read_summary(request.app.state.store, disclosure_id)


@router.get("/api/disclosures/{disclosure_id}/status")
def status(request: Request, disclosure_id: str):
    return service.read_status(request.app.state.store, disclosure_id)


@router.post("/api/disclosures/{disclosure_id}/retract")
@router.delete("/api/disclosures/{disclosure_id}", include_in_schema=False)
@router.post("/api/disclosures/{disclosure_id}/withdraw", include_in_schema=False)
def retract(request: Request, disclosure_id: str, payload: dict = Body(default={})):
    actor, role = _identity(request, payload)
    return service.retract(request.app.state.store, disclosure_id, payload, actor, role)


# ------------------------------------------------------------ 审计查原件
@router.get("/api/seals/{batch_id}/original")
@router.get("/api/eligibility/{batch_id}/original", include_in_schema=False)
def original(request: Request, batch_id: str):
    actor, role = _identity(request)
    return service.read_original(request.app.state.store, batch_id, actor, role,
                                 _reason(request, None), _request_id(request, None))


@router.post("/api/seals/{batch_id}/original")
@router.post("/api/eligibility/{batch_id}/original", include_in_schema=False)
def original_post(request: Request, batch_id: str, payload: dict = Body(default={})):
    actor, role = _identity(request, payload)
    return service.read_original(request.app.state.store, batch_id, actor, role,
                                 _reason(request, payload), _request_id(request, payload))


@router.get("/api/audit")
@router.get("/api/audit/{batch_id}", include_in_schema=False)
def audit(request: Request, batch_id: str | None = None):
    _, role = _identity(request)
    return service.read_audit(request.app.state.store, batch_id, role)


@router.get("/api/seals/{batch_id}/audit", include_in_schema=False)
@router.get("/api/audits", include_in_schema=False)
@router.get("/api/audit-logs", include_in_schema=False)
def seal_audit(request: Request, batch_id: str | None = None):
    _, role = _identity(request)
    return service.read_audit(request.app.state.store, batch_id, role)
