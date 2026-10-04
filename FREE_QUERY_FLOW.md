# free-query 空闲时段查询流程说明（可与源码逐条核对）

本文只描述当前版本中 **free-query（空闲时段查询，含可选 `--min-minutes` 最小时长筛选）**
这一条已有流程：一次命令行调用如何从参数变成标准输出上的一个 JSON 对象。
不引入、不修改任何产品代码、公开命令行接口或数据库表结构。

文中每条关键结论都标注了源码位置（文件 + 函数 + 行号，行号以当前版本为准），
并附简短代码片段支撑；文末的两个查询示例可直接在 shell 中复现。

- 入口文件：`booking/__main__.py`
- 持久化层：`booking/store.py`
- 运行方式：在包含 `booking/` 包的目录（项目根目录）下执行
  `python -m booking --db <sqlite 文件> free-query ...`
  （必须用 `-m booking`；直接运行 `booking/__main__.py` 会因相对导入失败。）

---

## 1. 端到端流程总览

`main(argv)`（`booking/__main__.py:246`）按固定顺序完成下列阶段：

1. **构造参数解析器**（`build_parser`，`booking/__main__.py:210`；free-query 子命令在
   `booking/__main__.py:236-241`）。
2. **在连接数据库之前完成全部输入校验**：资源标识、时间窗口、可选的最小时长
   （`main` 中 try 块，`booking/__main__.py:250-296`）。任一不合法 →
   输出 `{"error":"invalid_input"}`、退出码 2，**此时尚未打开数据库**。
3. **打开数据库**：`store.connect`（`booking/store.py:22`），文件不存在会初始化，
   旧库会补齐 `cancelled` 列。
4. **超大标识短路**：超过 SQLite INTEGER 上限的合法正整数直接按“资源不存在”处理，
   不绑定给 SQLite（`booking/__main__.py:306-308`）。
5. **查资源是否存在**：`store.resource_exists`（`booking/store.py:76`）；不存在 →
   `{"error":"resource_not_found"}`、退出码 2（`booking/__main__.py:343-344`）。
6. **计算空闲区间**：`store.query_free_slots`（`booking/store.py:153`）——
   先一条 SQL 取同资源、未取消、与窗口相交的占用，再在 Python 侧扫描窗口、
   切出最大连续空闲区间，最后按 `--min-minutes` 过滤。
7. **组装并输出结果**：退出码 0，单个 JSON 对象，`free_slots` 按开始时间升序
   （`booking/__main__.py:345-356`）。

输出统一走 `_emit`（`booking/__main__.py:60-62`）：只向**标准输出**打印一个 JSON
对象（`print` 自带一个换行），返回值即进程退出码；错误路径不向标准错误写任何内容。

```python
def _emit(payload, exit_code):
    print(json.dumps(payload, ensure_ascii=False))
    return exit_code

def invalid_input():
    return _emit({"error": "invalid_input"}, 2)
```

---

## 2. 参数校验（先于一切数据库动作）

### 2.1 free-query 声明了哪些参数

`build_parser`（`booking/__main__.py:236-241`）：

```python
p_free_query = subparsers.add_parser("free-query")
p_free_query.add_argument("--resource", required=True)
p_free_query.add_argument("--start", required=True)
p_free_query.add_argument("--end", required=True)
# 可选：只保留窗口内连续分钟数不低于该值的空闲区间；缺省不过滤。
p_free_query.add_argument("--min-minutes", default=None)
```

`--db` 是全局必填参数（`booking/__main__.py:212`）。缺参数、无法识别的参数等
argparse 默认报错被自定义解析器转成同一个异常（`booking/__main__.py:53-57`）：

```python
class _Parser(argparse.ArgumentParser):
    """把 argparse 的默认报错（退出码 2 + stderr 文本）转为 UsageError。"""
    def error(self, message):
        raise UsageError(message)
```

