"""客户历史时间线领域规则测试。"""

import pytest

from app.business import DomainIssue
from app.history.parser import HEADER, parse_events
from app.history.timeline import OPEN_END, build


def change(record, customer, start, segment, line=1):
    return {
        "record": record,
        "customer": customer,
        "kind": "change",
        "start_day": start,
        "end_day": None,
        "segment": str(segment),
        "target": "",
        "note": "",
        "_line": line,
    }


def revoke(record, customer, target, start=1, line=1):
    return {
        "record": record,
        "customer": customer,
        "kind": "revoke",
        "start_day": start,
        "end_day": None,
        "segment": None,
        "target": target,
        "note": "",
        "_line": line,
    }


def settle(record, customer, start, end, line=1):
    return {
        "record": record,
        "customer": customer,
        "kind": "settle",
        "start_day": start,
        "end_day": end,
        "segment": None,
        "target": "",
        "note": "",
        "_line": line,
    }


def segments(result, customer):
    return [(s["start"], s["end"], s["segment"]) for s in result["timeline"][customer]]


def test_late_change_only_reaches_next_confirmed_change():
    # 先确认 0->1、10->2；晚到的 5->3 只能落在 [5,10)
    events = [
        change("E1", "C1", 0, 1),
        change("E2", "C1", 10, 2),
        change("E3", "C1", 5, 3),
    ]
    result = build(events)
    assert segments(result, "C1") == [(0, 5, "1"), (5, 10, "3"), (10, OPEN_END, "2")]
    # 首版无基线时不列漂移；提供基线后仅 [5,10) 报漂移，后续确认不被冲掉
    baseline = {"C1": [
        {"start": 0, "end": 10, "segment": "1", "records": ["E1"]},
        {"start": 10, "end": OPEN_END, "segment": "2", "records": ["E2"]},
    ]}
    with_base = build(events, baseline=baseline)
    shift = [i for i in with_base["impacts"] if i["reason"] == "OWNERSHIP_SHIFT"]
    assert [(i["start"], i["end"], i["before"], i["after"]) for i in shift] == [
        (5, 10, "1", "3")
    ]


def test_late_change_does_not_override_later_confirmation_when_reordered():
    # 无论导入顺序如何，结果只由生效点决定
    events = [
        change("E3", "C1", 5, 3),
        change("E2", "C1", 10, 2),
        change("E1", "C1", 0, 1),
    ]
    result = build(events)
    assert segments(result, "C1") == [(0, 5, "1"), (5, 10, "3"), (10, OPEN_END, "2")]


def test_settled_period_keeps_original_ownership():
    events = [
        change("E1", "C1", 0, 5),
        change("E2", "C1", 10, 6),
        change("E3", "C1", 3, 7),  # 晚到，冲击已结算 [0,10)
    ]
    locks = [("C1", 0, 10, "5")]
    result = build(events, locks=locks, baseline={"C1": [
        {"start": 0, "end": 10, "segment": "5", "records": ["E1"]},
        {"start": 10, "end": OPEN_END, "segment": "6", "records": ["E2"]},
    ]})
    conflict = result["conflicts"]
    assert len(conflict) == 1 and conflict[0]["reason"] == "SETTLED_LOCKED"
    assert (conflict[0]["start"], conflict[0]["end"]) == (3, 10)
    assert (conflict[0]["before"], conflict[0]["after"]) == ("5", "7")


def test_settled_lock_does_not_block_uncovered_future_period():
    events = [change("E1", "C1", 0, 5), change("E2", "C1", 20, 9)]
    result = build(events, locks=[("C1", 0, 10, "5")])
    assert result["conflicts"] == []
    assert segments(result, "C1") == [(0, 20, "5"), (20, OPEN_END, "9")]


