# 共享资源预约台

建设服务于会议室和共享设备的本地预约产品，逐步覆盖资源目录、开放时段、预约创建与取消、冲突提示、按日查询、简单重复预约和站内提醒。

当前已实现最小流程：**登记资源 → 提交预约 → 得到确定的冲突结果**。

计划采用：Python 3 标准库 / sqlite3 / datetime / argparse。

## 入口

仅依赖 Python 3 标准库，无需安装第三方包：

```bash
python -m booking --db <sqlite 文件> <命令> [参数]
```

- `--db` 指定 SQLite 文件路径（如 `demo.sqlite`）。
- 文件不存在时自动创建并初始化表结构；已存在则直接沿用其中的数据，重启进程后记录依然有效。

## 命令与示例

### 1. 登记资源 `resource-add`

```bash
python -m booking --db demo.sqlite resource-add --name "一号会议室"
```

成功（退出码 0）：

```json
{"resource_id": 1, "name": "一号会议室"}
```

规则：

- 名称去除首尾空白后不能为空，否则返回输入错误。
- 同名资源允许分别登记，各自获得不同的 `resource_id`。
- `resource_id` 是同一数据库内唯一且稳定的正整数。

### 2. 提交预约 `reserve`

```bash
python -m booking --db demo.sqlite reserve \
  --resource 1 \
  --start 2026-10-05T09:00 \
  --end 2026-10-05T10:00
```

成功（退出码 0）：

```json
{"booking_id": 1, "resource_id": 1, "start": "2026-10-05T09:00", "end": "2026-10-05T10:00"}
```

规则：

- `--resource` 必须是正整数，对应已登记的资源。
- 时间统一解释为**固定 UTC+08:00 的本地时间**（不做时区换算，仅按该挂钟时间存储与比较）。
- 只接受 `YYYY-MM-DDTHH:MM` 格式的有效日期时间：必须是真实存在的日期（含闰年、大小月校验），**不接受秒或时区后缀**。
- 开始时间必须严格早于结束时间；允许跨日时段（如 `23:00` 至次日 `01:00`），也允许过去日期。
- 同一资源的预约按**左闭右开区间** `[start, end)` 判断冲突：
  - 已有 `09:00–10:00` 时，`09:30–10:30` 被拒绝；`10:00–11:00` 可以成功（端点相邻不算重叠）。
  - 完全相同时段、包含或被包含的时段同样被拒绝。
  - 不同资源的预约互不影响。
- `booking_id` 是数据库内唯一且稳定的正整数；返回中的 `start`/`end` 保留输入格式。

## 返回结果

每条业务命令都在标准输出返回一个 JSON 对象。

成功时退出码为 `0`，返回业务字段（见上方示例）。失败时退出码统一为 `2`，错误对象形如：

| JSON 错误 | 触发情形 |
| --- | --- |
| `{"error":"invalid_input"}` | 参数缺失、资源标识不是正整数、名称去空白后为空、时间格式或日期无效、开始时间不早于结束时间 |
| `{"error":"resource_not_found"}` | 资源标识在数据库中不存在 |
| `{"error":"booking_conflict"}` | 与同一资源的已有预约时段重叠 |

校验顺序固定为：**先检查输入是否合法，再检查资源是否存在，最后判断冲突**。任何失败都不会新增资源或预约，也不会修改已有记录。

### 典型流程

```bash
python -m booking --db demo.sqlite resource-add --name "一号会议室"
# {"resource_id": 1, "name": "一号会议室"}

python -m booking --db demo.sqlite reserve --resource 1 --start 2026-10-05T09:00 --end 2026-10-05T10:00
# {"booking_id": 1, "resource_id": 1, "start": "2026-10-05T09:00", "end": "2026-10-05T10:00"}

# 换一个进程（或重启后）再次执行：
python -m booking --db demo.sqlite reserve --resource 1 --start 2026-10-05T09:30 --end 2026-10-05T10:30
# {"error": "booking_conflict"}  （退出码 2）

python -m booking --db demo.sqlite reserve --resource 1 --start 2026-10-05T10:00 --end 2026-10-05T11:00
# {"booking_id": 2, ...}         （相邻时段可以成功）
```