因此缺参数等也走 `UsageError` → `invalid_input()`，不会出现 argparse 的原生 stderr 文本。

### 2.2 资源标识：`parse_positive_int`

调用点：`booking/__main__.py:288`；定义：`booking/__main__.py:69-91`。

```python
POSITIVE_INT_RE = re.compile(r"^\d+\Z")
...
def parse_positive_int(text):
    if text is None or not POSITIVE_INT_RE.match(text):
        raise UsageError("expected positive integer")
    ...  # 剥前导零；纯零拒绝
    if value > SQLITE_MAX_ID:
        return OVERSIZED_ID
    return value
```

- 必须是正整数文本；零、负数、小数、含空白/换行/其他字符的文本抛 `UsageError`。
  正则用 `\Z`（不是 `$`）锚定绝对结尾，所以 `"1\n"` 不会被当成 1（见
  `booking/__main__.py:23-26` 的注释）。
- 允许任意前导零，按数值解释。
- 数值大于 `2^63-1` 时不报错，而是返回哨兵 `OVERSIZED_ID`
  （`booking/__main__.py:41-46`）——这是**合法输入**，留到第 4 阶段按“不存在”处理。

### 2.3 时间窗口：`parse_time` + `parse_time_window`

调用点：`booking/__main__.py:289`；定义：`booking/__main__.py:139-164`。

```python
TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
TIMEZONE_OFFSET = datetime.timezone(datetime.timedelta(hours=8))

def parse_time(text):
    if text is None or not TIME_RE.match(text):
        raise UsageError("expected YYYY-MM-DDTHH:mm")
    try:
        parsed = datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M")
    except ValueError:
        raise UsageError("invalid date or time")
    return parsed.replace(tzinfo=TIMEZONE_OFFSET)

def parse_time_window(start_text, end_text):
    start_dt = parse_time(start_text)
    end_dt = parse_time(end_text)
    if not start_dt < end_dt:
        raise UsageError("start must be strictly before end")
    return start_text, end_text
```

由此得到三条结论：

1. **固定 UTC+08:00、分钟精度**：只接受定宽、零填充的 `YYYY-MM-DDTHH:mm`，
   拒绝秒、时区后缀等；解析结果一律挂上固定的 UTC+08:00 时区，与运行机器的时区无关。
   日期本身还必须真实存在（`strptime` 对 `2026-02-30`、`12:60` 抛 `ValueError`）。
2. **过去日期合法、跨午夜窗口合法**：`parse_time_window` 唯一的顺序约束是
   `start_dt < end_dt`，全函数没有任何与“当前时间”比较的代码，所以过去窗口（如
   `2000-01-01T09:30` 至 `12:00`）与跨午夜窗口（如 `2026-10-05T22:00` 至
   `2026-10-06T02:00`）都正常返回。端点是带完整日期的定宽文本，字典序即时间先后，
   跨午夜不会错位。
3. **起止相等或起止倒置被拒绝**：相等时 `start_dt < end_dt` 为假、倒置时同样为假，
   统一抛 `UsageError`，即 `{"error":"invalid_input"}`、退出码 2。

校验成功后返回的是**原始文本**（`booking/__main__.py:158-164`），成功结果顶层原样
回显，定宽文本也直接交给存储层比较。

### 2.4 最小时长：`parse_min_minutes`

调用点：`booking/__main__.py:290-292`；定义：`booking/__main__.py:116-123`，
数值规则集中在共用的 `parse_bounded_ascii_int`（`booking/__main__.py:94-113`）：

```python
BOUNDED_ASCII_INT_RE = re.compile(r"^[0-9]+\Z")
MAX_MIN_MINUTES = 1440
...
def parse_bounded_ascii_int(text, lower, upper, message):
    if text is None or not BOUNDED_ASCII_INT_RE.match(text):
        raise UsageError(message)
    stripped = text.lstrip("0")
    if not stripped or len(stripped) > len(str(upper)):
        raise UsageError(message)
    value = int(stripped)
    if not lower <= value <= upper:
        raise UsageError(message)
    return value

def parse_min_minutes(text):
    return parse_bounded_ascii_int(
        text, 1, MAX_MIN_MINUTES, "expected integer minutes in [1, 1440]"
    )
```

