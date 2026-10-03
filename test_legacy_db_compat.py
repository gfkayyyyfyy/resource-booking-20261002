"""旧版数据库（取消功能上线前创建）首次打开兼容性的回归测试。

对应 README 中 cancel 命令的兼容承诺：取消功能上线前创建的数据库文件
直接兼容，首次打开时自动补齐取消标记列，已有预约一律视为未取消。

旧库样本由标准库 sqlite3 直接构造：resources / bookings 两表的字段与
AUTOINCREMENT 规则与当前版本一致，仅 bookings 表缺少 cancelled 列。
样本含资源 7 与 12，各有一条 2026-10-05T09:00 至 2026-10-05T10:00 的
预约，标识分别为 21 与 35（sqlite_sequence 随之推进，后续新预约标识
必然大于 35）。

业务行为一律通过公开命令行入口 `python -m booking --db <文件> <命令>`
在独立进程中观察 JSON 与退出码，不调用 booking 包内函数；sqlite3 仅用于
构造样本与核对原始表内容。每个用例使用独立的临时数据库并自动清理，
不依赖网络、当前日期、机器时区或已有演示数据。
"""

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# 项目根目录（booking 包所在目录），子进程以此作为工作目录，
# 保证 `python -m booking` 与测试运行时的当前目录无关。
PROJECT_ROOT = Path(__file__).resolve().parent

# 旧版表结构：与当前版本相同的字段与 AUTOINCREMENT 规则，仅无 cancelled 列。
LEGACY_SCHEMA = """
CREATE TABLE resources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL
);
CREATE TABLE bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id INTEGER NOT NULL,
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    FOREIGN KEY (resource_id) REFERENCES resources(id)
);
"""

# 固定的样本数据（固定 UTC+08:00 的本地时间，与运行环境无关）。
BOOKING_DATE = "2026-10-05"
START = f"{BOOKING_DATE}T09:00"
END = f"{BOOKING_DATE}T10:00"
RESOURCE_A_ID = 7
RESOURCE_A_NAME = "旧会议室"
RESOURCE_B_ID = 12
RESOURCE_B_NAME = "旧投影仪"
BOOKING_A_ID = 21  # 资源 7 上的既有预约
BOOKING_B_ID = 35  # 资源 12 上的既有预约，也是样本中的最大预约标识
MAX_LEGACY_BOOKING_ID = 35


def write_legacy_db(db_path):
    """在指定路径构造一份“取消功能上线前”的旧版数据库样本。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(LEGACY_SCHEMA)
        # 显式写入标识：AUTOINCREMENT 会把 sqlite_sequence 推进到
        # 已用最大值，保证后续新预约标识大于样本最大标识 35。
        conn.execute(
            "INSERT INTO resources (id, name) VALUES (?, ?)",
            (RESOURCE_A_ID, RESOURCE_A_NAME),
        )
        conn.execute(
            "INSERT INTO resources (id, name) VALUES (?, ?)",
            (RESOURCE_B_ID, RESOURCE_B_NAME),
        )
        conn.execute(
            "INSERT INTO bookings (id, resource_id, start, end) VALUES (?, ?, ?, ?)",
            (BOOKING_A_ID, RESOURCE_A_ID, START, END),
        )
        conn.execute(
            "INSERT INTO bookings (id, resource_id, start, end) VALUES (?, ?, ?, ?)",
            (BOOKING_B_ID, RESOURCE_B_ID, START, END),
        )
        conn.commit()
    finally:
        conn.close()


def read_table(db_path, sql, params=()):
    """以只读方式核对数据库原始表内容（标准库 sqlite3，不触碰包内实现）。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


