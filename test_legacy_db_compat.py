"""取消功能上线前旧 SQLite 库兼容承诺的回归测试（仅标准库，python -m unittest 可发现）。

旧库样本保留当前资源表与预约表的字段及 AUTOINCREMENT 自增标识规则，
仅缺少 cancelled 列（模拟取消功能上线前创建的数据库）。样本固定内容：
- 资源 7 与资源 12，各有一条 2026-10-05T09:00 至 10:00 的预约；
- 预约标识分别为 21（资源 7）与 35（资源 12）。

全部业务观察只通过公开命令行入口 `python -m booking --db <文件> <命令>`
的标准输出 JSON 与进程退出码完成，不调用 booking 包内函数；
旧库样本的构造与表结构/原始数据的只读核对使用标准库 sqlite3。
每个用例使用独立的临时数据库文件，结束后自动清理；
不依赖网络、当前日期、机器时区或任何已有演示数据。

覆盖的兼容承诺：
- 旧库首次打开即自动迁移，已有预约一律视为有效：按日查询准确返回原预约
  及其完整起止时间，不同资源记录互不混入；同时段预约返回 booking_conflict，
  冲突失败后查询结果不变；资源名称、资源标识与预约原始时间不被首次打开改变；
- 取消旧预约成功后时段释放，同资源同时段可用新标识（大于样本最大标识 35）
  重新预约；旧标识再次取消返回 booking_not_found 且不影响新预约；
  取消状态与新预约在以独立进程重新打开同一文件后保持一致，资源 12 的预约 35 始终保留；
- 在一份从未被打开过的旧库上以预约标识 0 调用 cancel：invalid_input、退出码 2，
  且表结构（仍无 cancelled 列）与已有数据均不改变。
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

# 固定的样例日期与起止时间（固定 UTC+08:00 的本地时间，与运行环境无关）。
BOOKING_DATE = "2026-10-05"
START = f"{BOOKING_DATE}T09:00"
END = f"{BOOKING_DATE}T10:00"

# 旧库样本中的固定标识与名称（名称刻意区分，便于发现被改写或串号）。
RESOURCE_7 = 7
RESOURCE_12 = 12
NAME_7 = "七号会议室（旧库）"
NAME_12 = "十二号会议室（旧库）"
BOOKING_21 = 21
BOOKING_35 = 35
# 样本中已使用的最大预约标识；迁移后新建预约必须严格大于它（AUTOINCREMENT 不复用）。
MAX_SAMPLE_BOOKING_ID = 35

# 取消功能上线前的表结构：字段与自增规则与当前一致，仅没有 cancelled 列。
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


def create_legacy_db(path):
    """创建一份取消功能上线前的旧库样本（无 cancelled 列），写入固定资源与预约。"""
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(LEGACY_SCHEMA)
        # 显式指定标识，复刻旧库中 AUTOINCREMENT 已分配到 7/12 与 21/35 的状态。
        conn.execute(
            "INSERT INTO resources (id, name) VALUES (?, ?)", (RESOURCE_7, NAME_7)
        )
        conn.execute(
            "INSERT INTO resources (id, name) VALUES (?, ?)", (RESOURCE_12, NAME_12)
        )
        conn.execute(
            "INSERT INTO bookings (id, resource_id, start, end) VALUES (?, ?, ?, ?)",
            (BOOKING_21, RESOURCE_7, START, END),
        )
        conn.execute(
            "INSERT INTO bookings (id, resource_id, start, end) VALUES (?, ?, ?, ?)",
            (BOOKING_35, RESOURCE_12, START, END),
        )
        conn.commit()
    finally:
        conn.close()


class LegacyDatabaseCompatibilityTestCase(unittest.TestCase):
    """每个用例一个独立的临时目录和旧库样本，tearDown 自动清理。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="booking-test-legacy-")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "legacy.sqlite"
        create_legacy_db(self.db_path)

    # ---- 公开命令的调用辅助 ----

    def run_cli(self, *args, db=None):
        """运行一条 booking 命令，返回 CompletedProcess。"""
        return subprocess.run(
            [sys.executable, "-m", "booking", "--db", str(db or self.db_path), *args],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def run_ok(self, *args, db=None):
        """运行命令并断言退出码 0、标准输出为单个 JSON 对象，返回解析结果。"""
        proc = self.run_cli(*args, db=db)
        self.assertEqual(proc.returncode, 0, msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        payload = json.loads(proc.stdout)  # 若输出不是单个 JSON 文档会抛错
        self.assertIsInstance(payload, dict)
        return payload

    def run_error(self, expected_error, *args, db=None):
        """运行命令并断言退出码 2、输出为指定的错误对象。"""
        proc = self.run_cli(*args, db=db)
        self.assertEqual(proc.returncode, 2, msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        self.assertEqual(json.loads(proc.stdout), {"error": expected_error})
        return proc

    def day_query(self, resource_id, date=BOOKING_DATE):
        return self.run_ok(
            "day-query", "--resource", str(resource_id), "--date", date
        )

    # ---- 直接对样本文件做只读核对（构造旧库与验证表结构/原始数据所必需） ----

    def booking_columns(self):
        conn = sqlite3.connect(str(self.db_path))
        try:
            return [row[1] for row in conn.execute("PRAGMA table_info(bookings)")]
        finally:
            conn.close()

    def raw_resources(self):
        conn = sqlite3.connect(str(self.db_path))
        try:
            return conn.execute("SELECT id, name FROM resources ORDER BY id").fetchall()
        finally:
            conn.close()

    def raw_bookings(self):
        """返回含 cancelled 列的完整行；未迁移的旧库调用前不应使用此方法。"""
        conn = sqlite3.connect(str(self.db_path))
        try:
            return conn.execute(
                "SELECT id, resource_id, start, end, cancelled "
                "FROM bookings ORDER BY id"
            ).fetchall()
        finally:
            conn.close()

    def raw_bookings_without_cancelled(self):
        """旧库原样四列（不含 cancelled），用于核对未迁移文件的已有数据。"""
        conn = sqlite3.connect(str(self.db_path))
        try:
            return conn.execute(
                "SELECT id, resource_id, start, end FROM bookings ORDER BY id"
            ).fetchall()
        finally:
            conn.close()

    def schema_snapshot(self):
        """两张表的列结构快照，用于核对一次调用前后表结构完全不变。"""
        conn = sqlite3.connect(str(self.db_path))
        try:
            return {
                "resources": [row[1] for row in conn.execute("PRAGMA table_info(resources)")],
                "bookings": [row[1] for row in conn.execute("PRAGMA table_info(bookings)")],
            }
        finally:
            conn.close()

    # ---- 首次打开：旧预约仍有效，数据不被改变 ----

    def test_first_open_keeps_legacy_bookings_active_and_unchanged(self):
        """旧库首次 day-query 即完成迁移：原预约有效、跨资源不混入、同时段冲突，原始数据不变。"""
        # 前置条件：样本确实是没有 cancelled 列的旧库。
        self.assertEqual(self.booking_columns(), ["id", "resource_id", "start", "end"])

        # 首次打开：查询资源 7，退出码 0，日期回显，列表准确包含原预约 21 与完整起止时间。
        payload_7 = self.day_query(RESOURCE_7)
        self.assertEqual(
            payload_7,
            {
                "resource_id": RESOURCE_7,
                "date": BOOKING_DATE,
                "bookings": [{"booking_id": BOOKING_21, "start": START, "end": END}],
            },
        )

        # 资源 12 的记录不混入资源 7 的结果；反向同样隔离。
        payload_12 = self.day_query(RESOURCE_12)
        self.assertEqual(
            payload_12,
            {
                "resource_id": RESOURCE_12,
                "date": BOOKING_DATE,
                "bookings": [{"booking_id": BOOKING_35, "start": START, "end": END}],
            },
        )

        # 旧预约仍被视为有效：资源 7 同一时段提交预约 → booking_conflict，退出码 2。
        self.run_error(
            "booking_conflict",
            "reserve", "--resource", str(RESOURCE_7),
            "--start", START, "--end", END,
        )
        # 冲突失败不改动记录：随后查询结果与首次打开时完全一致。
        self.assertEqual(self.day_query(RESOURCE_7), payload_7)
        self.assertEqual(self.day_query(RESOURCE_12), payload_12)

        # 首次打开不得改变资源名称、资源标识与预约原始时间；
        # 迁移仅补齐 cancelled 列，两条旧预约一律视为未取消（值为 0）。
        self.assertEqual(
            self.raw_resources(),
            [(RESOURCE_7, NAME_7), (RESOURCE_12, NAME_12)],
        )
        self.assertEqual(
            self.raw_bookings(),
            [
                (BOOKING_21, RESOURCE_7, START, END, 0),
                (BOOKING_35, RESOURCE_12, START, END, 0),
            ],
        )

    # ---- 取消旧预约 → 重新预约 → 重开持久化 ----

    def test_cancel_legacy_booking_then_rebook_persists_across_reopens(self):
        """取消旧预约 21 后时段释放、新标识 > 35；旧标识不可再取消；重开文件后状态一致，35 始终保留。"""
        # 首次打开完成迁移，并确认两条旧预约初始都有效。
        self.assertEqual(
            self.day_query(RESOURCE_7)["bookings"],
            [{"booking_id": BOOKING_21, "start": START, "end": END}],
        )

        # 取消预约 21：成功返回原标识与 cancelled=true（独立进程）。
        payload = self.run_ok("cancel", "--booking", str(BOOKING_21))
        self.assertEqual(payload, {"booking_id": BOOKING_21, "cancelled": True})

        # 资源 7 的按日结果变为空数组；资源 12 的预约 35 不受影响。
        self.assertEqual(self.day_query(RESOURCE_7)["bookings"], [])
        self.assertEqual(
            self.day_query(RESOURCE_12)["bookings"],
            [{"booking_id": BOOKING_35, "start": START, "end": END}],
        )

        # 同一时段再次预约成功，获得新标识，且严格大于样本中的最大预约标识 35。
        payload = self.run_ok(
            "reserve", "--resource", str(RESOURCE_7),
            "--start", START, "--end", END,
        )
        new_id = payload["booking_id"]
        self.assertEqual(
            payload,
            {
                "booking_id": new_id,
                "resource_id": RESOURCE_7,
                "start": START,
                "end": END,
            },
        )
        self.assertGreater(new_id, MAX_SAMPLE_BOOKING_ID)
        self.assertNotIn(new_id, (BOOKING_21, BOOKING_35))

        # 再次取消旧标识 21：booking_not_found，退出码 2（独立进程）。
        self.run_error("booking_not_found", "cancel", "--booking", str(BOOKING_21))

        # 新预约仍可查询，且不影响资源 12 的预约 35。
        self.assertEqual(
            self.day_query(RESOURCE_7)["bookings"],
            [{"booking_id": new_id, "start": START, "end": END}],
        )
        self.assertEqual(
            self.day_query(RESOURCE_12)["bookings"],
            [{"booking_id": BOOKING_35, "start": START, "end": END}],
        )

        # 以新的独立进程重新打开同一文件：取消状态（21 已取消）与新预约保持一致，
        # 同时段再预约仍冲突，预约 35 始终保留。
        self.run_error(
            "booking_conflict",
            "reserve", "--resource", str(RESOURCE_7),
            "--start", START, "--end", END,
        )
        self.run_error("booking_not_found", "cancel", "--booking", str(BOOKING_21))
        self.assertEqual(
            self.day_query(RESOURCE_7)["bookings"],
            [{"booking_id": new_id, "start": START, "end": END}],
        )
        self.assertEqual(
            self.day_query(RESOURCE_12)["bookings"],
            [{"booking_id": BOOKING_35, "start": START, "end": END}],
        )
        # 磁盘上的最终状态：21 已取消、新预约与 35 未取消，原始时间未被改写。
        self.assertEqual(
            self.raw_bookings(),
            [
                (BOOKING_21, RESOURCE_7, START, END, 1),
                (BOOKING_35, RESOURCE_12, START, END, 0),
                (new_id, RESOURCE_7, START, END, 0),
            ],
        )
        self.assertEqual(
            self.raw_resources(),
            [(RESOURCE_7, NAME_7), (RESOURCE_12, NAME_12)],
        )

    # ---- 未打开的旧库上非法输入：不连接、不迁移、不改数据 ----

    def test_cancel_with_id_zero_on_unopened_legacy_db_changes_nothing(self):
        """未打开过的旧库上 cancel --booking 0：invalid_input、退出码 2，表结构与数据原样保留。"""
        # 调用前快照：表结构仍是旧结构（无 cancelled 列），数据为样本原样。
        schema_before = self.schema_snapshot()
        self.assertEqual(
            schema_before,
            {
                "resources": ["id", "name"],
                "bookings": ["id", "resource_id", "start", "end"],
            },
        )
        data_before = {
            "resources": self.raw_resources(),
            "bookings": self.raw_bookings_without_cancelled(),
        }

        # 输入校验先于数据库连接：非法标识不触发首次打开/迁移。
        self.run_error("invalid_input", "cancel", "--booking", "0")

        # 表结构与已有数据均未改变：bookings 依旧没有 cancelled 列。
        self.assertEqual(self.schema_snapshot(), schema_before)
        self.assertEqual(
            self.raw_bookings_without_cancelled(),
            data_before["bookings"],
        )
        self.assertEqual(self.raw_resources(), data_before["resources"])
        self.assertEqual(
            self.raw_bookings_without_cancelled(),
            [
                (BOOKING_21, RESOURCE_7, START, END),
                (BOOKING_35, RESOURCE_12, START, END),
            ],
        )


if __name__ == "__main__":
    unittest.main()