- 仅接受 ASCII 十进制数字（`[0-9]`，全角/阿拉伯文数字不算），允许前导零并按数值解释
  （`0030` 即 30）；用 `\Z` 锚定，带空格、制表符、换行、`+`、小数点等一律拒绝。
- 取值范围是闭区间 **[1, 1440]**：零、负数、小数、`1441`、超长数字串都抛
  `UsageError`（缺值由 argparse 捕获，同样落入 `UsageError`）。
- 省略该参数时为 `None`，表示不做时长过滤（`booking/__main__.py:290-292`）。

### 2.5 校验顺序：invalid_input 先于资源是否存在，且不碰数据库

free-query 的三项校验都在 `main` 的同一个 try 块内，而 `store.connect(...)` 在
try 块**之后**（`booking/__main__.py:287-299`）：

```python
elif args.command == "free-query":
    resource_id = parse_positive_int(args.resource)
    start, end = parse_time_window(args.start, args.end)
    min_minutes = None
    if args.min_minutes is not None:
        min_minutes = parse_min_minutes(args.min_minutes)
...
    except UsageError:
        return invalid_input()

# 输入合法后再打开数据库（不存在则初始化），后续进程沿用同一文件。
conn = store.connect(args.db)
```

因此：

- 非法时间、非法资源标识、非法 `--min-minutes` 中任意一项出现，都在连接前返回
  `{"error":"invalid_input"}`、退出码 2、stderr 为空；**即使同时给了不存在的资源，
  也是 invalid_input 优先**（非法的 `--min-minutes 0` + `--resource 999` 仍报
  invalid_input）。
- 由于根本没有执行 `sqlite3.connect`，指向**尚不存在的数据库路径**时不会创建文件，
  也不会触发旧库迁移（回归测试：`test_free_query.py:275`、
  `test_free_min_minutes.py:339-390`）。

---

## 3. 打开数据库与资源查找

### 3.1 `store.connect`：合法请求的初始化/旧库迁移副作用

`booking/store.py:22-39`：

```python
def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.isolation_level = None
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)                 # CREATE TABLE IF NOT EXISTS ...
    columns = {row[1] for row in conn.execute("PRAGMA table_info(bookings)")}
    if "cancelled" not in columns:
        conn.execute(
            "ALTER TABLE bookings ADD COLUMN cancelled INTEGER NOT NULL DEFAULT 0"
        )
    return conn
```

- `SCHEMA`（`booking/store.py:6-19`）用 `CREATE TABLE IF NOT EXISTS` 建
  `resources` 与 `bookings` 两表；`bookings` 含
  `cancelled INTEGER NOT NULL DEFAULT 0`。
- 输入**全部合法**时（哪怕随后会因资源不存在而失败），这里都会正常打开文件：
  文件不存在就新建并建表；旧库缺少 `cancelled` 列就 `ALTER TABLE` 补齐，已有预约
  一律视为未取消。实测：对不存在的路径用未知资源做一次合法 free-query，
  返回 resource_not_found 的同时文件已被创建。

### 3.2 超大正整数标识：合法但必然不存在

`booking/__main__.py:301-308`：

```python
if args.command in ("reserve", "day-query", "free-query"):
    if resource_id is OVERSIZED_ID:
        return _emit({"error": "resource_not_found"}, 2)
```

真实标识由 SQLite INTEGER 主键分配，不可能超过 `9223372036854775807`（2^63-1）。
`9223372036854775808` 或更长的正整数是**合法输入**，所以数据库照常打开（可能初始化/
迁移），随后在绑定给 SQLite 之前短路成 `resource_not_found`、退出码 2。
边界值 2^63-1 不触发短路，按普通整数交给数据库查询。

