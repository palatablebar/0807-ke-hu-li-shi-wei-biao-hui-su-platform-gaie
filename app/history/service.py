"""客户历史维护的应用服务：预览、提交、处理冲突、发布。"""

from hashlib import sha256
from time import monotonic

from app.business import DomainIssue
from app.config import RULE_VERSION
from app.history.parser import parse_events
from app.history.store import batch_id_for, now
from app.history.timeline import build, locks_from_settles


def _excluded(batch):
    return {item["record"] for item in batch["resolutions"]}


def _all_excluded(store):
    return {item["record"] for item in store.all_resolutions()}


def preview(store, data):
    """不入库的提交前预览：受影响区间、前后归属、待核原因。"""
    accepted, isolation, summary = parse_events(data)
    locks = store.locks()
    current = store.current_version()
    baseline = current["snapshot"]["timeline"] if current else {}
    result = build(accepted, locks=locks, baseline=baseline)
    return {
        "schema": "history-preview/1",
        "intake": {**summary, "isolation": isolation},
        "timeline": result["timeline"],
        "impacts": result["impacts"],
        "conflicts": result["conflicts"],
        "publishable": not result["conflicts"] and not isolation and bool(accepted),
    }


def submit(store, data):
    """登记一批事件。同字节重放返回原批次且不新增记录。"""
    start = monotonic()
    fingerprint = sha256(data).hexdigest()
    existing_id = store.find_batch(fingerprint)
    accepted, isolation, summary = parse_events(data)
    batch_id = batch_id_for(fingerprint)
    manifest = {
        "sha256": fingerprint,
        "rule_version": RULE_VERSION,
        "created": now(),
        "duration_ms": round((monotonic() - start) * 1000, 3),
        "bytes": len(data),
        "batch_id": batch_id,
        "replayed": existing_id is not None,
        "intake": {**summary, "isolation": isolation},
    }
    batch, inserted = store.register_batch(batch_id, fingerprint, manifest, accepted, isolation)
    # 重放时保留首次登记的清单（含 replayed=False），但本次返回标注为重放。
    if existing_id is not None:
        manifest = {**batch["manifest"], "replayed": True}
    return evaluate(store, batch, inserted=inserted, manifest_override=manifest)


def evaluate(store, batch, inserted=0, manifest_override=None):
    current = store.current_version()
    baseline = current["snapshot"]["timeline"] if current else {}
    excluded = _all_excluded(store)
    # 时间线基于全量账本重建；各批次已处理的事件从构建中剔除。
    ledger = store.all_ledger_events()
    result = build(
        ledger,
        locks=store.locks(),
        baseline=baseline,
        excluded=excluded,
    )
    batch_records = {event["record"] for event in batch["events"]}
    open_conflicts = [
        conflict
        for conflict in result["conflicts"]
        if conflict["records"]
        and all(record in batch_records for record in conflict["records"])
        and not all(record in excluded for record in conflict["records"])
    ]
    manifest = manifest_override or batch["manifest"]
    return {
        "id": batch["id"],
        "status": batch["status"],
        "inserted": inserted,
        "manifest": manifest,
        "resolutions": batch["resolutions"],
        "excluded": sorted(excluded),
        "timeline": result["timeline"],
        "impacts": result["impacts"],
        "conflicts": result["conflicts"],
        "open_conflicts": open_conflicts,
        "publishable": batch["status"] != "PUBLISHED"
        and not open_conflicts
        and not batch["manifest"]["intake"]["isolation"]
        and bool(batch["events"]),
    }


def resolve_conflict(store, batch_id, values):
    if not isinstance(values, dict):
        raise DomainIssue("HISTORY_RESOLUTION_FIELDS")
    record = values.get("record")
    reason_code = values.get("reason")
    note = values.get("note", "")
    if not isinstance(record, str) or not record:
        raise DomainIssue("HISTORY_RESOLUTION_FIELDS")
    batch = store.resolve(batch_id, record, reason_code, note)
    return evaluate(store, batch)


def publish(store, batch_id, failure_simulator=None):
    batch = store.load_batch(batch_id)
    before = store.current_version()
    current = before["snapshot"]["timeline"] if before else {}
    excluded = _all_excluded(store)
    ledger = store.all_ledger_events()
    result = build(
        ledger,
        locks=store.locks(),
        baseline=current,
        excluded=excluded,
    )
    batch_records = {event["record"] for event in batch["events"]}
    open_conflicts = [
        conflict
        for conflict in result["conflicts"]
        if conflict["records"]
        and all(record in batch_records for record in conflict["records"])
        and not all(record in excluded for record in conflict["records"])
    ]
    isolation = batch["manifest"]["intake"]["isolation"]
    if open_conflicts or isolation or not batch["events"]:
        raise DomainIssue("HISTORY_UNRESOLVED")
    # 仅本批次申报的结算账期在本次发布落锁；与既有锁完全相同的不重复登记。
    existing_locks = {
        (customer, start, end, segment)
        for customer, start, end, segment in store.locks()
    }
    new_locks = [
        lock
        for lock in locks_from_settles(batch["events"], result["timeline"])
        if (lock["customer"], lock["start"], lock["end"], lock["segment"])
        not in existing_locks
    ]
    snapshot = {
        "schema": "customer-history/1",
        "batch_id": batch_id,
        "rule_version": RULE_VERSION,
        "created": now(),
        "based_on_version": before["version"] if before else 0,
        "timeline": result["timeline"],
        "impacts": result["impacts"],
        "locks_added": new_locks,
        "resolutions": batch["resolutions"],
        "manifest": batch["manifest"],
    }
    try:
        version = store.publish(batch_id, snapshot, new_locks, failure_simulator)
    except DomainIssue:
        raise
    except Exception as error:  # 发布失败保留旧版与依据
        retained = store.current_version()
        if retained != before:
            raise
        raise DomainIssue("HISTORY_PUBLISH_FAILED") from error
    return {"version": version, "batch": store.load_batch(batch_id)}


def evidence(store, batch):
    current = store.current_version()
    report = evaluate(store, batch)
    return {
        "schema": "history-evidence/1",
        "batch": {
            "id": batch["id"],
            "status": batch["status"],
            "manifest": batch["manifest"],
            "resolutions": batch["resolutions"],
        },
        "published_version": current["version"] if current else None,
        "timeline": report["timeline"],
        "impacts": report["impacts"],
        "conflicts": report["conflicts"],
        "open_conflicts": report["open_conflicts"],
        "locks": [
            dict(customer=c, start=s, end=e, segment=g)
            for c, s, e, g in store.locks()
        ],
    }