class LegacyDbCompatTestCase(unittest.TestCase):
    """每个用例一份独立的旧库样本，tearDown 自动清理。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="booking-legacy-test-")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "legacy.sqlite"
        write_legacy_db(self.db_path)

    # ---- 公开命令的调用辅助 ----

    def run_cli(self, *args):
        """运行一条 booking 命令，返回 CompletedProcess。"""
        return subprocess.run(
            [sys.executable, "-m", "booking", "--db", str(self.db_path), *args],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def run_ok(self, *args):
        """运行命令并断言退出码 0、标准输出为单个 JSON 对象，返回解析结果。"""
        proc = self.run_cli(*args)
        self.assertEqual(proc.returncode, 0, msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        payload = json.loads(proc.stdout)  # 若输出不是单个 JSON 文档会抛错
        self.assertIsInstance(payload, dict)
        return payload

    def run_error(self, expected_error, *args):
        """运行命令并断言退出码 2、输出为指定的错误对象。"""
        proc = self.run_cli(*args)
        self.assertEqual(proc.returncode, 2, msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        self.assertEqual(json.loads(proc.stdout), {"error": expected_error})
        return proc

    def day_query(self, resource_id, date=BOOKING_DATE):
        return self.run_ok("day-query", "--resource", str(resource_id), "--date", date)

    # ---- 原始表内容核对辅助 ----

    def assert_legacy_rows_intact(self):
        """资源名称、资源标识与预约原始时间保持样本原样。"""
        self.assertEqual(
            read_table(self.db_path, "SELECT id, name FROM resources ORDER BY id"),
            [(RESOURCE_A_ID, RESOURCE_A_NAME), (RESOURCE_B_ID, RESOURCE_B_NAME)],
        )
        self.assertEqual(
            read_table(
                self.db_path,
                "SELECT id, resource_id, start, end FROM bookings ORDER BY id",
            ),
            [
                (BOOKING_A_ID, RESOURCE_A_ID, START, END),
                (BOOKING_B_ID, RESOURCE_B_ID, START, END),
            ],
        )

    # ---- 首次打开：旧预约按未取消处理 ----

    def test_first_open_day_query_treats_legacy_bookings_as_active(self):
        """首次用 day-query 打开旧库：原预约 21 可查且完整，资源 12 的记录不混入。"""
        payload = self.day_query(RESOURCE_A_ID)
        self.assertEqual(
            payload,
            {
                "resource_id": RESOURCE_A_ID,
                "date": BOOKING_DATE,
                "bookings": [
                    {"booking_id": BOOKING_A_ID, "start": START, "end": END}
                ],
            },
        )
        # 资源 12 的预约 35 不混入资源 7 的结果，且自身可查。
        self.assertEqual(
            self.day_query(RESOURCE_B_ID)["bookings"],
            [{"booking_id": BOOKING_B_ID, "start": START, "end": END}],
        )

        # 首次打开后：cancelled 列已补齐，且既有预约一律标记为未取消。
        columns = {
            row[1]
            for row in read_table(self.db_path, "PRAGMA table_info(bookings)")
        }
        self.assertIn("cancelled", columns)
        self.assertEqual(
            read_table(
                self.db_path, "SELECT id, cancelled FROM bookings ORDER BY id"
            ),
            [(BOOKING_A_ID, 0), (BOOKING_B_ID, 0)],
        )
        # 资源名称、资源标识与预约原始时间不因首次打开而改变。
        self.assert_legacy_rows_intact()

    def test_legacy_booking_still_blocks_conflicting_reserve(self):
        """旧预约仍被视为有效：同时段预约冲突，且冲突失败后查询结果不变。"""
        before = self.day_query(RESOURCE_A_ID)

        self.run_error(
            "booking_conflict",
            "reserve", "--resource", str(RESOURCE_A_ID),
            "--start", START, "--end", END,
        )

        after = self.day_query(RESOURCE_A_ID)
        self.assertEqual(after, before)
        self.assertEqual(
            after["bookings"],
            [{"booking_id": BOOKING_A_ID, "start": START, "end": END}],
        )
        # 冲突失败不改动任何既有记录。
        self.assert_legacy_rows_intact()

    # ---- 取消 → 再次预约 → 旧标识隔离（每步均为独立进程重开同一文件）----

    def test_cancel_then_rebook_on_legacy_database(self):
        """取消旧预约 21 释放时段；再次预约获得大于 35 的新标识；旧标识不可再取消。"""
        # 取消成功：返回原标识与 cancelled=true。
        payload = self.run_ok("cancel", "--booking", str(BOOKING_A_ID))
        self.assertEqual(payload, {"booking_id": BOOKING_A_ID, "cancelled": True})

        # 资源 7 的按日结果变为空数组（新进程重开后仍然生效）。
        self.assertEqual(
            self.day_query(RESOURCE_A_ID),
            {"resource_id": RESOURCE_A_ID, "date": BOOKING_DATE, "bookings": []},
        )

        # 同一时段再次预约成功，新标识大于样本中的最大预约标识 35。
        payload = self.run_ok(
            "reserve", "--resource", str(RESOURCE_A_ID),
            "--start", START, "--end", END,
        )
        new_id = payload["booking_id"]
        self.assertIsInstance(new_id, int)
        self.assertGreater(new_id, MAX_LEGACY_BOOKING_ID)
        self.assertEqual(
            payload,
            {
                "booking_id": new_id,
                "resource_id": RESOURCE_A_ID,
                "start": START,
                "end": END,
            },
        )

        # 再次取消旧标识 21：booking_not_found，退出码 2。
        self.run_error("booking_not_found", "cancel", "--booking", str(BOOKING_A_ID))

        # 新预约不受旧标识操作影响，重开后仍可查询。
        self.assertEqual(
            self.day_query(RESOURCE_A_ID)["bookings"],
            [{"booking_id": new_id, "start": START, "end": END}],
        )
        # 资源 12 的预约 35 始终保留。
        self.assertEqual(
            self.day_query(RESOURCE_B_ID)["bookings"],
            [{"booking_id": BOOKING_B_ID, "start": START, "end": END}],
        )
        # 旧预约的原始时间记录不因上述操作而改变。
        self.assertEqual(
            read_table(
                self.db_path,
                "SELECT id, resource_id, start, end FROM bookings WHERE id = ?",
                (BOOKING_A_ID,),
            ),
            [(BOOKING_A_ID, RESOURCE_A_ID, START, END)],
        )

    # ---- 非法标识：未打开的旧库保持原样 ----

    def test_invalid_booking_id_leaves_unopened_legacy_db_untouched(self):
        """在未打开的旧库上以标识 0 调用 cancel：invalid_input，表结构与数据均未改变。"""
        self.run_error("invalid_input", "cancel", "--booking", "0")

        # 输入校验先于数据库打开：表结构保持旧版（无 cancelled 列）。
        columns = {
            row[1]
            for row in read_table(self.db_path, "PRAGMA table_info(bookings)")
        }
        self.assertNotIn("cancelled", columns)
        # 已有数据完全未变。
        self.assert_legacy_rows_intact()


if __name__ == "__main__":
    unittest.main()