### 3.3 普通资源存在性检查

free-query 分支（`booking/__main__.py:342-344`）配合
`store.resource_exists`（`booking/store.py:76-80`）：

```python
if not store.resource_exists(conn, resource_id):
    return _emit({"error": "resource_not_found"}, 2)
```

```python
def resource_exists(conn, resource_id):
    row = conn.execute(
        "SELECT 1 FROM resources WHERE id = ?", (resource_id,)
    ).fetchone()
    return row is not None
```

普通未知正整数（如 `999`）走到这里返回 `{"error":"resource_not_found"}`、退出码 2、
stderr 为空。**先过这一关，才会执行空闲查询。**

---

## 4. 占用如何变成空闲区间：`store.query_free_slots`

源码：`booking/store.py:153-191`。

### 4.1 第一步：只取“同资源、未取消、与窗口相交”的占用

```python
rows = conn.execute(
    """
    SELECT start, end FROM bookings
    WHERE resource_id = ? AND cancelled = 0
      AND start < ? AND end > ?
    ORDER BY start ASC, id ASC
    """,
    (resource_id, end, start),
).fetchall()
```

四个限定条件各自对应一条可核对的结论：

1. `resource_id = ?`：**只有同一资源的预约算占用**，其他资源当天订满也不影响本资源。
2. `cancelled = 0`：**只有未取消预约算占用**；已取消行不会被选出（实测取消预约 1
   后，原 09:00–10:00 时段重新出现在 free_slots 中）。
3. `start < :window_end AND end > :window_start`：**左闭右开的相交判断**。
   - 完全在窗口外的行不满足，被忽略；
   - 跨出窗口边界的行满足条件被选出，但只取相交部分（见 4.2 的截断）；
   - 结束于窗口起点（`end == start`）或开始于窗口终点（`start == end`）的预约
     **不满足**严格不等式，故端点相接不算相交，不影响结果。
4. `ORDER BY start ASC, id ASC`：占用按开始时间升序（开始相同再按 id），这是最终
   free_slots 按开始时间升序的来源。

### 4.2 第二步：单趟扫描窗口，切出“最大连续空闲区间”

```python
free_slots = []
cursor = start
for booking_start, booking_end in rows:
    # 只保留落在窗口内的相交部分（定宽文本，字典序即时间先后）。
    busy_start = max(booking_start, start)
    busy_end = min(booking_end, end)
    if cursor < busy_start:
        free_slots.append({"start": cursor, "end": busy_start})
    if cursor < busy_end:
        cursor = busy_end
if cursor < end:
    free_slots.append({"start": cursor, "end": end})
```

要点：

- `cursor` 初始化为窗口起点 `start`，逐条占用推进；每条预约先被
  `max/min` **截断到窗口内**（`busy_start`/`busy_end`），所以跨出窗口的占用只计算
  与窗口相交的部分，窗口外的部分既不会产生空闲项也不会错误地推移游标。
- 空闲段只在严格有间隔时入表：`if cursor < busy_start`。因此两条**端点相接**的占用
  （如 10:00–10:30 与 10:30–11:00）之间 `cursor == busy_start`，**不会产生零长度
  空闲项**；完全占满窗口时结果为空列表。
- 每一段空闲都是被两侧边界（窗口边缘或占用）界定的**最大连续区间**：循环只沿时间轴
  顺序前进，从不回头，也**不会跨越某段占用把前后两段拼起来**。
- 循环结束后 `if cursor < end` 补上最后一段到窗口终点；窗口内无占用时，整段窗口作为
  唯一项返回。每项只含 `start` 与 `end`，均为完整日期时间文本。

### 4.3 第三步：可选的最小时长过滤

`booking/store.py:184-190`：

