from flask import jsonify

LABELS = {
    "FILE_TOO_LARGE": "文件超过 256 KiB",
    "FILE_EMPTY": "文件没有内容",
    "FILE_BLANK": "文件只有空白",
    "HEADER_ONLY": "文件只有标题行",
    "HEADER_MISMATCH": "列名与字段字典不一致",
    "ENCODING_INVALID": "请使用 UTF-8 CSV",
    "CSV_MALFORMED": "CSV 引号或行格式异常",
    "ROW_LIMIT": "文件超过 2000 行",
    "FILE_REQUIRED": "请选择 CSV 文件",
    "RUN_NOT_FOUND": "批次不存在",
    "REVIEW_FIELDS": "请填写 3 至 200 字复核意见",
    "STALE_REVIEW": "批次已更新，请刷新",
    "RUN_FINALIZED": "批次已签发",
    "UNRESOLVED_ISSUES": "存在待处理差异",
    "SAMPLE_UNKNOWN": "样例不存在",
    "STAGE_UNKNOWN": "处理阶段不存在",
    "HISTORY_HEADER_MISMATCH": "事件列名与客户历史字段字典不一致",
    "HISTORY_COLUMN_COUNT": "事件行列数不匹配",
    "HISTORY_BAD_IDENTIFIER": "事件记录键或客户标识不合法",
    "HISTORY_UNKNOWN_KIND": "事件类型仅支持 change、revoke、settle",
    "HISTORY_NOTE_TOO_LONG": "说明列超过 200 字",
    "HISTORY_CHANGE_SHAPE": "归属变更需带生效点与新归属",
    "HISTORY_REVOKE_SHAPE": "撤销需指向目标变更且不能携带归属",
    "HISTORY_SETTLE_SHAPE": "结算账期需为左闭右开的有效区间",
    "HISTORY_SETTLE_SPLIT": "结算账期跨越多个归属，不能冻结",
    "HISTORY_BATCH_NOT_FOUND": "客户历史批次不存在",
    "HISTORY_BATCH_FINALIZED": "客户历史批次已发布，不可再改",
    "HISTORY_RECORD_UNKNOWN": "待处理事件不在该批次中",
    "HISTORY_RESOLUTION_REASON": "处理原因不合法",
    "HISTORY_RESOLUTION_NOTE": "请填写 3 至 200 字待核说明",
    "HISTORY_RESOLUTION_FIELDS": "请提供事件记录键与处理原因",
    "HISTORY_UNRESOLVED": "冲突未处理，不能形成正式版本",
    "HISTORY_PUBLISH_FAILED": "发布失败，已保留旧版本与依据",
}


def response(error):
    code = error.code
    status = (
        404
        if code in {"RUN_NOT_FOUND", "SAMPLE_UNKNOWN", "STAGE_UNKNOWN",
                    "HISTORY_BATCH_NOT_FOUND"}
        else (
            409
            if code in {"STALE_REVIEW", "RUN_FINALIZED", "UNRESOLVED_ISSUES",
                        "HISTORY_UNRESOLVED", "HISTORY_BATCH_FINALIZED",
                        "HISTORY_PUBLISH_FAILED"}
            else 413 if code == "FILE_TOO_LARGE" else 422
        )
    )
    return jsonify(code=code, message=LABELS.get(code, code)), status
