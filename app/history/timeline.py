"""客户归属时间线的纯领域逻辑。

输入为事件字典（parser 产出）与已发布版本冻结的账期锁，输出每个客户的
归属分段、受影响区间（前后归属）与阻断签发的冲突。这里不接触数据库。
"""

from collections import defaultdict

from app.business import intervals

OPEN_END = None  # 末条分段开放，右闭点未知


def _active(events, excluded):
    changes, revokes, settles = [], [], []
    for event in events:
        if event["record"] in excluded:
            continue
        if event["kind"] == "change":
            changes.append(event)
        elif event["kind"] == "revoke":
            revokes.append(event)
        else:
            settles.append(event)
    return changes, revokes, settles


def _segment_at(segments, day):
    for segment in segments:
        if segment["start"] <= day and (
            segment["end"] is OPEN_END or day < segment["end"]
        ):
            return segment["segment"]
    return None


def _slice_overlap(start, end, segments):
    """返回 [(day_start, day_end, 归属)]，end=None 表示开放。"""
    hits = []
    for segment in segments:
        left = max(start, segment["start"])
        right = segment["end"] if segment["end"] is not None else end
        right = min(end, right) if end is not None else right
        if right is not None and right <= left:
            continue
        if segment["end"] is OPEN_END and end is OPEN_END:
            hits.append((left, OPEN_END, segment["segment"]))
        elif right is not None and right > left:
            hits.append((left, right, segment["segment"]))
    return hits


def build(events, locks=(), baseline=None, excluded=frozenset()):
    """构建候选时间线。

    events   本批或账本中的全部事件
    locks    已发布版本冻结的账期 [(customer, start, end, segment)]
    baseline 已发布时间线 {customer: [segment...]}，用于计算前后归属
    excluded 经冲突处理排除的事件 record 集合
    """
    baseline = baseline or {}
    # 账本可能含跨批次重放的同一 record，先按 record 去重（后者覆盖前者）。
    deduped = {}
    for event in events:
        deduped[event["record"]] = event
    events = list(deduped.values())
    changes, revokes, settles = _active(events, excluded)
    conflicts, impacts = [], []

    changes_by_record = {event["record"]: event for event in changes}
    revoked = set()
    for revoke in sorted(revokes, key=lambda event: event["record"]):
        target = changes_by_record.get(revoke["target"])
        if target is None or target["record"] in revoked:
            conflicts.append(
                {
                    "reason": "REVOKE_TARGET_MISSING",
                    "customer": revoke["customer"],
                    "records": [revoke["record"]],
                    "target": revoke["target"],
                    "start": revoke["start_day"],
                    "end": OPEN_END,
                    "before": None,
                    "after": None,
                }
            )
            continue
        revoked.add(target["record"])
        impacts.append(
            {
                "reason": "REVOCATION",
                "customer": target["customer"],
                "records": [revoke["record"], target["record"]],
                "start": target["start_day"],
                "end": OPEN_END,
                "before": None,
                "after": None,
            }
        )

    grouped = defaultdict(list)
    for event in changes:
        if event["record"] not in revoked:
            grouped[event["customer"]].append(event)

    timeline = {}
    for customer, rows in grouped.items():
        ordered = sorted(rows, key=lambda event: (event["start_day"], event["record"]))
        winners = []
        day_groups = defaultdict(list)
        for event in ordered:
            day_groups[event["start_day"]].append(event)
        for start_day, same_day in sorted(day_groups.items()):
            segments = {event["segment"] for event in same_day}
            if len(segments) > 1:
                conflicts.append(
                    {
                        "reason": "SAME_EFFECTIVE_CONFLICT",
                        "customer": customer,
                        "records": [event["record"] for event in same_day],
                        "start": start_day,
                        "end": OPEN_END,
                        "before": None,
                        "after": sorted(segments),
                    }
                )
            winners.append(same_day[0])  # record 序最小者，保证确定性
        segments = []
        for index, event in enumerate(winners):
            end = (
                winners[index + 1]["start_day"]
                if index + 1 < len(winners)
                else OPEN_END
            )
            segments.append(
                {
                    "start": event["start_day"],
                    "end": end,
                    "segment": event["segment"],
                    "records": sorted({
                        item["record"]
                        for item in day_groups[event["start_day"]]
                        if item["segment"] == event["segment"]
                    }),
                }
            )
        timeline[customer] = segments

    # 撤销影响区间：以基线对照，回填前后归属，且只回退目标变更涉及的分段。
    for impact in impacts:
        if impact["reason"] != "REVOCATION":
            continue
        target = next(
            event
            for event in changes
            if event["record"] == impact["records"][1]
        )
        base_segments = baseline.get(impact["customer"], [])
        candidate = timeline.get(impact["customer"], [])
        impact["start"] = target["start_day"]
        impact["before"] = _segment_at(base_segments, target["start_day"])
        impact["after"] = _segment_at(candidate, target["start_day"])

    # 与已发布基线对照，列出归属漂移（晚到变更的自然落点）。
    # 首版无基线时，全部归属都是新增而非漂移，不列影响区间。
    for customer, segments in timeline.items():
        base_segments = baseline.get(customer)
        if not base_segments:
            continue
        for segment in segments:
            for start, end, base in _all_spans(segment, base_segments):
                if base != segment["segment"]:
                    impacts.append(
                        {
                            "reason": "OWNERSHIP_SHIFT",
                            "customer": customer,
                            "records": list(segment["records"]),
                            "start": start,
                            "end": end,
                            "before": base,
                            "after": segment["segment"],
                        }
                    )

    # 已结算账期冻结：候选归属与锁不一致即阻断，未覆盖的账期同样阻断。
    locks_by_customer = defaultdict(list)
    for customer, start, end, segment in locks:
        locks_by_customer[customer].append((start, end, segment))
    for customer, entries in locks_by_customer.items():
        candidate = timeline.get(customer, [])
        for lock_start, lock_end, locked in sorted(entries):
            covered = _slice_overlap(lock_start, lock_end, candidate)
            for start, end, segment in covered:
                if segment != locked:
                    conflicts.append(
                        {
                            "reason": "SETTLED_LOCKED",
                            "customer": customer,
                            "records": [
                                record
                                for piece in candidate
                                if piece["start"] <= start
                                and (piece["end"] is OPEN_END or start < piece["end"])
                                for record in piece["records"]
                            ],
                            "start": start,
                            "end": end,
                            "before": locked,
                            "after": segment,
                        }
                    )
            covered_spans = intervals(
                [(start, end) for start, end, _ in covered if end is not None]
            )
            for gap_start, gap_end in _gaps(lock_start, lock_end, covered_spans):
                conflicts.append(
                    {
                        "reason": "SETTLED_LOCKED",
                        "customer": customer,
                        "records": [],
                        "start": gap_start,
                        "end": gap_end,
                        "before": locked,
                        "after": None,
                    }
                )

    # 本批申报的结算账期必须被候选时间线完整、且单一归属覆盖，才能落锁。
    for settle in settles:
        candidate = timeline.get(settle["customer"], [])
        covered = _slice_overlap(
            settle["start_day"], settle["end_day"], candidate
        )
        covered_spans = intervals(
            [(start, end) for start, end, _ in covered if end is not None]
        )
        for gap_start, gap_end in _gaps(
            settle["start_day"], settle["end_day"], covered_spans
        ):
            conflicts.append(
                {
                    "reason": "SETTLE_UNCOVERED",
                    "customer": settle["customer"],
                    "records": [settle["record"]],
                    "start": gap_start,
                    "end": gap_end,
                    "before": None,
                    "after": None,
                }
            )
        if len({segment for _, _, segment in covered}) > 1:
            conflicts.append(
                {
                    "reason": "SETTLE_SPLIT_OWNERSHIP",
                    "customer": settle["customer"],
                    "records": [settle["record"]],
                    "start": settle["start_day"],
                    "end": settle["end_day"],
                    "before": sorted({segment for _, _, segment in covered}),
                    "after": None,
                }
            )

    return {
        "timeline": dict(sorted(timeline.items())),
        "impacts": _dedupe(sorted(impacts, key=_sort_key)),
        "conflicts": _dedupe(sorted(conflicts, key=_sort_key)),
    }