```python
if min_minutes is not None:
    # 区间已按窗口截断，其完整时长即窗口内的连续分钟数。
    free_slots = [
        slot
        for slot in free_slots
        if _minutes_between(slot["start"], slot["end"]) >= min_minutes
    ]
```

分钟数由 `_minutes_between`（`booking/store.py:194-205`）按**完整日期时间**求差：

```python
def _minutes_between(start, end):
    time_format = "%Y-%m-%dT%H:%M"
    delta = (
        datetime.datetime.strptime(end, time_format)
        - datetime.datetime.strptime(start, time_format)
    )
    return delta.days * 1440 + delta.seconds // 60
```

由此：

- 比较是 **`>=`**：连续分钟数**恰好等于**阈值的区间保留（等于阈值不算淘汰）。
  区间在过滤前已按窗口截断，所以参与比较的就是“窗口内连续分钟数”，跨出窗口的占用
  不会被计入。
- 跨午夜区间按完整日期求差，经过的分钟数自然正确（分钟精度下结果必为整数）。
- 过滤只是一个**布尔判定**：合格的区间**保留完整起止端点**，不会被截成阈值长度，
  不会被拆分，也不会与别的段合并；不合格直接从列表剔除。
- 省略 `--min-minutes` 时 `min_minutes is None`，整段过滤跳过，结果与无该参数的查询
  逐字节一致。
- 资源存在但**没有任何达标区间**时，列表为空，仍属成功：`free_slots: []`、退出码 0。

### 4.4 第四步：组装成功结果

`booking/__main__.py:345-356`：

```python
free_slots = store.query_free_slots(
    conn, resource_id, start, end, min_minutes
)
return _emit(
    {
        "resource_id": resource_id,
        "start": start,
        "end": end,
        "free_slots": free_slots,
    },
    0,
)
```

顶层 `resource_id` 为数值，`start`/`end` 原样回显输入文本；`free_slots` 每项只有
`start`、`end` 两个键。占用经 SQL 升序取出、循环按该顺序追加、过滤保持顺序，
所以 **free_slots 必然按开始时间升序**。连接在 `finally` 中关闭
（`booking/__main__.py:393-394`）。

---

## 5. 可复现的两个查询示例（共用同一组数据）

以下命令在项目根目录（含 `booking/` 包的目录）执行，使用一个全新的数据库文件，
**不依赖任何已有数据库**；资源标识取自登记命令的返回结果。时间端点均写完整日期。

### 5.1 准备数据

```sh
# 全新数据库；登记一个会议室（返回的 resource_id 即后续使用的资源标识）
python3 -m booking --db demo.sqlite resource-add --name "一号会议室"
# {"resource_id": 1, "name": "一号会议室"}

# 在 2026-10-05 创建两条未取消预约：09:00-10:00 与 10:30-11:00
python3 -m booking --db demo.sqlite reserve --resource 1 \
    --start 2026-10-05T09:00 --end 2026-10-05T10:00
# {"booking_id": 1, "resource_id": 1, "start": "2026-10-05T09:00", "end": "2026-10-05T10:00"}

python3 -m booking --db demo.sqlite reserve --resource 1 \
    --start 2026-10-05T10:30 --end 2026-10-05T11:00
# {"booking_id": 2, "resource_id": 1, "start": "2026-10-05T10:30", "end": "2026-10-05T11:00"}
```

新库首个资源标识为 1；若登记返回的是其他值，把下面命令里的 `--resource 1`
换成实际返回的 `resource_id` 即可。

### 5.2 查询一：窗口 09:30–12:00，`--min-minutes 30`

```sh
python3 -m booking --db demo.sqlite free-query --resource 1 \
    --start 2026-10-05T09:30 --end 2026-10-05T12:00 --min-minutes 30
```

标准输出（退出码 **0**，标准错误为空）：

