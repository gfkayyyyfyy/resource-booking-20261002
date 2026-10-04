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


def list_resources(conn):
    """返回全部资源，按 resource_id 数值升序。

    每项只含 resource_id 与 name；name 为登记时保存的原值，
    不去重、不按名称排序。只读查询，不写入任何记录、不消耗标识。
    """
    rows = conn.execute(
        "SELECT id, name FROM resources ORDER BY id ASC"
    ).fetchall()
    return [{"resource_id": row[0], "name": row[1]} for row in rows]


def resource_exists(conn, resource_id):
    row = conn.execute(
        "SELECT 1 FROM resources WHERE id = ?", (resource_id,)
    ).fetchone()
    return row is not None


def has_conflict(conn, resource_id, start, end):
    """同一资源上是否存在与 [start, end) 相交的未取消预约（左闭右开）。"""
    row = conn.execute(
        """
        SELECT 1 FROM bookings
        WHERE resource_id = ? AND cancelled = 0
          AND start < ? AND end > ?
        LIMIT 1
        """,
        (resource_id, end, start),
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


def _slot_minutes(slot):
    """空闲区间在窗口内的连续分钟数（起止均为定宽文本，按完整日期时间求差）。"""
    fmt = "%Y-%m-%dT%H:%M"
    start = datetime.datetime.strptime(slot["start"], fmt)
    end = datetime.datetime.strptime(slot["end"], fmt)
    return int((end - start).total_seconds()) // 60


def query_free_slots(conn, resource_id, start, end, min_minutes=None):
    """返回 [start, end) 内全部最大连续空闲区间，按开始时间升序。

    只统计同一资源的未取消预约；跨出窗口的预约只按相交部分截断。
    左闭右开：端点相接不算重叠。结果只含非空区间，相邻两项之间必有
    占用时段。每项只含 start 与 end（完整日期时间文本）。
    min_minutes 不为 None 时，只保留窗口内连续分钟数达到该值的区间；
    合格区间保留完整起止端点，不截断、不拆分。
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
        free_slots = [
            slot for slot in free_slots
            if _slot_minutes(slot) >= min_minutes
        ]
    return free_slots


def insert_booking(conn, resource_id, start, end):
    """在事务内依次检查资源存在与时段冲突，返回 booking_id。

    返回 (booking_id, None)；资源不存在返回 (None, "resource_not_found")；
    时段冲突返回 (None, "booking_conflict")。失败时回滚，不改动任何记录。
    时间以定宽文本存储，字典序即时间先后。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        if not resource_exists(conn, resource_id):
            conn.rollback()
            return None, "resource_not_found"
        if has_conflict(conn, resource_id, start, end):
            conn.rollback()
            return None, "booking_conflict"
        cur = conn.execute(
            "INSERT INTO bookings (resource_id, start, end) VALUES (?, ?, ?)",
            (resource_id, start, end),
        )
        booking_id = cur.lastrowid
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return booking_id, None
