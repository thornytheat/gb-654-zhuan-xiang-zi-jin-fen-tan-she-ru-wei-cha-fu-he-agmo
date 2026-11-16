"""M7 公示与封存：封存、公示、撤回、审计留痕与权限边界。"""
import json
import sqlite3

from app.modules.m7_disclosures import core


PROJECTS = [
    {"project_id": "000101", "region": "rural", "score": 82, "blacklisted": False},
    {"project_id": "000102", "region": "urban", "score": 95, "blacklisted": True},
    {"project_id": "000103", "region": "rural", "score": 55, "blacklisted": False},
]
DECISIONS = [
    {"id": "000101", "eligible": True, "reason": "RURAL"},
    {"id": "000102", "eligible": False, "reason": "BLACKLIST"},
    {"id": "000103", "eligible": False, "reason": "LOW_SCORE"},
]
ALLOCATIONS = {
    "A": "300.00",
    "B": "420.00",
    "C": "280.00",
}


def seed(client, batch_id="B-001"):
    seal = client.post("/api/seals", json={
        "batch_id": batch_id,
        "projects": PROJECTS,
        "decisions": DECISIONS,
    })
    assert seal.status_code == 200, seal.text
    publish = client.post("/api/disclosures", json={
        "batch_id": batch_id,
        "allocations": ALLOCATIONS,
        "decisions": DECISIONS,
    })
    assert publish.status_code == 200, publish.text
    return seal.json()["seal"], publish.json()["disclosure"]


# --------------------------------------------------------------- 封存
def test_seal_preserves_decisions_verbatim(client):
    seal, _ = seed(client)
    assert seal["decision_count"] == 3
    assert seal["eligible_count"] == 1
    assert seal["rejected_count"] == 2


def test_seal_is_idempotent_for_same_material(client):
    first, _ = seed(client)
    second = client.post("/api/seals", json={
        "batch_id": "B-001", "projects": PROJECTS, "decisions": DECISIONS}).json()
    assert second["created"] is False
    assert second["seal"]["digest"] == first["digest"]


def test_seal_rejects_overwrite_with_different_material(client):
    seed(client)
    conflicting = client.post("/api/seals", json={
        "batch_id": "B-001", "decisions": [{"id": "A", "eligible": True}]})
    assert conflicting.status_code == 409
    assert conflicting.json()["error"] == "SEAL_CONFLICT"


def test_seal_does_not_recompute_when_decisions_supplied(client):
    # 判定结论原样采信：即便与 M2 默认规则不符也不重算。
    response = client.post("/api/seals", json={
        "batch_id": "B-X",
        "decisions": [{"id": "A", "eligible": True, "reason": "MANUAL"},
                      {"id": "B", "eligible": False, "reason": "MANUAL"}],
    })
    assert response.status_code == 200
    original = client.post("/api/seals/B-X/original",
                           headers={"x-actor": "a1", "x-role": "auditor"},
                           json={"reason": "复核"}).json()["seal"]["original"]
    assert [d["reason"] for d in original["decisions"]] == ["MANUAL", "MANUAL"]


def test_seal_empty_evidence_rejected(client):
    response = client.post("/api/seals", json={"batch_id": "EMPTY", "decisions": []})
    assert response.status_code == 422
    assert response.json()["error"] == "EMPTY_EVIDENCE"


# --------------------------------------------------------------- 公示
def test_disclosure_only_has_project_and_amount(client):
    _, disclosure = seed(client)
    assert disclosure["items"] == [
        {"project_id": "A", "amount": "300.00"},
        {"project_id": "B", "amount": "420.00"},
        {"project_id": "C", "amount": "280.00"},
    ]
    assert disclosure["total"] == "1000.00"
    assert disclosure["seal_status"] == "SEALED"


def test_disclosure_drops_eligibility_and_reasons(client):
    response = client.post("/api/disclosures", json={
        "batch_id": "B-002",
        "allocations": [
            {"project_id": "A", "amount": "10.00", "reason": "BLACKLIST",
             "score": 12, "blacklisted": True},
        ],
    })
    disclosure = response.json()["disclosure"]
    payload = json.dumps(disclosure, ensure_ascii=False)
    assert "BLACKLIST" not in payload
    assert "blacklisted" not in payload
    assert disclosure["items"] == [{"project_id": "A", "amount": "10.00"}]


