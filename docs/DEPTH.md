# 客户历史维表回溯台规则登记

- 规模：中型

## 功能模块

| 模块 | 动作 | 接口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 样例采信 | 预览并定位坏行 | `POST /api/stages/intake` | `app/intake/service.py` | `tests/test_intake.py` |
| M2 版本编排 | 检查领域约束 | `POST /api/stages/prepare` | `app/modules/prepare.py` | `tests/test_prepare.py` |
| M3 时点关联 | 查看业务计算结果 | `POST /api/stages/calculate` | `app/modules/calculate.py` | `tests/test_calculate.py` |
| M4 补数判定 | 查看签收判定 | `POST /api/stages/policy` | `app/modules/policy.py` | `tests/test_policy.py` |
| M5 运行对账 | 对照输入、采信和隔离行 | `POST /api/stages/balance` | `app/modules/reconcile.py` | `tests/test_reconcile.py` |
| M6 批次复核 | 执行批次并签发或暂缓 | `POST /api/runs、PATCH /api/runs/<id>` | `app/review/service.py` | `tests/test_review.py` |
| M7 证据交付 | 下载复核清单 | `GET /api/runs/<id>/evidence` | `app/evidence/render.py` | `tests/test_evidence.py` |
| M8 客户历史维护 | 维护可签发的归属时间线 | `POST /api/history/preview、/api/history/batches` | `app/history/timeline.py` | `tests/test_history_timeline.py` |
| M9 历史冲突与发布 | 处理冲突、冻结账期、发布正式版本 | `POST /api/history/batches/<id>/resolutions、/publish` | `app/history/service.py`、`app/history/store.py` | `tests/test_history_service.py`、`tests/test_history_http.py` |

## 客户历史维护规则（M8/M9）

事件类型 `change`（归属变更，生效点 + 新归属）、`revoke`（撤销，`target` 指向被撤销变更）、`settle`（已结算账期，左闭右开）。

1. 时间线只由生效点决定，与导入顺序无关；晚到变更只覆盖其生效点至下一条确认变更之间，不冲掉后续确认归属。
2. 已结算账期落账期锁（`history_locks`），后续候选时间线在锁区间内归属不一致报 `SETTLED_LOCKED`，未覆盖报同名冲突；账期申报本身未覆盖报 `SETTLE_UNCOVERED`，跨多个归属报 `SETTLE_SPLIT_OWNERSHIP`。
3. 撤销只回退 `target` 指向的变更；目标不存在或已被撤销报 `REVOKE_TARGET_MISSING`，不删除任何无关分段。
4. 同一生效点出现不同归属报 `SAME_EFFECTIVE_CONFLICT`。
5. 提交前 `/preview` 与批次详情列出受影响区间、前后归属（`before`/`after`）与待核原因；存在未处理冲突或隔离行时不能发布（`HISTORY_UNRESOLVED`，409）。
6. 冲突处理只写 `history_resolutions`（账本只追加，不删事件），被处理事件从时间线构建中排除。
7. 同字节批次以指纹幂等：重放返回同一批次且 `inserted=0`，不新增事件；并发提交由 `UNIQUE(batch_id, record)` 兜底。
8. 发布在单事务内写入版本与账期锁，任何异常整笔回滚，旧版本与依据保留（`HISTORY_PUBLISH_FAILED`）。

## 输入陷阱

| 编号 | 规则 | 处理 | 测试 |
|---|---|---|---|
| TRAP-02 | 去除 BOM | `app/intake/reader.py` | `tests/test_intake.py::test_bom_preserves_identifier` |
| TRAP-03 | 按逻辑记录读取引号内换行 | `app/intake/reader.py` | `tests/test_intake.py::test_quoted_newline_is_one_logical_record` |
| TRAP-04 | 重复表头计入忽略行 | `app/intake/service.py` | `tests/test_intake.py::test_repeated_header_is_counted` |
| TRAP-05 | 异常列数隔离 | `app/intake/schema.py` | `tests/test_intake.py::test_extra_column_is_isolated` |
| TRAP-06 | 归一化全角数字和千分位 | `app/intake/numeric.py` | `tests/test_intake.py::test_full_width_and_grouping_are_canonical` |
| TRAP-07 | 空值行隔离 | `app/intake/numeric.py` | `tests/test_intake.py::test_sentinel_keeps_row_out_of_total` |
| TRAP-08 | 记录键保留前导零 | `app/intake/schema.py` | `tests/test_intake.py::test_bom_preserves_identifier` |
| TRAP-14 | 冲突版本都进入隔离 | `app/intake/resolve.py` | `tests/test_intake.py::test_duplicate_conflict_removes_both_copies` |
| TRAP-16 | 引号未闭合时拒收文件 | `app/intake/reader.py` | `tests/test_intake.py::test_unclosed_quote_invalidates_source` |
| TRAP-18 | 区分空文件、全空白和仅表头 | `app/intake/reader.py` | `tests/test_intake.py::test_empty_states_have_distinct_causes` |
| TRAP-26 | 拒绝旧版本复核 | `app/review/store.py` | `tests/test_review.py::test_review_optimistic_lock` |

## 领域反例

生效范围左闭右开。客户同一时段只允许一个生效版本。无法命中的交易单列明细且禁止发布，保留已经关联的金额。

逐组预期结论见 `tests/test_policy.py`，计算过程证据见 `tests/test_calculate.py`。
