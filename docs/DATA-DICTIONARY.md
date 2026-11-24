# 字段与业务定义

输入编码 UTF-8，允许 BOM。首行必须匹配样例列顺序。记录键和业务组保留原始字符串。`kind` 允许 version, transaction, expected。说明列 `note` 可以跨行，限制 200 字。

业务字段按 `start_day`、`end_day`、`segment`、`cents` 排列。数字接受非负整数、全角数字和完整千分位。空串、短横线、N/A、NULL 与未知为缺值。无关字段用 0 占位。

生效范围左闭右开。客户同一时段只允许一个生效版本。无法命中的交易单列明细且禁止发布，保留已经关联的金额。

`incoming.csv` 包含四个业务组，第二组提供不同边界，第三组触发业务拒收，第四组需要核对。另含重复表头、冲突记录、重复回放与错误列数。`ready.csv` 含可以签发的两个组。

## 客户历史事件字段

事件文件编码与数值规则同上，表头固定为 `record,customer,kind,start_day,end_day,segment,target,note`，生效区间左闭右开。

| kind | 含义 | 必填 |
|---|---|---|
| `change` | 归属变更，`start_day` 为生效点，`segment` 为新归属，`end_day` 留空 | `record`、`customer`、`start_day`、`segment` |
| `revoke` | 撤销，`target` 填被撤销变更的 `record`，自身不带归属 | `record`、`customer`、`target` |
| `settle` | 已结算账期，`[start_day, end_day)` 内归属冻结，发布时按当时归属落锁 | `record`、`customer`、`start_day`、`end_day` |

样例 `history_ready.csv` 为可发布批次（含一笔可落锁的结算）；`history_incoming.csv` 覆盖晚到变更区间裁剪、定向撤销与结算账期冲突。