def test_disclosure_computes_total_from_items(client):
    disclosure = client.post("/api/disclosures", json={
        "allocations": {"A": "0.10", "B": "0.20"}}).json()["disclosure"]
    assert disclosure["total"] == "0.30"


def test_disclosure_rejects_duplicate_project(client):
    response = client.post("/api/disclosures", json={
        "batch_id": "DUP",
        "allocations": [{"id": "A", "amount": "1.00"}, {"id": "A", "amount": "2.00"}],
    })
    assert response.status_code == 422
    assert response.json()["error"] == "DUPLICATE_PROJECT"


def test_disclosure_rejects_negative_amount(client):
    response = client.post("/api/disclosures", json={
        "batch_id": "NEG", "allocations": [{"id": "A", "amount": "-1"}]})
    assert response.status_code == 422
    assert response.json()["error"] == "INVALID_AMOUNT"


def test_public_read_and_download(client):
    _, disclosure = seed(client)
    public = client.get(f"/api/disclosures/{disclosure['disclosure_id']}").json()
    assert public["disclosure"]["items"] == disclosure["items"]
    download = client.get(f"/api/disclosures/{disclosure['disclosure_id']}/download.csv")
    assert download.status_code == 200
    assert download.text.splitlines()[0] == "project_id,amount"
    assert "300.00" in download.text


# --------------------------------------------------------------- 撤回
def test_retract_invalidates_download_immediately(client):
    _, disclosure = seed(client)
    path = f"/api/disclosures/{disclosure['disclosure_id']}/download.csv"
    assert client.get(path).status_code == 200
    retract = client.post(f"/api/disclosures/{disclosure['disclosure_id']}/retract",
                          json={"reason": "复核发现口径错误"})
    assert retract.status_code == 200
    assert retract.json()["status"] == "RETRACTED"
    assert client.get(path).status_code == 410


def test_retracted_summary_keeps_total_batch_and_seal(client):
    seal, disclosure = seed(client)
    client.post(f"/api/disclosures/{disclosure['disclosure_id']}/retract",
                json={"reason": "口径修正"})
    summary = client.get(f"/api/disclosures/{disclosure['disclosure_id']}").json()["disclosure"]
    assert summary["total"] == "1000.00"
    assert summary["batch_id"] == "B-001"
    assert summary["status"] == "RETRACTED"
    assert summary["seal_status"] == "SEALED"
    assert summary["seal_digest"] == seal["digest"]
    assert "items" not in summary
    assert summary["download_url"] is None


def test_retract_without_reason_still_works(client):
    """撤回只要求权限；未填理由时留一个显式占位，不阻塞撤回。"""
    _, disclosure = seed(client)
    response = client.post(f"/api/disclosures/{disclosure['disclosure_id']}/retract", json={})
    assert response.status_code == 200
    assert response.json()["status"] == "RETRACTED"


def test_retract_is_idempotent(client):
    _, disclosure = seed(client)
    url = f"/api/disclosures/{disclosure['disclosure_id']}/retract"
    first = client.post(url, json={"reason": "撤回"}).json()
    second = client.post(url, json={"reason": "撤回"}).json()
    assert first["changed"] is True
    assert second["changed"] is False
    assert second["status"] == "RETRACTED"


def test_republish_after_retract_gets_new_entry(client):
    _, disclosure = seed(client)
    client.post(f"/api/disclosures/{disclosure['disclosure_id']}/retract", json={"reason": "改口径"})
    again = client.post("/api/disclosures", json={"batch_id": "B-001", "allocations": ALLOCATIONS})
    assert again.status_code == 200
    new = again.json()["disclosure"]
    assert new["disclosure_id"] != disclosure["disclosure_id"]
    assert new["status"] == "PUBLISHED"
    assert client.get(f"/api/disclosures/{new['disclosure_id']}/download.csv").status_code == 200
    assert client.get(f"/api/disclosures/{disclosure['disclosure_id']}/download.csv").status_code == 410


def test_retracted_seal_still_auditable(client):
    seal, disclosure = seed(client)
    client.post(f"/api/disclosures/{disclosure['disclosure_id']}/retract", json={"reason": "撤回"})
    original = client.post("/api/seals/B-001/original",
                           headers={"x-actor": "auditor-1", "x-role": "auditor"},
                           json={"reason": "撤回后追责"}).json()
    assert original["seal"]["digest"] == seal["digest"]
    assert original["seal"]["original"]["decisions"] == DECISIONS