def _all_spans(segment, base_segments):
    """把一个候选分段按基线归属切成 (start, end, 基线归属) 序列。"""
    end = segment["end"]
    base_hits = _slice_overlap(segment["start"], end, base_segments)
    if not base_hits:
        return [(segment["start"], end, None)]
    result = []
    cursor = segment["start"]
    for start, base_end, base in base_hits:
        if start > cursor:
            result.append((cursor, start, None))
        result.append((start, base_end, base))
        cursor = base_end
    if end is not None and cursor < end:
        result.append((cursor, end, None))
    if end is OPEN_END and cursor is not None and (
        not base_hits or base_hits[-1][1] is not None
    ):
        result.append((cursor, OPEN_END, None))
    return result


def _gaps(start, end, covered_spans):
    from app.business import subtract

    return subtract([(start, end)], covered_spans)


def _sort_key(item):
    return (
        item["customer"],
        item["start"],
        item["end"] if item["end"] is not None else 1 << 30,
        item["reason"],
        ",".join(item["records"]),
    )


def _dedupe(items):
    seen, result = set(), []
    for item in items:
        key = (
            item["reason"],
            item["customer"],
            item["start"],
            item["end"],
            tuple(item["records"]),
            str(item.get("before")),
            str(item.get("after")),
        )
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def locks_from_settles(events, timeline):
    """发布时把结算账期连同当时归属固化为账期锁。"""
    locks = []
    for event in events:
        if event["kind"] != "settle":
            continue
        for start, end, segment in _slice_overlap(
            event["start_day"], event["end_day"], timeline.get(event["customer"], [])
        ):
            if end is None:
                continue
            locks.append(
                {
                    "customer": event["customer"],
                    "start": start,
                    "end": end,
                    "segment": segment,
                    "record": event["record"],
                }
            )
    return sorted(locks, key=lambda item: (item["customer"], item["start"], item["end"]))