def test_revoke_only_rolls_back_target_change():
    events = [
        change("E1", "C2", 0, 1),
        change("E2", "C2", 8, 4),
        change("E3", "C2", 20, 2),
        revoke("R1", "C2", "E2"),
    ]
    result = build(events)
    # E2 被撤销：[8,20) 回到 E1，E3 分段保留
    assert segments(result, "C2") == [(0, 20, "1"), (20, OPEN_END, "2")]
    revocation = next(i for i in result["impacts"] if i["reason"] == "REVOCATION")
    assert revocation["records"] == ["R1", "E2"]
    assert revocation["start"] == 8


def test_revoke_missing_target_is_conflict_not_silent_delete():
    events = [change("E1", "C2", 0, 1), revoke("R1", "C2", "GHOST")]
    result = build(events)
    assert [c["reason"] for c in result["conflicts"]] == ["REVOKE_TARGET_MISSING"]
    assert segments(result, "C2") == [(0, OPEN_END, "1")]


def test_revoke_twice_second_is_conflict():
    events = [change("E1", "C2", 0, 1), revoke("R1", "C2", "E1"),
              revoke("R2", "C2", "E1")]
    result = build(events)
    reasons = [c["reason"] for c in result["conflicts"]]
    assert reasons == ["REVOKE_TARGET_MISSING"]
    assert result["conflicts"][0]["records"] == ["R2"]


def test_same_effective_day_different_ownership_is_conflict():
    events = [change("E1", "C4", 5, 1), change("E2", "C4", 5, 2)]
    result = build(events)
    assert result["conflicts"][0]["reason"] == "SAME_EFFECTIVE_CONFLICT"
    assert result["conflicts"][0]["records"] == ["E1", "E2"]


def test_excluded_event_drops_out_of_timeline():
    events = [
        change("E1", "C1", 0, 5),
        change("E3", "C1", 3, 7),
    ]
    result = build(events, locks=[("C1", 0, 10, "5")], excluded=frozenset({"E3"}))
    assert result["conflicts"] == []
    assert segments(result, "C1") == [(0, OPEN_END, "5")]


def test_settle_event_without_coverage_is_conflict():
    events = [settle("S1", "C9", 0, 10), change("E1", "C9", 10, 5)]
    result = build(events)
    assert result["conflicts"][0]["reason"] == "SETTLE_UNCOVERED"
    assert (result["conflicts"][0]["start"], result["conflicts"][0]["end"]) == (0, 10)


def test_parser_isolates_bad_shapes(tmp_path):
    import io, csv
    stream = io.StringIO(newline="")
    csv.writer(stream).writerows([
        HEADER,
        ("E1", "C1", "change", "0", "", "", "", "缺归属"),
        ("E2", "C1", "revoke", "1", "", "", "", "缺目标"),
        ("E3", "C1", "settle", "0", "0", "", "", "空账期"),
        ("E4", "C1", "unknown", "0", "", "1", "", "坏类型"),
    ])
    accepted, isolation, summary = parse_events(stream.getvalue().encode())
    assert accepted == []
    assert sorted(item["reason"] for item in isolation) == [
        "HISTORY_CHANGE_SHAPE",
        "HISTORY_REVOKE_SHAPE",
        "HISTORY_SETTLE_SHAPE",
        "HISTORY_UNKNOWN_KIND",
    ]


def test_parser_header_mismatch():
    with pytest.raises(DomainIssue, match="HISTORY_HEADER_MISMATCH"):
        parse_events(b"wrong,header\n1,2\n")


def test_parser_replay_is_isolated_as_duplicate_replay():
    import io, csv
    stream = io.StringIO(newline="")
    csv.writer(stream).writerows([
        HEADER,
        ("E1", "C1", "change", "0", "", "1", "", "同一事件"),
        ("E1", "C1", "change", "0", "", "1", "", "同一事件"),
    ])
    accepted, isolation, summary = parse_events(stream.getvalue().encode())
    assert [e["record"] for e in accepted] == ["E1"]
    assert isolation[0]["reason"] == "DUPLICATE_REPLAY"