# --------------------------------------------------------------- 权限
def test_viewer_cannot_read_original(client):
    seed(client)
    response = client.post("/api/seals/B-001/original",
                           headers={"x-actor": "v1", "x-role": "viewer"},
                           json={"reason": "想看"})
    assert response.status_code == 403
    assert response.json()["error"] == "FORBIDDEN"
    assert client.get("/api/audit").status_code == 403


def test_unknown_role_treated_as_ordinary(client):
    seed(client)
    response = client.get("/api/seals/B-001/original", params={"reason": "想看", "actor": "u1"},
                          headers={"x-role": "superuser"})
    assert response.status_code == 403


def test_viewer_cannot_retract_or_publish(client):
    _, disclosure = seed(client)
    assert client.post(f"/api/disclosures/{disclosure['disclosure_id']}/retract",
                       headers={"x-role": "viewer"},
                       json={"reason": "越权"}).status_code == 403
    assert client.post("/api/disclosures",
                       headers={"x-role": "viewer"},
                       json={"allocations": ALLOCATIONS}).status_code == 403


def test_viewer_can_read_public_disclosure(client):
    _, disclosure = seed(client)
    assert client.get(f"/api/disclosures/{disclosure['disclosure_id']}",
                      headers={"x-role": "viewer"}).status_code == 200


def test_original_requires_reason(client):
    seed(client)
    response = client.post("/api/seals/B-001/original",
                           headers={"x-actor": "a1", "x-role": "auditor"}, json={})
    assert response.status_code == 422
    assert response.json()["error"] == "REASON_REQUIRED"


# --------------------------------------------------------------- 留痕
def test_auditor_read_leaves_trace(client):
    seed(client)
    response = client.post("/api/seals/B-001/original",
                           headers={"x-actor": "auditor-1", "x-role": "auditor"},
                           json={"reason": "核对获批金额"})
    assert response.status_code == 200
    audit = client.get("/api/audit", headers={"x-role": "auditor"}).json()["audit"]
    assert len(audit) == 1
    record = audit[0]
    assert record["actor"] == "auditor-1"
    assert record["reason"] == "核对获批金额"
    assert record["action"] == "READ_ORIGINAL"
    assert record["status"] == "RECORDED"
    assert record["content_digest"] == response.json()["seal"]["digest"]


def test_retry_after_interruption_does_not_duplicate_trace(client):
    seed(client)
    headers = {"x-actor": "auditor-1", "x-role": "auditor"}
    body = {"reason": "同一理由重试"}
    first = client.post("/api/seals/B-001/original", headers=headers, json=body).json()
    second = client.post("/api/seals/B-001/original", headers=headers, json=body).json()
    assert first["audit"]["record_id"] == second["audit"]["record_id"]
    assert second["audit"]["created"] is False
    assert len(client.get("/api/audit", headers=headers).json()["audit"]) == 1


def test_pending_trace_is_recovered_without_duplicate(client):
    """模拟阶段一成功、阶段二前中断：只留下 PENDING，重试补完且不新增记录。"""
    seed(client)
    store = client.app.state.store
    fingerprint = core.request_fingerprint("B-001", "auditor-1", "auditor", "中断恢复")
    store.record_audit(fingerprint, "B-001", None, "auditor-1", "auditor", "中断恢复",
                       "READ_ORIGINAL", store.get_seal("B-001")["digest"])
    assert store.count_audit() == 1
    response = client.post("/api/seals/B-001/original",
                           headers={"x-actor": "auditor-1", "x-role": "auditor"},
                           json={"reason": "中断恢复"})
    assert response.status_code == 200
    assert store.count_audit() == 1
    assert store.list_audit()[0]["status"] == "RECORDED"
    assert response.json()["audit"]["created"] is False


def test_audit_write_failure_refuses_original(client, monkeypatch):
    seed(client)

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(client.app.state.store, "record_audit", broken)
    response = client.post("/api/seals/B-001/original",
                           headers={"x-actor": "auditor-1", "x-role": "auditor"},
                           json={"reason": "留痕失败"})
    assert response.status_code == 503
    assert response.json()["error"] == "AUDIT_WRITE_FAILED"
    assert "original" not in response.text
    assert client.app.state.store.count_audit() == 0


