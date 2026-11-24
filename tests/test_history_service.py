"""客户历史批次服务与持久化测试。"""

import io
from pathlib import Path

import pytest

from app.business import DomainIssue
from app.history.parser import HEADER
from app.history.service import evidence, preview, publish, resolve_conflict, submit
from app.history.store import HistoryStore

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def store(tmp_path):
    return HistoryStore(tmp_path / "history.db")


@pytest.fixture
def incoming():
    return (ROOT / "sample_data" / "history_incoming.csv").read_bytes()


@pytest.fixture
def ready():
    return (ROOT / "sample_data" / "history_ready.csv").read_bytes()


def _csv(rows):
    import csv
    stream = io.StringIO(newline="")
    csv.writer(stream).writerows([HEADER, *rows])
    return stream.getvalue().encode()


def test_preview_lists_impacts_before_after_and_reasons(store, incoming):
    report = preview(store, incoming)
    assert report["publishable"] is False
    reasons = {c["reason"] for c in report["conflicts"]}
    # C003 结算账期被晚到变更切成两个归属；C002 撤销目标在样例中有效
    assert "SETTLE_SPLIT_OWNERSHIP" in reasons
    c1 = report["timeline"]["C001"]
    assert [(s["start"], s["end"], s["segment"]) for s in c1] == [
        (0, 5, "1"), (5, 10, "3"), (10, None, "2"),
    ]
    revocation = next(i for i in report["impacts"] if i["reason"] == "REVOCATION")
    assert revocation["records"] == ["C002-R1", "C002-E2"]


def test_submit_then_same_batch_replay_adds_no_records(store, ready):
    first = submit(store, ready)
    second = submit(store, ready)
    assert first["id"] == second["id"]
    assert first["inserted"] == 6 and second["inserted"] == 0
    assert second["manifest"]["replayed"] is True
    assert len(store.list_batches()) == 1


def test_publish_creates_version_and_locks(store, ready):
    batch = submit(store, ready)
    assert batch["publishable"] is True
    output = publish(store, batch["id"])
    assert output["version"]["version"] == 1
    locks = store.locks()
    assert locks == [("C003", 0, 10, "5")]
    assert store.load_batch(batch["id"])["status"] == "PUBLISHED"


def test_unresolved_conflict_blocks_official_version(store, incoming):
    batch = submit(store, incoming)
    with pytest.raises(DomainIssue, match="HISTORY_UNRESOLVED"):
        publish(store, batch["id"])
    assert store.current_version() is None  # 没有形成正式版本


def test_resolution_excludes_event_and_allows_publish(store):
    data = _csv([
        ("E1", "C1", "change", "0", "", "5", "", "首次"),
        ("S1", "C1", "settle", "0", "10", "", "", "已结算"),
    ])
    first = submit(store, data)
    publish(store, first["id"])
    second = submit(store, _csv([
        ("L1", "C1", "change", "2", "", "9", "", "冲击已结算账期"),
    ]))
    with pytest.raises(DomainIssue, match="HISTORY_UNRESOLVED"):
        publish(store, second["id"])
    updated = resolve_conflict(store, second["id"], {
        "record": "L1", "reason": "SOURCE_CORRECTED", "note": "来源更正，剔除该晚到事件",
    })
    assert updated["open_conflicts"] == [] and updated["publishable"] is True
    publish(store, second["id"])
    # 已结算账期归属保持原值
    assert store.current_version()["snapshot"]["timeline"]["C1"][0]["segment"] == "5"


def test_revoke_isolates_target_segment_only(store, ready, incoming):
    base = submit(store, ready)
    publish(store, base["id"])
    batch = submit(store, incoming)
    # C002 撤销 E2：E1 分段保留，且不产生 C001/C003 之外的误删
    c2 = next(impact for impact in batch["impacts"] if impact["reason"] == "REVOCATION")
    assert c2["records"] == ["C002-R1", "C002-E2"]
    customers = {c for c in batch["timeline"]}
    assert customers == {"C001", "C002", "C003"}


