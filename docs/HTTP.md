# 阶段接口

`POST /api/stages/<stage>` 接收 `multipart/form-data`。字段 `sample=dirty`、`sample=clean` 或 `file=<CSV>` 三选一。阶段为 `intake`、`prepare`、`calculate`、`policy`、`balance`。预览不入库。

`POST /api/runs` 接收同样的输入，执行全批并返回运行报告。`GET /api/runs` 返回最近 50 批。`GET /api/runs/<id>` 查询一批和当前复核状态。

`PATCH /api/runs/<id>` 接收 `revision`、`status` 和 `note`。状态使用 `APPROVED` 或 `HELD`。冲突返回 409，客户端读取新版本后重试。

`GET /api/runs/<id>/evidence` 下载内含运行清单、行号隔离、业务证据和复核信息的 JSON。

## 客户历史接口

事件 CSV 表头：`record,customer,kind,start_day,end_day,segment,target,note`，同样接受 `multipart/form-data` 的 `file` 或 `sample`。

- `POST /api/history/preview`：不入库预览，返回时间线、受影响区间（含前后归属）、冲突与 `publishable`。
- `POST /api/history/batches`：登记事件批次，同字节重放幂等（`inserted=0`、`manifest.replayed=true`）；`GET /api/history/batches` 列批次，`GET /api/history/batches/<id>` 看详情。
- `POST /api/history/batches/<id>/resolutions`：处理冲突，字段 `record`、`reason`（`CONFIRMED_CONFLICT` 或 `SOURCE_CORRECTED`）、`note`（3–200 字）。
- `POST /api/history/batches/<id>/publish`：冲突未处理返回 409 `HISTORY_UNRESOLVED`；成功生成递增正式版本并固化账期锁，发布失败回滚并保留旧版。
- `GET /api/history/current`：当前正式版本与归属时间线。
- `GET /api/history/batches/<id>/evidence`：下载含时间线、影响区间、冲突、账期锁与处理留痕的 JSON 依据。