def test_audit_completion_failure_refuses_original(client, monkeypatch):
    seed(client)

    def broken(fingerprint):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(client.app.state.store, "complete_audit", broken)
    response = client.post("/api/seals/B-001/original",
                           headers={"x-actor": "auditor-1", "x-role": "auditor"},
                           json={"reason": "留痕未完成"})
    assert response.status_code == 503
    assert "projects" not in response.text


# --------------------------------------------------------- 并发与不扩展
def test_publish_and_retract_are_isolated_by_permission(client):
    """并发撤回与查阅：撤回不改变封存件，审计读取仍可用且留痕完整。"""
    seal, disclosure = seed(client)
    client.post(f"/api/disclosures/{disclosure['disclosure_id']}/retract",
                headers={"x-role": "admin"}, json={"reason": "撤回"})
    original = client.post("/api/seals/B-001/original",
                           headers={"x-actor": "auditor-1", "x-role": "auditor"},
                           json={"reason": "并发核对"}).json()
    assert original["seal"]["digest"] == seal["digest"]
    audit = client.get("/api/audit", headers={"x-role": "auditor"}).json()["audit"]
    assert [r["action"] for r in audit] == ["READ_ORIGINAL"]


def test_seal_and_disclosure_do_not_touch_m2(client):
    before = client.post("/api/eligibility/evaluate", json={}).json()
    seed(client)
    after = client.post("/api/eligibility/evaluate", json={}).json()
    assert before == after


def test_no_appeal_endpoint_added(client):
    for path in ("/api/appeals", "/api/eligibility/appeal", "/api/decisions"):
        assert client.post(path, json={}).status_code in (404, 405)


def test_public_payload_never_contains_original_material(client):
    seed(client)
    public = client.get("/api/disclosures").json()
    dumped = json.dumps(public, ensure_ascii=False)
    assert "blacklisted" not in dumped
    assert "BLACKLIST" not in dumped
    assert "score" not in dumped


def test_download_alias_points_to_same_csv(client):
    _, disclosure = seed(client)
    alias = client.get(f"/api/public/disclosures/{disclosure['disclosure_id']}.csv")
    assert alias.status_code == 200
    assert alias.text.splitlines()[0] == "project_id,amount"


def test_original_via_query_identity(client):
    seed(client)
    response = client.get("/api/seals/B-001/original",
                          params={"reason": "查询复核", "actor": "auditor-9", "role": "auditor"})
    assert response.status_code == 200
    assert response.json()["audit"]["fingerprint"]


