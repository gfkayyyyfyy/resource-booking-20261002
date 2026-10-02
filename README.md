# 共享资源预约台

服务于会议室和共享设备的本地预约工具。当前版本提供最小流程：**登记资源 → 提交预约 → 得到确定的冲突结果 → 取消预约 → 按日查询**，数据持久化在单个 SQLite 文件中。

仅使用 Python 3 标准库（`sqlite3` / `datetime` / `argparse`），无需安装任何依赖。

## 入口

```
python -m booking --db <sqlite 文件> <命令> [参数 ...]
```

- `--db` 指定 SQLite 文件路径；文件不存在时自动初始化，后续进程沿用其中数据。
- 每次业务命令都在标准输出打印一个 JSON 对象：成功退出码为 `0`，失败退出码统一为 `2`。

## 示例

```sh
# 登记资源（同名资源允许分别登记，各自得到独立 id）
python -m booking --db demo.sqlite resource-add --name "一号会议室"
# {"resource_id": 1, "name": "一号会议室"}

python -m booking --db demo.sqlite resource-add --name "  二号会议室  "
# {"resource_id": 2, "name": "二号会议室"}   （名称保存时去除首尾空白）

# 提交预约
python -m booking --db demo.sqlite reserve --resource 1 \
    --start 2026-10-05T09:00 --end 2026-10-05T10:00
# {"booking_id": 1, "resource_id": 1, "start": "2026-10-05T09:00", "end": "2026-10-05T10:00"}

# 与 09:00–10:00 重叠 → 冲突
python -m booking --db demo.sqlite reserve --resource 1 \
    --start 2026-10-05T09:30 --end 2026-10-05T10:30
# {"error": "booking_conflict"}

# 取消预约 1；被取消的时段随后可以重新预约
python -m booking --db demo.sqlite cancel --booking 1
# {"booking_id": 1, "cancelled": true}

python -m booking --db demo.sqlite reserve --resource 1 \
    --start 2026-10-05T09:00 --end 2026-10-05T10:00
# {"booking_id": 2, "resource_id": 1, "start": "2026-10-05T09:00", "end": "2026-10-05T10:00"}
# （新预约使用新的 booking_id，不复用被取消的标识）

# 左闭右开：10:00 与上一时段端点相接，不冲突
python -m booking --db demo.sqlite reserve --resource 1 \
    --start 2026-10-05T10:00 --end 2026-10-05T11:00
# {"booking_id": 3, "resource_id": 1, "start": "2026-10-05T10:00", "end": "2026-10-05T11:00"}

# 按日查询：跨日预约完整返回，按 start、booking_id 排序
python -m booking --db demo.sqlite day-query --resource 1 --date 2026-10-05
# {"resource_id": 1, "date": "2026-10-05", "bookings": [{"booking_id": 2, "start": "2026-10-05T09:00", "end": "2026-10-05T10:00"}, {"booking_id": 3, "start": "2026-10-05T10:00", "end": "2026-10-05T11:00"}]}
```

## 命令

### resource-add —— 登记资源

```
resource-add --name <名称>
```

- 名称去除首尾空白后不能为空，保存后的名称为去除空白的结果。
- 同名资源允许分别登记，各自获得独立标识。
- 成功返回：`{"resource_id": <正整数>, "name": "<保存后的名称>"}`。
- `resource_id` 为正整数，在同一数据库内唯一且稳定。

### reserve —— 提交预约

```
reserve --resource <resource_id> --start <开始时间> --end <结束时间>
```

- 成功返回：`{"booking_id": <正整数>, "resource_id": <正整数>, "start": "...", "end": "..."}`，时间按输入格式原样返回。
- `booking_id` 为正整数，在同一数据库内唯一且稳定。

### cancel —— 取消预约

```
cancel --booking <booking_id>
```

