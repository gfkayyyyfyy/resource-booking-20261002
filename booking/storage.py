"""SQLite 持久化层：建库、登记资源、提交预约与冲突判断。"""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS resources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id INTEGER NOT NULL REFERENCES resources(id),
    start TEXT NOT NULL,
    end TEXT NOT NULL
);
"""


class Storage:
    def __init__(self, path):
        # isolation_level=None：由代码显式管理事务，便于使用 BEGIN IMMEDIATE。
        # 文件不存在时 sqlite3 会创建新文件，建表语句负责初始化；
        # 已存在的库则直接沿用其中数据。
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.execute("PRAGMA foreign_keys = ON")
        # 多进程并发时让后来的写事务等待锁，而不是立刻报 database is locked。
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.executescript(SCHEMA)

    def close(self):
        self.conn.close()

    def add_resource(self, name):
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "INSERT INTO resources(name) VALUES (?)", (name,)
            )
            resource_id = cur.lastrowid
            conn.execute("COMMIT")
            return resource_id
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def resource_exists(self, resource_id):
        row = self.conn.execute(
            "SELECT 1 FROM resources WHERE id = ?", (resource_id,)
        ).fetchone()
        return row is not None

    def add_booking(self, resource_id, start, end):
        """原子地完成冲突判断与写入。

        返回新建预约的正整数 id；与同一资源已有预约重叠时返回 None。
        采用左闭右开区间 [start, end)：重叠当且仅当
        start < 对方 end 且 对方 start < end。
        时间固定为等宽的 YYYY-MM-DDTHH:MM 文本（固定 UTC+08:00），
        字典序与时间先后一致，含跨日场景，可直接在 SQL 中比较。
        """
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            if conn.execute(
                """
                SELECT 1 FROM bookings
                WHERE resource_id = ? AND start < ? AND ? < end
                LIMIT 1
                """,
                (resource_id, end, start),
            ).fetchone():
                conn.execute("ROLLBACK")
                return None
            cur = conn.execute(
                """
                INSERT INTO bookings(resource_id, start, end)
                VALUES (?, ?, ?)
                """,
                (resource_id, start, end),
            )
            booking_id = cur.lastrowid
            conn.execute("COMMIT")
            return booking_id
        except Exception:
            conn.execute("ROLLBACK")
            raise
