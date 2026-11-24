"""客户历史的 SQLite 持久化。

账本只追加：重放同一批以 (batch, record) 幂等去重，不新增记录；
冲突处理只写 resolutions，不删除事件；发布在单事务内完成，
失败整笔回滚，旧版本与旧账期锁保持不动。
"""

import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from app.business import DomainIssue
from app.config import RULE_VERSION

SCHEMA = """
CREATE TABLE IF NOT EXISTS history_batches (
    id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    manifest TEXT NOT NULL,
    created TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'DRAFT'
);
CREATE TABLE IF NOT EXISTS history_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    record TEXT NOT NULL,
    customer TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    line INTEGER NOT NULL,
    UNIQUE(batch_id, record)
);
CREATE TABLE IF NOT EXISTS history_resolutions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    record TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    note TEXT NOT NULL,
    created TEXT NOT NULL,
    UNIQUE(batch_id, record)
);
CREATE TABLE IF NOT EXISTS history_versions (
    version INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL UNIQUE,
    snapshot TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS history_locks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version INTEGER NOT NULL,
    customer TEXT NOT NULL,
    start_day INTEGER NOT NULL,
    end_day INTEGER NOT NULL,
    segment TEXT NOT NULL,
    record TEXT NOT NULL
);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def batch_id_for(fingerprint):
    return sha256(f"history:{RULE_VERSION}:{fingerprint}".encode()).hexdigest()


class HistoryStore:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=15)) as db, db:
            db.row_factory = sqlite3.Row
            db.executescript(SCHEMA)
            yield db

    # ---- 批次与账本 -------------------------------------------------

    def load_batch(self, batch_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM history_batches WHERE id=?", (batch_id,)
            ).fetchone()
        if row is None:
            raise DomainIssue("HISTORY_BATCH_NOT_FOUND")
        result = dict(row)
        result["manifest"] = json.loads(result["manifest"])
        result["events"] = self.events(batch_id)
        result["resolutions"] = self.resolutions(batch_id)
        return result

    def find_batch(self, fingerprint):
        with self.connect() as db:
            row = db.execute(
                "SELECT id FROM history_batches WHERE fingerprint=?", (fingerprint,)
            ).fetchone()
        return row["id"] if row else None

    def list_batches(self):
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id, fingerprint, created, status FROM history_batches "
                    "ORDER BY created DESC, id LIMIT 50"
                )
            ]

    def events(self, batch_id):
        with self.connect() as db:
            rows = db.execute(
                "SELECT payload, line FROM history_events WHERE batch_id=? "
                "ORDER BY line, id",
                (batch_id,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def all_ledger_events(self):
        with self.connect() as db:
            rows = db.execute(
                "SELECT payload FROM history_events ORDER BY id"
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def resolutions(self, batch_id):
        with self.connect() as db:
            rows = db.execute(
                "SELECT record, reason_code, note, created FROM history_resolutions "
                "WHERE batch_id=? ORDER BY id",
                (batch_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def all_resolutions(self):
        with self.connect() as db:
            rows = db.execute(
                "SELECT record, reason_code, note, created, batch_id "
                "FROM history_resolutions ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]

    def register_batch(self, batch_id, fingerprint, manifest, accepted, isolation):
        """幂等登记。同指纹批次重放返回既有批次，不新增事件记录。"""
        with self.connect() as db:
            db.execute(
                "INSERT INTO history_batches(id, fingerprint, manifest, created) "
                "VALUES (?,?,?,?) ON CONFLICT(id) DO NOTHING",
                (batch_id, fingerprint, json.dumps(manifest, ensure_ascii=False), now()),
            )
            existing = db.execute(
                "SELECT COUNT(*) FROM history_events WHERE batch_id=?", (batch_id,)
            ).fetchone()[0]
            inserted = 0
            if not existing:
                for event in accepted:
                    db.execute(
                        "INSERT INTO history_events"
                        "(batch_id, record, customer, kind, payload, line) "
                        "VALUES (?,?,?,?,?,?)",
                        (
                            batch_id,
                            event["record"],
                            event["customer"],
                            event["kind"],
                            json.dumps(event, ensure_ascii=False),
                            event.get("_line", 0),
                        ),
                    )
                    inserted += 1
        return self.load_batch(batch_id), inserted

    # ---- 冲突处理 ---------------------------------------------------

    def resolve(self, batch_id, record, reason_code, note):
        batch = self.load_batch(batch_id)
        if batch["status"] == "PUBLISHED":
            raise DomainIssue("HISTORY_BATCH_FINALIZED")
        if not any(event["record"] == record for event in batch["events"]):
            raise DomainIssue("HISTORY_RECORD_UNKNOWN")
        if reason_code not in ("CONFIRMED_CONFLICT", "SOURCE_CORRECTED"):
            raise DomainIssue("HISTORY_RESOLUTION_REASON")
        if not isinstance(note, str) or not (3 <= len(note.strip()) <= 200):
            raise DomainIssue("HISTORY_RESOLUTION_NOTE")
        with self.connect() as db:
            db.execute(
                "INSERT INTO history_resolutions(batch_id, record, reason_code, note, created) "
                "VALUES (?,?,?,?,?) ON CONFLICT(batch_id, record) DO UPDATE SET "
                "reason_code=excluded.reason_code, note=excluded.note, created=excluded.created",
                (batch_id, record, reason_code, note.strip(), now()),
            )
        return self.load_batch(batch_id)

    # ---- 版本发布 ---------------------------------------------------

    def current_version(self):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM history_versions ORDER BY version DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["snapshot"] = json.loads(result["snapshot"])
        return result

    def locks(self):
        with self.connect() as db:
            rows = db.execute(
                "SELECT customer, start_day, end_day, segment FROM history_locks "
                "ORDER BY id"
            ).fetchall()
        return [
            (row["customer"], row["start_day"], row["end_day"], row["segment"])
            for row in rows
        ]

    def publish(self, batch_id, snapshot, new_locks, failure_simulator=None):
        """单事务发布。任何异常都回滚，旧版本与依据完整保留。"""
        batch = self.load_batch(batch_id)
        if batch["status"] == "PUBLISHED":
            raise DomainIssue("HISTORY_BATCH_FINALIZED")
        with self.connect() as db:
            try:
                fingerprint = sha256(
                    json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()
                ).hexdigest()
                db.execute(
                    "INSERT INTO history_versions(batch_id, snapshot, fingerprint, created) "
                    "VALUES (?,?,?,?)",
                    (
                        batch_id,
                        json.dumps(snapshot, ensure_ascii=False),
                        fingerprint,
                        now(),
                    ),
                )
                version = db.execute(
                    "SELECT version FROM history_versions WHERE batch_id=?",
                    (batch_id,),
                ).fetchone()["version"]
                for lock in new_locks:
                    db.execute(
                        "INSERT INTO history_locks"
                        "(version, customer, start_day, end_day, segment, record) "
                        "VALUES (?,?,?,?,?,?)",
                        (
                            version,
                            lock["customer"],
                            lock["start"],
                            lock["end"],
                            lock["segment"],
                            lock["record"],
                        ),
                    )
                if failure_simulator is not None:
                    failure_simulator(db)
                db.execute(
                    "UPDATE history_batches SET status='PUBLISHED' WHERE id=?",
                    (batch_id,),
                )
            except Exception:
                db.rollback()
                raise
        return self.current_version()
