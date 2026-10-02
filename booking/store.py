"""共享资源预约台：SQLite 持久化层。"""

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
    _migrate(conn)
    return conn


def _migrate(conn):
    """为旧版本数据库补齐后加的列，已有数据原样保留。"""
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(bookings)")
    }
    if "cancelled" not in columns:
        conn.execute(
            "ALTER TABLE bookings ADD COLUMN cancelled INTEGER NOT NULL DEFAULT 0"
        )


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


def resource_exists(conn, resource_id):
    row = conn.execute(
        "SELECT 1 FROM resources WHERE id = ?", (resource_id,)
    ).fetchone()
    return row is not None


def has_conflict(conn, resource_id, start, end):
    """同一资源上是否存在与 [start, end) 相交的有效预约（左闭右开）。

    已取消（cancelled = 1）的预约不参与冲突判断。
    """
    row = conn.execute(
        """
        SELECT 1 FROM bookings
        WHERE resource_id = ? AND start < ? AND end > ? AND cancelled = 0
        LIMIT 1
        """,
        (resource_id, end, start),
    ).fetchone()
    return row is not None


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


def cancel_booking(conn, booking_id):
    """在事务内取消指定预约，返回 (booking_id, None)。

    预约不存在或已经取消时返回 (None, "booking_not_found")，
    不改动任何记录。取消是幂等判定之外的一次性状态翻转，
    AUTOINCREMENT 保证后续预约不复用该标识。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT cancelled FROM bookings WHERE id = ?", (booking_id,)
        ).fetchone()
        if row is None or row[0] != 0:
            conn.rollback()
            return None, "booking_not_found"
        conn.execute(
            "UPDATE bookings SET cancelled = 1 WHERE id = ?", (booking_id,)
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return booking_id, None