```json
{"resource_id": 1, "start": "2026-10-05T09:30", "end": "2026-10-05T12:00", "free_slots": [{"start": "2026-10-05T10:00", "end": "2026-10-05T10:30"}, {"start": "2026-10-05T11:00", "end": "2026-10-05T12:00"}]}
```

扫描过程（对应 4.2 的代码，窗口 `[09:30, 12:00)`，`cursor` 初值 `09:30`）：

- 占用 `09:00–10:00`：截断后 `busy = max(09:00,09:30)=09:30` 至
  `min(10:00,12:00)=10:00`。`cursor(09:30) < busy_start(09:30)` 不成立，不产生前缀；
  游标推进到 `10:00`。**首条占用只有窗口内的 `[09:30,10:00)` 部分生效**——
  `[09:00,09:30)` 在查询窗口之外，不应消耗窗口内任何空闲。
- 占用 `10:30–11:00`：`busy = 10:30–11:00`。`cursor(10:00) < 10:30` 成立，加入
  `{10:00, 10:30}`；游标推进到 `11:00`。
- 收尾：`cursor(11:00) < 12:00`，加入 `{11:00, 12:00}`。
- 时长过滤：两段分别为 30 分钟、60 分钟。阈值用 `>=`，**30 恰好等于阈值，保留**；
  60 也保留。合格段**保留完整端点**，所以第一段是完整的 `10:00–10:30`，
  不会被改成别的长度。

### 5.3 查询二：同一窗口，`--min-minutes 31`

```sh
python3 -m booking --db demo.sqlite free-query --resource 1 \
    --start 2026-10-05T09:30 --end 2026-10-05T12:00 --min-minutes 31
```

标准输出（退出码 **0**，标准错误为空）：

```json
{"resource_id": 1, "start": "2026-10-05T09:30", "end": "2026-10-05T12:00", "free_slots": [{"start": "2026-10-05T11:00", "end": "2026-10-05T12:00"}]}
```

两次查询切出的原始空闲段完全相同；差别只在过滤：`10:00–10:30` 为 30 分钟，
`30 >= 31` 为假被剔除，`11:00–12:00` 为 60 分钟保留，于是只剩一段。
两次结果都按开始时间升序。

### 5.4 一次性核对退出码与标准错误

```sh
python3 -m booking --db demo.sqlite free-query --resource 1 \
    --start 2026-10-05T09:30 --end 2026-10-05T12:00 --min-minutes 30 \
    >out.txt 2>err.txt
echo "exit=$?"          # exit=0
wc -c err.txt           # 0（标准错误为空）
cat out.txt
```

---

## 6. 这条流程的真实边界（逐条对照源码）

**占用范围**

- 只有**同一资源**（SQL `resource_id = ?`）且**未取消**（`cancelled = 0`）的预约影响
  空闲结果；其他资源、已取消预约、与窗口不相交的预约都不入选
  （`booking/store.py:163-171`）。
- 左闭右开：占用与窗口、占用与占用之间均按严格不等式判交，**端点相接既不算重叠，
  相接的两条占用之间也没有零长度空闲项**（SQL 的 `start < ? AND end > ?`；
  扫描时的 `if cursor < busy_start`）。
- 空闲段是最大连续区间：**不跨占用拼接时长**，跨出窗口的占用只按相交部分截断
  （`booking/store.py:176-183`）。
- 没有达标区间（包括窗口被完全占满）时，仍是成功返回 `free_slots: []`、退出码 0，
  不视为错误（过滤在 `booking/store.py:184-190`，成功分支在
  `booking/__main__.py:348-356`）。

**错误形态（free-query 只有两类业务错误）**

| 情形 | 标准输出 | 退出码 | 源码位置 |
| --- | --- | --- | --- |
| 非法时间、非法资源标识、非法 `--min-minutes`、缺参数、起止相等/倒置 | `{"error":"invalid_input"}` | 2 | `booking/__main__.py:295-296`、`65-66` |
| 输入全部合法但资源不存在（含超过 2^63-1 的正整数标识） | `{"error":"resource_not_found"}` | 2 | `booking/__main__.py:306-308`、`343-344` |