def test_concurrent_retract_and_audit_are_isolated(client):
    """并发撤回与审计查阅：各自按权限与事务边界完成，互不篡改对方结果。"""
    import threading

    seal, disclosure = seed(client)
    audit_headers = {"x-actor": "auditor-c", "x-role": "auditor"}
    results: dict[str, object] = {}
    barrier = threading.Barrier(2)

    def retract_worker():
        barrier.wait()
        results["retract"] = client.post(
            f"/api/disclosures/{disclosure['disclosure_id']}/retract",
            headers={"x-role": "admin"}, json={"reason": "并发撤回"}).status_code

    def audit_worker():
        barrier.wait()
        response = client.post("/api/seals/B-001/original", headers=audit_headers,
                               json={"reason": "并发查阅"})
        results["audit"] = (response.status_code, response.json())

    threads = [threading.Thread(target=retract_worker), threading.Thread(target=audit_worker)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert results["retract"] == 200
    assert results["audit"][0] == 200
    assert results["audit"][1]["seal"]["digest"] == seal["digest"]
    audit = client.get("/api/audit", headers=audit_headers).json()["audit"]
    assert len(audit) == 1 and audit[0]["status"] == "RECORDED"
    # 撤回后旧下载入口失效，封存件摘要不变。
    assert client.get(f"/api/disclosures/{disclosure['disclosure_id']}/download.csv").status_code == 410
    current = client.get("/api/seals/B-001").json()["seal"]
    assert current["digest"] == seal["digest"] and current["status"] == "SEALED"


# --------------------------------------------------------- 与既有模块衔接
def test_seal_accepts_m2_evaluate_output_verbatim(client):
    from app.modules.m2_eligibility.core import run as m2_run

    eligibility = m2_run({})
    sealed = client.post("/api/seals", json={
        "batch_id": "B-M2", "decisions": eligibility["decisions"]}).json()
    assert sealed["seal"]["eligible_count"] == eligibility["valid_count"]
    assert sealed["seal"]["rejected_count"] == eligibility["quarantine_count"]


def test_disclosure_accepts_m4_allocated_output(client):
    from app.modules.m4_allocations.core import run as m4_run

    allocation = m4_run({})
    disclosure = client.post("/api/disclosures", json={
        "batch_id": "B-M4", "allocated": allocation["allocated"]}).json()["disclosure"]
    assert disclosure["total"] == allocation["allocated_total"]
    assert {item["project_id"]: item["amount"] for item in disclosure["items"]} == allocation["allocated"]


def test_public_items_reject_non_dict_entries(client):
    response = client.post("/api/disclosures", json={"batch_id": "X", "allocations": ["A", "B"]})
    assert response.status_code == 422
    assert response.json()["error"] == "INVALID_DISCLOSURE"


def test_request_id_makes_retry_idempotent(client):
    seed(client)
    headers = {"x-actor": "auditor-r", "x-role": "auditor", "x-request-id": "req-42"}
    first = client.post("/api/seals/B-001/original", headers=headers,
                        json={"reason": "请求重放"}).json()
    second = client.post("/api/seals/B-001/original", headers=headers,
                         json={"reason": "请求重放"}).json()
    assert first["audit"]["record_id"] == second["audit"]["record_id"]
    assert client.app.state.store.count_audit() == 1


def test_original_is_not_publicly_reachable_without_reason(client):
    seed(client)
    assert client.get("/api/seals/B-001/original",
                      headers={"x-role": "auditor", "x-actor": "a"}).status_code == 422
    assert client.get("/api/seals/B-001").json()["seal"].get("original") is None
    assert client.get("/api/seals").json()["seals"][0].get("original") is None


def test_audit_write_noop_is_detected_and_refused(client, monkeypatch):
    seed(client)

    def silent(*args, **kwargs):
        return {"record_id": 0, "fingerprint": "x", "content_digest": "y",
                "status": "PENDING"}, True

    monkeypatch.setattr(client.app.state.store, "record_audit", silent)
    response = client.post("/api/seals/B-001/original",
                           headers={"x-actor": "auditor-1", "x-role": "auditor"},
                           json={"reason": "静默失败"})
    assert response.status_code in (409, 503)
    assert "projects" not in response.text


def test_concurrent_identical_audit_requests_leave_one_trace(client):
    """并发重复查阅同一请求：唯一指纹保证只留一条痕。"""
    import threading

    seed(client)
    headers = {"x-actor": "auditor-p", "x-role": "auditor", "x-request-id": "req-par"}
    statuses = []
    lock = threading.Lock()
    barrier = threading.Barrier(4)

    def worker():
        barrier.wait()
        response = client.post("/api/seals/B-001/original", headers=headers,
                               json={"reason": "并发重复"})
        with lock:
            statuses.append(response.status_code)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert statuses == [200, 200, 200, 200]
    assert client.app.state.store.count_audit() == 1
    record = client.app.state.store.list_audit()[0]
    assert record["status"] == "RECORDED"


def test_seal_list_hides_original(client):
    seed(client)
    seals = client.get("/api/seals").json()["seals"]
    assert seals and "original" not in seals[0]
    assert "decisions" not in json.dumps(seals, ensure_ascii=False)


def test_role_via_header_variants(client):
    seed(client)
    # x-is-auditor 标记等价于审计角色。
    flagged = client.post("/api/seals/B-001/original",
                          headers={"x-actor": "a1", "x-is-auditor": "true"},
                          json={"reason": "标记审计"})
    assert flagged.status_code == 200
    # Bearer 口令里只有已知角色才被采信；随机口令按普通角色拒绝。
    bearer = client.post("/api/seals/B-001/original",
                         headers={"x-actor": "a2", "authorization": "Bearer auditor"},
                         json={"reason": "口令审计"})
    assert bearer.status_code == 200
    junk = client.post("/api/seals/B-001/original",
                       headers={"x-actor": "a3", "authorization": "Bearer opaque-token"},
                       json={"reason": "口令未知"})
    assert junk.status_code == 403
