"""客户变更事件 CSV 的采信。

复用 intake 的读取、数值归一化与重复记录判定，扩展事件专属字段：
- change   归属变更，start_day 为生效点，segment 为新归属
- revoke   撤销，target 指向被撤销变更的 record
- settle   已结算账期，[start_day, end_day) 左闭右开，归属冻结
"""

from collections import Counter, defaultdict
from csv import Error

from app.business import DomainIssue
from app.intake.numeric import quantity

EVENT_KINDS = ("change", "revoke", "settle")
HEADER = ("record", "customer", "kind", "start_day", "end_day", "segment", "target", "note")


def _partition(candidates):
    """事件版重复判定：完全一致记 DUPLICATE_REPLAY，内容冲突全部隔离。"""
    by_key = defaultdict(list)
    for line, row in candidates:
        by_key[row["record"]].append((line, row))
    accepted, rejected = [], []
    for key, items in by_key.items():
        first = items[0][1]

        def fingerprint(row):
            return {k: v for k, v in row.items() if k != "_line"}

        if any(fingerprint(row) != fingerprint(first) for _, row in items):
            rejected.extend(
                {
                    "line": line,
                    "record": key,
                    "customer": row["customer"],
                    "reason": "DUPLICATE_CONFLICT",
                }
                for line, row in items
            )
            continue
        accepted.append(first)
        rejected.extend(
            {
                "line": line,
                "record": key,
                "customer": row["customer"],
                "reason": "DUPLICATE_REPLAY",
            }
            for line, row in items[1:]
        )
    return accepted, rejected


def _parse_event(values):
    if len(values) != len(HEADER):
        raise DomainIssue("HISTORY_COLUMN_COUNT")
    row = dict(zip(HEADER, values))
    for key in ("record", "customer", "target"):
        row[key] = row[key].strip()
    if not (
        row["record"]
        and all(char.isalnum() or char in "_.-" for char in row["record"])
        and len(row["record"]) <= 36
    ):
        raise DomainIssue("HISTORY_BAD_IDENTIFIER")
    if not (
        row["customer"]
        and all(char.isalnum() or char in "_.-" for char in row["customer"])
        and len(row["customer"]) <= 36
    ):
        raise DomainIssue("HISTORY_BAD_IDENTIFIER")
    if row["kind"] not in EVENT_KINDS:
        raise DomainIssue("HISTORY_UNKNOWN_KIND")
    if len(row["note"]) > 200:
        raise DomainIssue("HISTORY_NOTE_TOO_LONG")
    row["start_day"] = quantity(row["start_day"]) if row["start_day"].strip() else 0
    row["end_day"] = quantity(row["end_day"]) if row["end_day"].strip() else None
    if row["segment"].strip():
        row["segment"] = str(quantity(row["segment"]))
    else:
        row["segment"] = None
    if row["kind"] == "change" and (row["segment"] is None or row["end_day"] is not None):
        raise DomainIssue("HISTORY_CHANGE_SHAPE")
    if row["kind"] == "revoke":
        if not row["target"] or row["segment"] is not None:
            raise DomainIssue("HISTORY_REVOKE_SHAPE")
    if row["kind"] == "settle":
        if row["end_day"] is None or row["end_day"] <= row["start_day"]:
            raise DomainIssue("HISTORY_SETTLE_SHAPE")
    return row


def parse_events(data):
    """返回 (采信事件, 隔离行, 汇总)。首行必须匹配事件表头。"""
    counted = blanks = 0
    candidates, rejected = [], []
    try:
        for line, values in records_with_history_header(data):
            counted += 1
            if not any(value.strip() for value in values):
                blanks += 1
                continue
            if tuple(values) == HEADER:
                continue
            try:
                candidates.append((line, _parse_event(values)))
            except DomainIssue as error:
                rejected.append(
                    dict(
                        line=line,
                        record=values[0][:36] if values else "",
                        customer=values[1][:36] if len(values) > 1 else "",
                        reason=error.code,
                    )
                )
    except Error as error:
        raise DomainIssue("CSV_MALFORMED") from error
    accepted, conflicts = _partition(candidates)
    line_by_record = {row["record"]: line for line, row in candidates}
    for event in accepted:
        event["_line"] = line_by_record[event["record"]]
    isolation = sorted(rejected + conflicts, key=lambda item: item["line"])
    summary = dict(
        input=counted,
        blank=blanks,
        accepted=len(accepted),
        rejected=len(isolation),
        reasons=dict(Counter(item["reason"] for item in isolation)),
    )
    return accepted, isolation, summary


def records_with_history_header(data):
    """与 intake.reader.records 相同的读取规则，但校验事件表头。"""
    from app.config import MAX_FILE, MAX_ROWS
    import csv
    import io

    if len(data) > MAX_FILE:
        raise DomainIssue("FILE_TOO_LARGE")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise DomainIssue("ENCODING_INVALID") from error
    if not text:
        raise DomainIssue("FILE_EMPTY")
    if not text.strip():
        raise DomainIssue("FILE_BLANK")
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    if tuple(next(reader)) != HEADER:
        raise DomainIssue("HISTORY_HEADER_MISMATCH")
    count = 0
    for number, row in enumerate(reader, 1):
        if number > MAX_ROWS:
            raise DomainIssue("ROW_LIMIT")
        count += 1
        yield reader.line_num, row
    if count == 0:
        raise DomainIssue("HEADER_ONLY")


def group_by_customer(events):
    grouped = defaultdict(list)
    for event in sorted(events, key=lambda item: (item["customer"], item["record"])):
        grouped[event["customer"]].append(event)
    return grouped