- 取消指定预约；成功返回：`{"booking_id": <正整数>, "cancelled": true}`。
- 被取消的时段立即恢复为可预约状态，且该预约不再参与后续冲突判断；取消结果持久化，重启进程后仍然有效。
- 取消只作用于目标预约，不改变资源信息或其他预约；过去日期与跨日预约同样允许取消。
- 新预约始终获得新的 `booking_id`（`AUTOINCREMENT`），不复用被取消的标识；再次取消旧标识不影响后来创建的预约。
- 预约标识不存在或已经取消时，统一返回 `{"error": "booking_not_found"}`，不改动任何记录。
- 取消功能上线前创建的数据库文件直接兼容：首次打开时自动补齐取消标记列，已有预约一律视为未取消。

### day-query —— 按日查询

```
day-query --resource <resource_id> --date <YYYY-MM-DD>
```

- 查询指定资源在某个本地日期（固定 UTC+08:00）的全部有效（未取消）预约。
- 查询区间为查询日 `00:00` 至次日 `00:00` 的左闭右开区间：预约与之有实际交集即返回（`start < 次日00:00` 且 `end > 当日00:00`）；结束于当日 `00:00` 或开始于次日 `00:00` 的预约不属于查询日，跨日预约完整返回、不截断，同一预约只出现一次。
- 成功返回：`{"resource_id": <正整数>, "date": "<YYYY-MM-DD>", "bookings": [...]}`；`bookings` 中每项为 `{"booking_id": <正整数>, "start": "...", "end": "..."}`，使用原预约标识与完整起止时间，按 `start` 升序、开始时间相同时按 `booking_id` 升序排列；资源存在但没有匹配预约时为空数组。
- 查询为只读操作：不新增或改动任何预约记录，也不消耗预约标识；其他资源的预约不返回。
- 日期采用严格零填充的 `YYYY-MM-DD` 且必须是有效日期（如 `2026-02-30` 非法）；日期格式或日期无效返回 `{"error": "invalid_input"}`。
- 输入合法但资源不存在时返回 `{"error": "resource_not_found"}`。

## 时间规则

- 时间统一解释为**固定 UTC+08:00 的本地时间**（不随运行环境时区变化）。
- 只接受严格的 `YYYY-MM-DDTHH:mm` 格式（如 `2026-10-05T09:00`），必须零填充；不接受秒（`:00:00`）、时区后缀（`Z` / `+08:00`）等其他写法。
- 日期必须是有效日期（如 `2026-02-30` 非法）。
- 开始时间必须**严格早于**结束时间；允许跨日预约，也允许过去日期。
- 同一资源的预约按**左闭右开区间** `[start, end)` 判断冲突：
  - 已有 `09:00–10:00` 时，`09:30–10:30` 被拒绝（重叠）；
  - `10:00–11:00` 可以成功（端点相接不算重叠）；
  - 完全相同时段、包含或被包含的时段均被拒绝；
  - 不同资源的预约互不影响。

## 返回结果与错误码

所有结果均为标准输出上的单个 JSON 对象，失败时退出码统一为 `2`，检查顺序为：先输入合法性，再资源是否存在，最后冲突判断；失败不会新增资源或预约，也不会改变已有记录。

| 情形 | 返回 | 退出码 |
| --- | --- | --- |
| 成功 | 见各命令 | `0` |
| 参数缺失、资源标识不是正整数、名称为空、时间格式或日期无效、起止顺序错误、预约标识不是正整数（零、负数、小数、非整数）、查询日期格式不符或日期无效 | `{"error": "invalid_input"}` | `2` |
| 资源标识不存在 | `{"error": "resource_not_found"}` | `2` |
| 与同一资源已有预约时段重叠 | `{"error": "booking_conflict"}` | `2` |
| 预约标识不存在或已经取消（仅 cancel） | `{"error": "booking_not_found"}` | `2` |

## 后续规划

资源目录与预约取消之外，逐步支持开放时段、简单重复预约和站内提醒。
