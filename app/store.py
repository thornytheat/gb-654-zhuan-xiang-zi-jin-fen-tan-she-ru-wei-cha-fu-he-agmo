from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading

from .errors import DomainError

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    digest TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    result TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seals (
    batch_id TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    payload TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    project_count INTEGER NOT NULL,
    decision_count INTEGER NOT NULL,
    eligible_count INTEGER NOT NULL,
    rejected_count INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS disclosures (
    disclosure_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    digest TEXT NOT NULL,
    items TEXT NOT NULL,
    total TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    retracted_at TEXT,
    retract_reason TEXT,
    retract_actor TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_disclosures_batch_seq
    ON disclosures(batch_id, seq);
CREATE INDEX IF NOT EXISTS idx_disclosures_batch
    ON disclosures(batch_id);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    batch_id TEXT NOT NULL,
    disclosure_id TEXT,
    actor TEXT NOT NULL,
    role TEXT NOT NULL,
    reason TEXT NOT NULL,
    action TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_batch ON audit_log(batch_id);
"""


def _now() -> str:
    """带偏移的时间戳，UTC 参与比较。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str):
        self.path = path
        self._memory: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        if path == ":memory:":
            # 内存库必须复用同一连接，否则每次 connect 都是空库。
            self._memory = sqlite3.connect(":memory:", timeout=15.0, check_same_thread=False)
            self._memory.row_factory = sqlite3.Row
            self._memory.executescript(SCHEMA)
        else:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with self._session() as db:
                db.executescript(SCHEMA)
            try:
                with self._session() as db:
                    db.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error:
                # 只读介质无法启用 WAL，不影响正确性。
                pass

    # ------------------------------------------------------------ plumbing
    def connect(self) -> sqlite3.Connection:
        if self._memory is not None:
            return self._memory
        db = sqlite3.connect(self.path, timeout=15.0)
        db.execute("PRAGMA busy_timeout=15000")
        return db

    @contextmanager
    def _session(self):
        with self._lock:
            db = self.connect()
            db.row_factory = sqlite3.Row
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                if self._memory is None:
                    db.close()

    @contextmanager
    def transaction(self, immediate: bool = True):
        """显式事务：写入用 BEGIN IMMEDIATE，撤回/公示/查阅互斥且不丢数据。"""
        with self._lock:
            db = self.connect()
            db.row_factory = sqlite3.Row
            try:
                db.execute("PRAGMA busy_timeout=15000")
                db.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                try:
                    yield db
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
                else:
                    db.execute("COMMIT")
            finally:
                if self._memory is None:
                    db.close()

    # ---------------------------------------------------------------- runs
    def record(self, digest: str, run_id: str, result: dict) -> dict:
        with self._session() as db:
            db.execute("INSERT OR IGNORE INTO runs VALUES (?, ?, ?)",
                       (digest, run_id, json.dumps(result, ensure_ascii=False, sort_keys=True)))
            row = db.execute("SELECT run_id, result FROM runs WHERE digest=?", (digest,)).fetchone()
        return {"run_id": row[0], "result": json.loads(row[1])}

    def count(self) -> int:
        with self._session() as db:
            return db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

    def all(self) -> list[dict]:
        with self._session() as db:
            rows = db.execute("SELECT run_id, digest, result FROM runs ORDER BY run_id").fetchall()
        return [{"run_id": r[0], "digest": r[1], "result": json.loads(r[2])} for r in rows]

    # --------------------------------------------------------------- seals
    @staticmethod
    def _seal_row(row: sqlite3.Row) -> dict:
        return {
            "batch_id": row["batch_id"],
            "digest": row["digest"],
            "payload": json.loads(row["payload"]),
            "rule_version": row["rule_version"],
            "project_count": row["project_count"],
            "decision_count": row["decision_count"],
            "eligible_count": row["eligible_count"],
            "rejected_count": row["rejected_count"],
            "status": row["status"],
            "created_at": row["created_at"],
        }

    def save_seal(self, seal: dict) -> tuple[dict, bool]:
        """封存件不可变：同批次同摘要复用，不同摘要拒绝覆盖。"""
        columns = ("batch_id", "digest", "payload", "rule_version", "project_count",
                   "decision_count", "eligible_count", "rejected_count", "status", "created_at")
        values = (
            seal["batch_id"], seal["digest"],
            json.dumps(seal["payload"], ensure_ascii=False, sort_keys=True),
            seal["rule_version"], seal["project_count"], seal["decision_count"],
            seal["eligible_count"], seal["rejected_count"], seal["status"],
            seal.get("created_at") or _now(),
        )
        with self.transaction() as db:
            row = db.execute("SELECT * FROM seals WHERE batch_id=?", (seal["batch_id"],)).fetchone()
            if row is not None:
                if row["digest"] != seal["digest"]:
                    raise DomainError("SEAL_CONFLICT", "该批次已封存且原件摘要不同，拒绝覆盖", 409)
                return self._seal_row(row), False
            db.execute(
                f"INSERT INTO seals ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                values,
            )
            row = db.execute("SELECT * FROM seals WHERE batch_id=?", (seal["batch_id"],)).fetchone()
            return self._seal_row(row), True

    def get_seal(self, batch_id: str) -> dict | None:
        with self._session() as db:
            row = db.execute("SELECT * FROM seals WHERE batch_id=?", (str(batch_id),)).fetchone()
        return self._seal_row(row) if row is not None else None

    def list_seals(self) -> list[dict]:
        with self._session() as db:
            rows = db.execute("SELECT * FROM seals ORDER BY batch_id").fetchall()
        return [self._seal_row(row) for row in rows]

    # --------------------------------------------------------- disclosures
    @staticmethod
    def _disclosure_row(row: sqlite3.Row) -> dict:
        return {
            "disclosure_id": row["disclosure_id"],
            "batch_id": row["batch_id"],
            "seq": row["seq"],
            "digest": row["digest"],
            "items": json.loads(row["items"]),
            "total": row["total"],
            "status": row["status"],
            "created_at": row["created_at"],
            "retracted_at": row["retracted_at"],
            "retract_reason": row["retract_reason"],
            "retract_actor": row["retract_actor"],
        }

    def create_disclosure(self, batch_id: str, items: list[dict], digest: str,
                          total: str) -> tuple[dict, bool]:
        """同一批次同一内容在公示期内重复提交复用；撤回后重新公示生成新入口。"""
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM disclosures WHERE batch_id=? AND digest=? AND status='PUBLISHED' "
                "ORDER BY seq LIMIT 1",
                (batch_id, digest),
            ).fetchone()
            if row is not None:
                return self._disclosure_row(row), False
            row = db.execute("SELECT MAX(seq) AS seq FROM disclosures WHERE batch_id=?",
                             (batch_id,)).fetchone()
            seq = (row["seq"] or 0) + 1
            disclosure_id = "dsc-" + hashlib.sha256(
                f"{batch_id}|{digest}|{seq}".encode("utf-8")
            ).hexdigest()[:12]
            db.execute(
                "INSERT INTO disclosures (disclosure_id, batch_id, seq, digest, items, total, "
                "status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'PUBLISHED', ?)",
                (disclosure_id, batch_id, seq, digest,
                 json.dumps(items, ensure_ascii=False, sort_keys=True), total, _now()),
            )
            row = db.execute("SELECT * FROM disclosures WHERE disclosure_id=?",
                             (disclosure_id,)).fetchone()
            return self._disclosure_row(row), True

    def get_disclosure(self, disclosure_id: str) -> dict | None:
        with self._session() as db:
            row = db.execute("SELECT * FROM disclosures WHERE disclosure_id=?",
                             (str(disclosure_id),)).fetchone()
        return self._disclosure_row(row) if row is not None else None

    def list_disclosures(self, batch_id: str | None = None) -> list[dict]:
        with self._session() as db:
            if batch_id is None:
                rows = db.execute("SELECT * FROM disclosures ORDER BY created_at, disclosure_id").fetchall()
            else:
                rows = db.execute("SELECT * FROM disclosures WHERE batch_id=? "
                                  "ORDER BY seq", (str(batch_id),)).fetchall()
        return [self._disclosure_row(row) for row in rows]

    def retract_disclosure(self, disclosure_id: str, actor: str, reason: str) -> tuple[dict, bool]:
        with self.transaction() as db:
            row = db.execute("SELECT * FROM disclosures WHERE disclosure_id=?",
                             (str(disclosure_id),)).fetchone()
            if row is None:
                raise DomainError("DISCLOSURE_NOT_FOUND", "公示不存在", 404)
            if row["status"] == "RETRACTED":
                return self._disclosure_row(row), False
            db.execute(
                "UPDATE disclosures SET status='RETRACTED', retracted_at=?, retract_reason=?, "
                "retract_actor=? WHERE disclosure_id=? AND status='PUBLISHED'",
                (_now(), reason, actor, disclosure_id),
            )
            row = db.execute("SELECT * FROM disclosures WHERE disclosure_id=?",
                             (disclosure_id,)).fetchone()
            return self._disclosure_row(row), True

    # --------------------------------------------------------------- audit
    @staticmethod
    def _audit_row(row: sqlite3.Row) -> dict:
        return {
            "record_id": row["id"],
            "fingerprint": row["fingerprint"],
            "batch_id": row["batch_id"],
            "disclosure_id": row["disclosure_id"],
            "actor": row["actor"],
            "role": row["role"],
            "reason": row["reason"],
            "action": row["action"],
            "content_digest": row["content_digest"],
            "status": row["status"],
            "created_at": row["created_at"],
            "completed_at": row["completed_at"],
        }

    def record_audit(self, fingerprint: str, batch_id: str, disclosure_id: str | None,
                     actor: str, role: str, reason: str, action: str,
                     content_digest: str) -> tuple[dict, bool]:
        """写入（或复用）一条审计留痕。

        采用两阶段留痕：先落 PENDING，再标记 RECORDED。进程在两步之间中断
        时，重试同一指纹会复用该行并补完，不会产生重复记录。写入失败会抛出
        异常，由调用方拒绝返回原文。
        """
        with self.transaction() as db:
            row = db.execute("SELECT * FROM audit_log WHERE fingerprint=?",
                             (fingerprint,)).fetchone()
            if row is not None:
                return self._audit_row(row), False
            try:
                db.execute(
                    "INSERT INTO audit_log (fingerprint, batch_id, disclosure_id, actor, role, reason, "
                    "action, content_digest, status, created_at, completed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, NULL)",
                    (fingerprint, batch_id, disclosure_id, actor, role, reason, action,
                     content_digest, _now()),
                )
            except sqlite3.IntegrityError:
                # 其他进程抢先写入了同一指纹：复用已有记录，不重复留痕。
                row = db.execute("SELECT * FROM audit_log WHERE fingerprint=?",
                                 (fingerprint,)).fetchone()
                if row is None:
                    raise
                return self._audit_row(row), False
            row = db.execute("SELECT * FROM audit_log WHERE fingerprint=?",
                             (fingerprint,)).fetchone()
            return self._audit_row(row), True

    def complete_audit(self, fingerprint: str) -> dict:
        """把 PENDING 留痕补完为 RECORDED；重复调用幂等。"""
        with self.transaction() as db:
            row = db.execute("SELECT * FROM audit_log WHERE fingerprint=?",
                             (fingerprint,)).fetchone()
            if row is None:
                raise DomainError("AUDIT_NOT_FOUND", "审计留痕不存在，拒绝返回原件", 503)
            if row["status"] != "RECORDED":
                db.execute("UPDATE audit_log SET status='RECORDED', completed_at=? "
                           "WHERE fingerprint=? AND status='PENDING'",
                           (_now(), fingerprint))
                row = db.execute("SELECT * FROM audit_log WHERE fingerprint=?",
                                 (fingerprint,)).fetchone()
            return self._audit_row(row)

    def list_audit(self, batch_id: str | None = None) -> list[dict]:
        with self._session() as db:
            if batch_id is None:
                rows = db.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
            else:
                rows = db.execute("SELECT * FROM audit_log WHERE batch_id=? ORDER BY id",
                                  (str(batch_id),)).fetchall()
        return [self._audit_row(row) for row in rows]

    def count_audit(self, batch_id: str | None = None) -> int:
        with self._session() as db:
            if batch_id is None:
                return db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
            return db.execute("SELECT COUNT(*) FROM audit_log WHERE batch_id=?",
                              (str(batch_id),)).fetchone()[0]
