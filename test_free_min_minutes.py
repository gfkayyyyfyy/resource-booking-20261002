"""free-query --min-minutes 最小时长筛选的回归测试（仅标准库，python -m unittest 可发现）。

通过公开命令行入口 `python -m booking --db <文件> <命令>` 准备数据并断言，
每个用例使用独立的临时 SQLite 数据库，结束后自动清理；
不依赖已有数据库、当前日期、机器时区或任何第三方库。
断言均基于解析后的 JSON 内容，不依赖输出对象的键顺序。
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

# 固定的查询日期与相邻日期（固定 UTC+08:00 的本地日期，与运行环境无关）。
QUERY_DATE = "2026-10-05"
PREV_DATE = "2026-10-04"
NEXT_DATE = "2026-10-06"


class FreeQueryMinMinutesTestCase(unittest.TestCase):
    """每个用例一个独立的临时目录和数据库路径，tearDown 自动清理。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="booking-test-")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "test.sqlite"

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
        """运行命令并断言退出码 2、输出为指定的错误对象、stderr 为空。"""
        proc = self.run_cli(*args, db=db)
        self.assertEqual(proc.returncode, 2, msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        self.assertEqual(json.loads(proc.stdout), {"error": expected_error})
        self.assertEqual(proc.stderr, "")
        return proc

    def add_resource(self, name="测试资源"):
        payload = self.run_ok("resource-add", "--name", name)
        return payload["resource_id"]

    def reserve(self, resource_id, start, end):
        payload = self.run_ok(
            "reserve", "--resource", str(resource_id), "--start", start, "--end", end
        )
        return payload["booking_id"]

    def free_query(self, resource_id, start, end, min_minutes=None):
        args = ["free-query", "--resource", str(resource_id), "--start", start, "--end", end]
        if min_minutes is not None:
            args += ["--min-minutes", min_minutes]
        return self.run_ok(*args)

    def prepare_acceptance_resource(self):
        """验收场景数据：09:00-10:00 与 10:30-11:00 两条预约。"""
        resource_id = self.add_resource()
        self.reserve(resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")
        self.reserve(resource_id, f"{QUERY_DATE}T10:30", f"{QUERY_DATE}T11:00")
        return resource_id

    # ---- 验收场景与基本筛选 ----

    def test_acceptance_min_30_returns_both_slots(self):
        """验收场景：09:30-12:00 窗口、--min-minutes 30，返回两段空闲。"""
        resource_id = self.prepare_acceptance_resource()

        payload = self.free_query(
            resource_id, f"{QUERY_DATE}T09:30", f"{QUERY_DATE}T12:00", min_minutes="30"
        )

        self.assertEqual(
            payload,
            {
                "resource_id": resource_id,
                "start": f"{QUERY_DATE}T09:30",
                "end": f"{QUERY_DATE}T12:00",
                "free_slots": [
                    {"start": f"{QUERY_DATE}T10:00", "end": f"{QUERY_DATE}T10:30"},
                    {"start": f"{QUERY_DATE}T11:00", "end": f"{QUERY_DATE}T12:00"},
                ],
            },
        )

    def test_acceptance_min_31_returns_only_longer_slot(self):
        """验收场景：--min-minutes 31 时只返回 11:00-12:00 一段。"""
        resource_id = self.prepare_acceptance_resource()

        payload = self.free_query(
            resource_id, f"{QUERY_DATE}T09:30", f"{QUERY_DATE}T12:00", min_minutes="31"
        )

        self.assertEqual(
            payload["free_slots"],
            [{"start": f"{QUERY_DATE}T11:00", "end": f"{QUERY_DATE}T12:00"}],
        )

    def test_omitted_min_minutes_matches_unfiltered_query(self):
        """省略 --min-minutes 时返回内容与既有查询完全一致。"""
        resource_id = self.prepare_acceptance_resource()

        without = self.free_query(resource_id, f"{QUERY_DATE}T09:30", f"{QUERY_DATE}T12:00")

        self.assertEqual(
            without,
            {
                "resource_id": resource_id,
                "start": f"{QUERY_DATE}T09:30",
                "end": f"{QUERY_DATE}T12:00",
                "free_slots": [
                    {"start": f"{QUERY_DATE}T10:00", "end": f"{QUERY_DATE}T10:30"},
                    {"start": f"{QUERY_DATE}T11:00", "end": f"{QUERY_DATE}T12:00"},
                ],
            },
        )

    def test_exact_duration_boundary_is_inclusive(self):
        """持续分钟数恰好等于下限的区间保留（大于或等于）。"""
        resource_id = self.add_resource()
        self.reserve(resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T09:30")

        payload = self.free_query(
            resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00", min_minutes="30"
        )

        self.assertEqual(
            payload["free_slots"],
            [{"start": f"{QUERY_DATE}T09:30", "end": f"{QUERY_DATE}T10:00"}],
        )

    def test_qualified_slot_keeps_full_endpoints(self):
        """合格区间保留完整起止端点，不截成指定长度、不拆分。"""
        resource_id = self.add_resource()
        self.reserve(resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")

        payload = self.free_query(
            resource_id, f"{QUERY_DATE}T08:00", f"{QUERY_DATE}T12:00", min_minutes="30"
        )

        self.assertEqual(
            payload["free_slots"],
            [
                {"start": f"{QUERY_DATE}T08:00", "end": f"{QUERY_DATE}T09:00"},
                {"start": f"{QUERY_DATE}T10:00", "end": f"{QUERY_DATE}T12:00"},
            ],
        )

    def test_no_qualifying_slot_returns_empty_array_with_success(self):
        """资源存在但没有达标区间：free_slots 为空数组，退出码 0。"""
        resource_id = self.prepare_acceptance_resource()

        payload = self.free_query(
            resource_id, f"{QUERY_DATE}T09:30", f"{QUERY_DATE}T12:00", min_minutes="61"
        )

        self.assertEqual(
            payload,
            {
                "resource_id": resource_id,
                "start": f"{QUERY_DATE}T09:30",
                "end": f"{QUERY_DATE}T12:00",
                "free_slots": [],
            },
        )

    def test_no_bookings_whole_window_returned_only_when_long_enough(self):
        """无占用时整个窗口仅在时长达标时返回，否则为空数组。"""
        resource_id = self.add_resource()

        enough = self.free_query(
            resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00", min_minutes="60"
        )
        not_enough = self.free_query(
            resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00", min_minutes="61"
        )

        self.assertEqual(
            enough["free_slots"],
            [{"start": f"{QUERY_DATE}T09:00", "end": f"{QUERY_DATE}T10:00"}],
        )
        self.assertEqual(not_enough["free_slots"], [])

    def test_cross_midnight_slot_counts_full_elapsed_minutes(self):
        """跨午夜的空闲区间按完整日期计算经过的分钟数。"""
        resource_id = self.add_resource()
        self.reserve(resource_id, f"{QUERY_DATE}T01:00", f"{QUERY_DATE}T02:00")

        # 窗口 23:30 至次日 03:00：空闲为 23:30-01:00（跨午夜共 90 分钟）
        # 与 02:00-03:00（60 分钟）。90 分钟下限时只保留前一段，91 时为空。
        at_limit = self.free_query(
            resource_id, f"{PREV_DATE}T23:30", f"{QUERY_DATE}T03:00", min_minutes="90"
        )
        over_limit = self.free_query(
            resource_id, f"{PREV_DATE}T23:30", f"{QUERY_DATE}T03:00", min_minutes="91"
        )

        self.assertEqual(
            at_limit["free_slots"],
            [{"start": f"{PREV_DATE}T23:30", "end": f"{QUERY_DATE}T01:00"}],
        )
        self.assertEqual(over_limit["free_slots"], [])

    def test_duration_measured_within_window_only(self):
        """跨出窗口的占用只按相交部分截断，窗口内分钟数才计入筛选。"""
        resource_id = self.add_resource()
        # 预约跨出窗口左边界，窗口内空闲为 09:30-10:00（30 分钟）。
        self.reserve(resource_id, f"{QUERY_DATE}T08:00", f"{QUERY_DATE}T09:30")
        self.reserve(resource_id, f"{QUERY_DATE}T10:00", f"{QUERY_DATE}T11:00")

        payload = self.free_query(
            resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T12:00", min_minutes="30"
        )

        self.assertEqual(
            payload["free_slots"],
            [
                {"start": f"{QUERY_DATE}T09:30", "end": f"{QUERY_DATE}T10:00"},
                {"start": f"{QUERY_DATE}T11:00", "end": f"{QUERY_DATE}T12:00"},
            ],
        )

    def test_leading_zeros_and_boundaries_accepted(self):
        """前导零按数值解释；1 与 1440 为合法边界值。"""
        resource_id = self.prepare_acceptance_resource()

        leading_zero = self.free_query(
            resource_id, f"{QUERY_DATE}T09:30", f"{QUERY_DATE}T12:00", min_minutes="0031"
        )
        self.assertEqual(
            leading_zero["free_slots"],
            [{"start": f"{QUERY_DATE}T11:00", "end": f"{QUERY_DATE}T12:00"}],
        )

        minimum = self.free_query(
            resource_id, f"{QUERY_DATE}T09:30", f"{QUERY_DATE}T12:00", min_minutes="1"
        )
        self.assertEqual(len(minimum["free_slots"]), 2)

        wide = self.add_resource("空旷资源")
        maximum = self.free_query(
            wide, f"{QUERY_DATE}T00:00", f"{NEXT_DATE}T00:00", min_minutes="1440"
        )
        self.assertEqual(
            maximum["free_slots"],
            [{"start": f"{QUERY_DATE}T00:00", "end": f"{NEXT_DATE}T00:00"}],
        )

    def test_repeated_queries_are_identical_and_do_not_consume_ids(self):
        """带筛选的查询为只读：重复结果一致，不消耗任何标识。"""
        resource_id = self.prepare_acceptance_resource()

        first = self.free_query(
            resource_id, f"{QUERY_DATE}T09:30", f"{QUERY_DATE}T12:00", min_minutes="30"
        )
        second = self.free_query(
            resource_id, f"{QUERY_DATE}T09:30", f"{QUERY_DATE}T12:00", min_minutes="30"
        )

        self.assertEqual(first, second)
        next_id = self.reserve(resource_id, f"{QUERY_DATE}T11:00", f"{QUERY_DATE}T12:00")
        self.assertEqual(next_id, 3)  # 前两条预约标识为 1、2，查询未消耗标识。

    # ---- 失败路径：--min-minutes 非法值 ----

    def test_invalid_min_minutes_values_return_invalid_input(self):
        """零、负数、小数、超范围、空白及其他字符：invalid_input，退出码 2。"""
        cases = [
            ("空文本", ""),
            ("零", "0"),
            ("纯零带前导零", "000"),
            ("负数", "-30"),
            ("小数", "30.5"),
            ("小于下限的零值小数文本", "0.0"),
            ("超出上限 1441", "1441"),
            ("远超上限", "99999"),
            ("超长数字串", "9" * 5000),
            ("带前导零的超长数字串", "0" * 5000 + "9" * 5000),
            ("前导空格", " 30"),
            ("尾随空格", "30 "),
            ("中间空格", "3 0"),
            ("尾随换行", "30\n"),
            ("尾随回车", "30\r"),
            ("前导引号", "+30"),
            ("非数字文本", "abc"),
            ("全角数字", "３０"),
            ("阿拉伯文数字", "٣٠"),
        ]
        for label, value in cases:
            with self.subTest(label=label):
                self.run_error(
                    "invalid_input",
                    "free-query", "--resource", "1",
                    "--start", f"{QUERY_DATE}T09:30", "--end", f"{QUERY_DATE}T12:00",
                    "--min-minutes", value,
                )

    def test_missing_min_minutes_value_returns_invalid_input(self):
        """--min-minutes 缺值（后面没有参数文本）：invalid_input。"""
        self.run_error(
            "invalid_input",
            "free-query", "--resource", "1",
            "--start", f"{QUERY_DATE}T09:30", "--end", f"{QUERY_DATE}T12:00",
            "--min-minutes",
        )

    def test_invalid_min_minutes_takes_priority_over_resource_not_found(self):
        """非法 --min-minutes 与不存在的资源同时出现时，优先 invalid_input。"""
        self.run_error(
            "invalid_input",
            "free-query", "--resource", "999",
            "--start", f"{QUERY_DATE}T09:30", "--end", f"{QUERY_DATE}T12:00",
            "--min-minutes", "0",
        )

    def test_invalid_min_minutes_does_not_create_database_file(self):
        """非法 --min-minutes 指向尚不存在的数据库路径时，不得创建文件。"""
        missing_db = Path(self._tmpdir.name) / "should-not-exist.sqlite"
        self.run_error(
            "invalid_input",
            "free-query", "--resource", "1",
            "--start", f"{QUERY_DATE}T09:30", "--end", f"{QUERY_DATE}T12:00",
            "--min-minutes", "-5",
            db=missing_db,
        )
        self.assertFalse(missing_db.exists())

    def test_invalid_min_minutes_does_not_migrate_legacy_database(self):
        """非法 --min-minutes 不迁移旧库：表结构与数据保持不变。"""
        legacy_db = Path(self._tmpdir.name) / "legacy.sqlite"
        conn = sqlite3.connect(legacy_db)
        conn.executescript(
            """
            CREATE TABLE resources (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL);
            CREATE TABLE bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                resource_id INTEGER NOT NULL,
                start TEXT NOT NULL,
                end TEXT NOT NULL,
                FOREIGN KEY (resource_id) REFERENCES resources(id)
            );
            INSERT INTO resources (id, name) VALUES (7, '旧资源');
            INSERT INTO bookings (id, resource_id, start, end)
            VALUES (21, 7, '2026-10-05T09:00', '2026-10-05T10:00');
            """
        )
        conn.commit()
        conn.close()

        self.run_error(
            "invalid_input",
            "free-query", "--resource", "7",
            "--start", f"{QUERY_DATE}T09:30", "--end", f"{QUERY_DATE}T12:00",
            "--min-minutes", "abc",
            db=legacy_db,
        )

        conn = sqlite3.connect(legacy_db)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(bookings)")}
            self.assertNotIn("cancelled", columns)  # 未补齐取消标记列
            rows = conn.execute(
                "SELECT id, resource_id, start, end FROM bookings"
            ).fetchall()
            self.assertEqual(rows, [(21, 7, "2026-10-05T09:00", "2026-10-05T10:00")])
        finally:
            conn.close()

    def test_valid_min_minutes_with_unknown_resource_returns_resource_not_found(self):
        """输入全部合法而资源不存在：resource_not_found，退出码 2。"""
        self.add_resource()
        self.run_error(
            "resource_not_found",
            "free-query", "--resource", "999",
            "--start", f"{QUERY_DATE}T09:30", "--end", f"{QUERY_DATE}T12:00",
            "--min-minutes", "30",
        )

    def test_valid_min_minutes_with_oversized_resource_returns_resource_not_found(self):
        """合法 --min-minutes 与超大正整数标识：resource_not_found。"""
        self.add_resource()
        self.run_error(
            "resource_not_found",
            "free-query", "--resource", "9223372036854775808",
            "--start", f"{QUERY_DATE}T09:30", "--end", f"{QUERY_DATE}T12:00",
            "--min-minutes", "30",
        )

    def test_results_consistent_after_reopen(self):
        """同一数据在独立进程重开后，带筛选的查询结果一致。"""
        resource_id = self.prepare_acceptance_resource()

        first = self.free_query(
            resource_id, f"{QUERY_DATE}T09:30", f"{QUERY_DATE}T12:00", min_minutes="31"
        )
        # 每条命令本来就是独立进程；再查一次验证重开后结果一致。
        second = self.free_query(
            resource_id, f"{QUERY_DATE}T09:30", f"{QUERY_DATE}T12:00", min_minutes="31"
        )

        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
