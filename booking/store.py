"""共享资源预约台：SQLite 持久化层。"""

import datetime
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS resources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id INTEGER NOT NULL,
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    cancelled INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (resource_id) REFERENCES resources(id)
);
"""


def connect(db_path):
    """打开（必要时初始化）数据库，返回启用外键的连接。

    连接处于 autocommit 模式，由调用方显式使用 BEGIN IMMEDIATE
    管理事务，保证多进程下“检查冲突 + 写入”的原子性。
    """
    conn = sqlite3.connect(db_path)
    conn.isolation_level = None
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    # 兼容取消功能上线前创建的数据库：补齐 cancelled 列，
    # 已有预约一律视为未取消，无需重建数据。
    columns = {row[1] for row in conn.execute("PRAGMA table_info(bookings)")}
    if "cancelled" not in columns:
        conn.execute(
            "ALTER TABLE bookings ADD COLUMN cancelled INTEGER NOT NULL DEFAULT 0"
        )
    return conn


def insert_resource(conn, name):
    """登记资源，返回新的 resource_id。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute("INSERT INTO resources (name) VALUES (?)", (name,))
        resource_id = cur.lastrowid
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return resource_id


def list_resources(conn, contains=None):
    """返回全部资源，按 resource_id 数值升序。

    每项只含 resource_id 与 name；name 为登记时保存的原值，
    不去重、不按名称排序。只读查询，不写入任何记录、不消耗标识。

    contains 不为 None 时只返回名称包含该片段的资源：区分大小写的
    连续子串比较，百分号、下划线等一律按普通字符处理，不做通配、
    正则或字符归一化。在 Python 侧用 in 判断而非 SQL LIKE，
    从机制上保证片段里没有字符会被当作通配符。
    """
    rows = conn.execute(
        "SELECT id, name FROM resources ORDER BY id ASC"
    ).fetchall()
    return [
        {"resource_id": row[0], "name": row[1]}
        for row in rows
        if contains is None or contains in row[1]
    ]


def resource_exists(conn, resource_id):
    row = conn.execute(
        "SELECT 1 FROM resources WHERE id = ?", (resource_id,)
    ).fetchone()
    return row is not None


def has_conflict(conn, resource_id, start, end, exclude_booking_id=None):
    """同一资源上是否存在与 [start, end) 相交的未取消预约（左闭右开）。

    创建（单次与每周重复）与改期共同的时段冲突规则只在此维护：
    只统计同一资源（resource_id 相同）且未取消（cancelled = 0）的预约，
    已取消预约与其他资源的预约不阻挡；相交判定 start < end AND
    end > start 是左闭右开 [start, end) 语义，端点相接不算重叠，
    部分重叠、完全相同与包含关系均冲突。

    exclude_booking_id 为 None（创建）时检查全部已有占用；不为 None
    （改期）时在同一条规则上额外排除该标识的预约——即目标预约自身，
    因此新旧时段重叠乃至时段完全不变都允许成功，但其他预约照常阻挡。
    该参数只取存储层自己的内部标识，不接受外部文本，拼入的 SQL
    片段是固定常量。
    """
    # 排除条件是二选一的固定 SQL 常量，不含任何外部输入；
    # 相交谓词本身在全文只出现这一次，两条路径共用。
    exclude_clause = "AND id != ?" if exclude_booking_id is not None else ""
    parameters = (
        (exclude_booking_id, resource_id, end, start)
        if exclude_booking_id is not None
        else (resource_id, end, start)
    )
    row = conn.execute(
        f"""
        SELECT 1 FROM bookings
        WHERE resource_id = ? AND cancelled = 0
          {exclude_clause}
          AND start < ? AND end > ?
        LIMIT 1
        """,
        parameters,
    ).fetchone()
    return row is not None