- 两类错误的标准错误都为空（argparse 报错已被 `_Parser.error` 转成 `UsageError`，
  全程无堆栈、无 argparse 原生文本）。
- **invalid_input 先于资源是否存在的判断**：所有校验在 `store.connect` 之前完成，
  非法输入既不查资源，也不创建数据库文件、不迁移旧库
  （`booking/__main__.py:250-299`）。
- 超大正整数是**合法输入**，故它与普通未知标识一样先打开数据库（文件不存在则初始化、
  旧库照常补齐 `cancelled` 列），再返回 resource_not_found
  （`booking/__main__.py:299-308`，`booking/store.py:22-39`）。

**“只读”的确切含义——不要概括成“绝不写文件”**

- free-query 的**业务效果是只读**的：`query_free_slots` 只执行 `SELECT`
  （`booking/store.py:163-171`），不插入、不更新、不删除任何预约或资源；
  不开启写事务，因而**不消耗 AUTOINCREMENT 标识**——查询后新建的预约仍取紧随其后的
  id（实测查询若干次后，新预约得到 id 3；回归测试
  `test_free_query.py:204-215`、`test_free_min_minutes.py:272-285`）。
- 但一次**输入合法**的请求在进入查询前会调用 `store.connect`：路径不存在时
  `sqlite3.connect` + `CREATE TABLE IF NOT EXISTS` 会**新建并初始化文件**；旧库缺少
  `cancelled` 列时会执行 `ALTER TABLE` **补齐该列**（`booking/store.py:28-38`）。
  这是“打开数据库”的既有行为，对 free-query、reserve、day-query 一视同仁。
- 只有 **invalid_input 路径**在连接数据库之前返回，才保证不创建文件、不迁移旧库；
  合法请求（即使结果是 resource_not_found）不享受这一保证。

**其他入口不受影响**：本文未改动任何源码。resource-add、reserve、cancel、
resource-list、day-query 的参数、输出与数据库格式保持原有行为；free-query 省略
`--min-minutes` 时也与加入该参数前完全一致。

---

## 附：关键结论到源码的索引

| 结论 | 文件 : 函数（行） |
| --- | --- |
| 时间固定 UTC+08:00、严格分钟格式 | `booking/__main__.py:16,19`，`parse_time` `139-147` |
| 过去/跨午夜合法；相等/倒置拒绝 | `parse_time_window` `booking/__main__.py:150-164` |
| 资源标识正整数规则与超大值哨兵 | `parse_positive_int` `booking/__main__.py:69-91`；`41-46` |
| `--min-minutes` 取值 [1,1440]、ASCII、`\Z` 锚定 | `parse_min_minutes`/`parse_bounded_ascii_int` `booking/__main__.py:94-123` |
| 校验先于连接，invalid_input 退出码 2、stderr 空 | `main` `booking/__main__.py:250-299`；`_emit`/`invalid_input` `60-66`；`_Parser.error` `53-57` |
| 打开即建库/补 cancelled 列 | `store.connect` `booking/store.py:22-39`（SCHEMA `6-19`） |
| 超大标识短路为 resource_not_found | `booking/__main__.py:301-308` |
| 资源存在性检查 | `store.resource_exists` `booking/store.py:76-80`；调用点 `booking/__main__.py:342-344` |
| 只取同资源未取消且相交的占用（左闭右开、升序） | `store.query_free_slots` SQL `booking/store.py:163-171` |
| 截断、相接无零长度项、不跨占用拼接 | `booking/store.py:172-183` |
| 阈值 `>=`、保留完整端点、空数组仍成功 | `booking/store.py:184-190`；`_minutes_between` `194-205` |
| 成功结果结构、退出码 0、按开始时间升序 | `booking/__main__.py:342-356` |