def test_publish_failure_keeps_old_version_and_evidence(store, ready, incoming):
    first = submit(store, ready)
    publish(store, first["id"])
    old = store.current_version()

    second = submit(store, _csv([
        ("N1", "C9", "change", "0", "", "1", "", "新客户"),
    ]))

    def boom(db):
        raise RuntimeError("disk full")

    with pytest.raises(DomainIssue, match="HISTORY_PUBLISH_FAILED"):
        publish(store, second["id"], failure_simulator=boom)
    retained = store.current_version()
    assert retained["version"] == old["version"]
    assert retained["fingerprint"] == old["fingerprint"]
    assert store.load_batch(second["id"])["status"] == "DRAFT"
    # 依据仍可导出
    packet = evidence(store, store.load_batch(second["id"]))
    assert packet["schema"] == "history-evidence/1"
    assert packet["published_version"] == 1


def test_published_batch_cannot_be_modified(store, ready):
    batch = submit(store, ready)
    publish(store, batch["id"])
    with pytest.raises(DomainIssue, match="HISTORY_BATCH_FINALIZED"):
        resolve_conflict(store, batch["id"], {
            "record": "C001-E1", "reason": "CONFIRMED_CONFLICT", "note": "试图改动已发布批次",
        })


def test_resolution_validation(store, ready):
    batch = submit(store, ready)
    with pytest.raises(DomainIssue, match="HISTORY_RECORD_UNKNOWN"):
        resolve_conflict(store, batch["id"], {
            "record": "NOPE", "reason": "CONFIRMED_CONFLICT", "note": "不存在的事件",
        })
    with pytest.raises(DomainIssue, match="HISTORY_RESOLUTION_REASON"):
        resolve_conflict(store, batch["id"], {
            "record": "C001-E1", "reason": "BAD", "note": "原因不合法",
        })
    with pytest.raises(DomainIssue, match="HISTORY_RESOLUTION_NOTE"):
        resolve_conflict(store, batch["id"], {
            "record": "C001-E1", "reason": "CONFIRMED_CONFLICT", "note": "短",
        })


def test_cross_batch_revoke_targets_ledger_change(store):
    first = submit(store, _csv([
        ("E1", "C1", "change", "0", "", "1", "", "首次"),
        ("E2", "C1", "change", "10", "", "2", "", "转区"),
    ]))
    publish(store, first["id"])
    second = submit(store, _csv([
        ("R1", "C1", "revoke", "12", "", "", "E2", "撤销历史变更"),
    ]))
    assert second["open_conflicts"] == []
    revocation = next(i for i in second["impacts"] if i["reason"] == "REVOCATION")
    assert revocation["records"] == ["R1", "E2"]
    assert (revocation["before"], revocation["after"]) == ("2", "1")
    publish(store, second["id"])
    c1 = store.current_version()["snapshot"]["timeline"]["C1"]
    assert [(s["start"], s["end"], s["segment"]) for s in c1] == [(0, None, "1")]


def test_second_publish_accumulates_locks_and_freezes_history(store):
    first = submit(store, _csv([
        ("E1", "C1", "change", "0", "", "5", "", "首次"),
        ("S1", "C1", "settle", "0", "10", "", "", "首期结算"),
    ]))
    publish(store, first["id"])
    second = submit(store, _csv([
        ("E2", "C1", "change", "10", "", "6", "", "新账期归属"),
        ("E3", "C1", "change", "3", "", "7", "", "晚到，冲击冻结账期"),
    ]))
    with pytest.raises(DomainIssue, match="HISTORY_UNRESOLVED"):
        publish(store, second["id"])
    resolve_conflict(store, second["id"], {
        "record": "E3", "reason": "CONFIRMED_CONFLICT", "note": "确认冲突，剔除晚到事件",
    })
    publish(store, second["id"])
    version = store.current_version()
    assert version["version"] == 2
    assert store.locks() == [("C1", 0, 10, "5")]  # 旧锁保留
    c1 = version["snapshot"]["timeline"]["C1"]
    assert [(s["start"], s["end"], s["segment"]) for s in c1] == [
        (0, 10, "5"), (10, None, "6"),
    ]