def cancel_booking(conn, booking_id):
    """在事务内取消指定预约。

    预约不存在或已经取消返回 False（不改动任何记录）；
    取消成功返回 True。id 由 AUTOINCREMENT 分配，不复用被取消的标识。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "UPDATE bookings SET cancelled = 1 WHERE id = ? AND cancelled = 0",
            (booking_id,),
        )
        if cur.rowcount == 0:
            conn.rollback()
            return False
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return True


def reschedule_booking(conn, booking_id, start, end):
    """在事务内把指定的未取消预约改到新时段，保留原资源与原标识。

    返回 (resource_id, None)；预约不存在或已取消返回
    (None, "booking_not_found")；新时段与同资源“其他”未取消预约相交
    （左闭右开）返回 (None, "booking_conflict")。

    冲突规则与创建共用 has_conflict：相交谓词、同资源与未取消条件
    都只在那一处维护，本函数只通过 exclude_booking_id 传入目标标识，
    显式排除目标行。目标预约自身不阻挡改期，因此新旧时段重叠乃至
    时段完全不变都允许成功；端点相接同样不冲突。任一检查失败即回滚，
    旧时段、取消状态与其他记录全部保持不变，不会出现只释放旧时段的
    中间结果。成功时只更新该行的起止文本：不新增预约、不消耗标识、
    不改资源归属与其他任何记录。每周重复预约在库中各自独立，
    本函数只改指定的一行。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT resource_id, cancelled FROM bookings WHERE id = ?",
            (booking_id,),
        ).fetchone()
        if row is None or row[1] != 0:
            # 不存在与已取消统一视为找不到；已取消预约不能借改期恢复。
            conn.rollback()
            return None, "booking_not_found"
        resource_id = row[0]
        if has_conflict(
            conn, resource_id, start, end, exclude_booking_id=booking_id
        ):
            conn.rollback()
            return None, "booking_conflict"
        conn.execute(
            "UPDATE bookings SET start = ?, end = ? WHERE id = ?",
            (start, end, booking_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return resource_id, None


def query_day_bookings(conn, resource_id, day_start, day_end):
    """返回指定资源上与 [day_start, day_end) 相交的未取消预约。

    时间为定宽文本，字典序即时间先后；起止时间原样返回，不截断跨日
    预约。左闭右开：end == day_start 或 start == day_end 的预约均不含。
    day_end 为 None 时表示不设上界（9999-12-31 的次日无法用日期表示）。
    只读查询，不写入任何记录。
    """
    if day_end is None:
        rows = conn.execute(
            """
            SELECT id, start, end FROM bookings
            WHERE resource_id = ? AND cancelled = 0
              AND end > ?
            ORDER BY start ASC, id ASC
            """,
            (resource_id, day_start),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, start, end FROM bookings
            WHERE resource_id = ? AND cancelled = 0
              AND start < ? AND end > ?
            ORDER BY start ASC, id ASC
            """,
            (resource_id, day_end, day_start),
        ).fetchall()
    return [
        {"booking_id": row[0], "start": row[1], "end": row[2]}
        for row in rows
    ]


def query_free_slots(conn, resource_id, start, end, min_minutes=None,
                     first_only=False):
    """返回 [start, end) 内全部最大连续空闲区间，按开始时间升序。

    只统计同一资源的未取消预约；跨出窗口的预约只按相交部分截断。
    左闭右开：端点相接不算重叠。结果只含非空区间，相邻两项之间必有
    占用时段。每项只含 start 与 end（完整日期时间文本）。
    min_minutes 不为 None 时，只保留窗口内连续分钟数不低于该值的区间；
    合格区间保留完整起止端点，不截成指定长度、不拆分、不跨占用拼接。
    first_only 为 True 时，在最小时长筛选之后只保留开始时间最早的
    一项（筛选使首项过短时继续取后续达标的最早一项）；没有合格区间时
    仍是空列表。合格区间保留完整端点，不截成指定时长。
    只读查询，不写入任何记录。
    """
    rows = conn.execute(
        """
        SELECT start, end FROM bookings
        WHERE resource_id = ? AND cancelled = 0
          AND start < ? AND end > ?
        ORDER BY start ASC, id ASC
        """,
        (resource_id, end, start),
    ).fetchall()
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
    if min_minutes is not None:
        # 区间已按窗口截断，其完整时长即窗口内的连续分钟数。
        free_slots = [
            slot
            for slot in free_slots
            if _minutes_between(slot["start"], slot["end"]) >= min_minutes
        ]
    if first_only:
        # 区间本就按开始时间升序，先按时长筛选（上面）再取首项：
        # 最早区间过短时自然继续取后续最早的达标区间。
        free_slots = free_slots[:1]
    return free_slots


def _minutes_between(start, end):
    """两个 YYYY-MM-DDTHH:mm 文本之间经过的分钟数。

    按完整日期时间求差，跨午夜区间经过的分钟数自然计入；
    输入为分钟精度，结果必为整数分钟。
    """
    time_format = "%Y-%m-%dT%H:%M"
    delta = (
        datetime.datetime.strptime(end, time_format)
        - datetime.datetime.strptime(start, time_format)
    )
    return delta.days * 1440 + delta.seconds // 60


def insert_bookings(conn, resource_id, intervals):
    """在单个事务内为同一资源创建一条或多条预约：全部成功或全部失败。

    单次预约与每周重复预约共同的创建规则只在此维护：资源存在性检查、
    冲突检查与写入都走这一条路径，insert_booking 即本函数的单区间情形。

    intervals 为按发生时间升序的 (start, end) 定宽文本对。依次检查资源
    存在、生成区间彼此不重叠（左闭右开，端点相接不冲突）以及与同资源
    未取消预约不冲突；任一检查失败即回滚，不新增记录、不消耗预约标识。
    成功返回 (booking_ids, None)，标识与 intervals 按顺序一一对应，各项
    独立分配且不复用；资源不存在返回 (None, "resource_not_found")，
    冲突返回 (None, "booking_conflict")。各条预约持久化为普通预约，
    不记录任何组标识。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        if not resource_exists(conn, resource_id):
            conn.rollback()
            return None, "resource_not_found"
        previous_end = None
        for start, end in intervals:
            # 区间按开始时间升序：彼此重叠当且仅当相邻两项相交。
            if previous_end is not None and previous_end > start:
                conn.rollback()
                return None, "booking_conflict"
            if has_conflict(conn, resource_id, start, end):
                conn.rollback()
                return None, "booking_conflict"
            previous_end = end
        booking_ids = []
        for start, end in intervals:
            cur = conn.execute(
                "INSERT INTO bookings (resource_id, start, end) VALUES (?, ?, ?)",
                (resource_id, start, end),
            )
            booking_ids.append(cur.lastrowid)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return booking_ids, None


def insert_booking(conn, resource_id, start, end):
    """在事务内依次检查资源存在与时段冲突，返回 booking_id。

    返回 (booking_id, None)；资源不存在返回 (None, "resource_not_found")；
    时段冲突返回 (None, "booking_conflict")。失败时回滚，不改动任何记录。
    时间以定宽文本存储，字典序即时间先后。

    单次预约即 insert_bookings 的单区间情形，资源检查、冲突处理与保存
    的共同规则集中在 insert_bookings 维护，此处只做返回结构的展开。
    """
    booking_ids, error = insert_bookings(conn, resource_id, [(start, end)])
    if error is not None:
        return None, error
    return booking_ids[0], None
