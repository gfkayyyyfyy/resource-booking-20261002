"""free-query --first-only 最早空闲区间开关的回归测试（仅标准库，python -m unittest 可发现）。

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

# 固定的查询日期（固定 UTC+08:00 的本地日期，与运行环境无关）。
QUERY_DATE = "2026-10-05"


class FreeQueryFirstOnlyTestCase(unittest.TestCase):
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

    def free_query(self, resource_id, start, end, min_minutes=None, first_only=False):
        args = ["free-query", "--resource", str(resource_id), "--start", start, "--end", end]
        if min_minutes is not None:
            args += ["--min-minutes", min_minutes]
        if first_only:
            args += ["--first-only"]
        return self.run_ok(*args)

    def prepare_acceptance_resource(self):
        """验收场景数据：09:00-10:00 与 10:30-11:00 两条预约。"""
        resource_id = self.add_resource()
        self.reserve(resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")
        self.reserve(resource_id, f"{QUERY_DATE}T10:30", f"{QUERY_DATE}T11:00")
        return resource_id

    # ---- 验收场景与基本行为 ----

    def test_acceptance_min_30_returns_earliest_slot_only(self):
        """验收场景：09:30-12:00 窗口、--first-only 加 30 分钟，只返回 10:00-10:30。"""
        resource_id = self.prepare_acceptance_resource()

        payload = self.free_query(
            resource_id, f"{QUERY_DATE}T09:30", f"{QUERY_DATE}T12:00",
            min_minutes="30", first_only=True,
        )

        self.assertEqual(
            payload,
            {
                "resource_id": resource_id,
                "start": f"{QUERY_DATE}T09:30",
                "end": f"{QUERY_DATE}T12:00",
                "free_slots": [
                    {"start": f"{QUERY_DATE}T10:00", "end": f"{QUERY_DATE}T10:30"},
                ],
            },
        )

    def test_acceptance_min_31_skips_too_short_earliest_slot(self):
        """验收场景：--first-only 加 31 分钟，最早区间过短时取后续达标的 11:00-12:00。"""
        resource_id = self.prepare_acceptance_resource()

        payload = self.free_query(
            resource_id, f"{QUERY_DATE}T09:30", f"{QUERY_DATE}T12:00",
            min_minutes="31", first_only=True,
        )

        self.assertEqual(
            payload,
            {
                "resource_id": resource_id,
                "start": f"{QUERY_DATE}T09:30",
                "end": f"{QUERY_DATE}T12:00",
                "free_slots": [
                    {"start": f"{QUERY_DATE}T11:00", "end": f"{QUERY_DATE}T12:00"},
                ],
            },
        )

    def test_first_only_without_min_minutes_returns_earliest_slot(self):
        """只开 --first-only 不带时长筛选：直接返回最早的最大空闲区间。"""
        resource_id = self.prepare_acceptance_resource()

        payload = self.free_query(
            resource_id, f"{QUERY_DATE}T09:30", f"{QUERY_DATE}T12:00", first_only=True
        )

        self.assertEqual(
            payload["free_slots"],
            [{"start": f"{QUERY_DATE}T10:00", "end": f"{QUERY_DATE}T10:30"}],
        )

    def test_omitted_first_only_matches_unfiltered_query(self):
        """省略 --first-only 时返回内容与既有查询完全一致。"""
        resource_id = self.prepare_acceptance_resource()

        without = self.free_query(resource_id, f"{QUERY_DATE}T09:30", f"{QUERY_DATE}T12:00")
        without_with_min = self.free_query(
            resource_id, f"{QUERY_DATE}T09:30", f"{QUERY_DATE}T12:00", min_minutes="30"
        )

        self.assertEqual(
            without["free_slots"],
            [
                {"start": f"{QUERY_DATE}T10:00", "end": f"{QUERY_DATE}T10:30"},
                {"start": f"{QUERY_DATE}T11:00", "end": f"{QUERY_DATE}T12:00"},
            ],
        )
        self.assertEqual(without["free_slots"], without_with_min["free_slots"])

    def test_selected_slot_keeps_full_endpoints(self):
        """选中的区间保留完整起止端点，不截成指定时长。"""
        resource_id = self.add_resource()
        self.reserve(resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")

        payload = self.free_query(
            resource_id, f"{QUERY_DATE}T08:00", f"{QUERY_DATE}T12:00",
            min_minutes="30", first_only=True,
        )

        self.assertEqual(
            payload["free_slots"],
            [{"start": f"{QUERY_DATE}T08:00", "end": f"{QUERY_DATE}T09:00"}],
        )

    def test_exact_duration_boundary_is_inclusive(self):
        """持续分钟数恰好等于阈值的最早区间可以入选。"""
        resource_id = self.add_resource()
        self.reserve(resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T09:30")

        payload = self.free_query(
            resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00",
            min_minutes="30", first_only=True,
        )

        self.assertEqual(
            payload["free_slots"],
            [{"start": f"{QUERY_DATE}T09:30", "end": f"{QUERY_DATE}T10:00"}],
        )

    def test_no_qualifying_slot_returns_empty_array_with_success(self):
        """资源存在但没有达标区间：free_slots 为空数组，退出码 0。"""
        resource_id = self.prepare_acceptance_resource()

        payload = self.free_query(
            resource_id, f"{QUERY_DATE}T09:30", f"{QUERY_DATE}T12:00",
            min_minutes="61", first_only=True,
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

    def test_no_bookings_whole_window_returned_as_single_slot(self):
        """无占用时整个窗口即最早区间，只返回这一项。"""
        resource_id = self.add_resource()

        payload = self.free_query(
            resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T12:00", first_only=True
        )

        self.assertEqual(
            payload["free_slots"],
            [{"start": f"{QUERY_DATE}T09:00", "end": f"{QUERY_DATE}T12:00"}],
        )

    def test_fully_booked_window_returns_empty_array(self):
        """窗口完全占满时 free_slots 为空数组并成功退出。"""
        resource_id = self.add_resource()
        self.reserve(resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T12:00")

        payload = self.free_query(
            resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T12:00", first_only=True
        )

        self.assertEqual(payload["free_slots"], [])

    def test_first_only_does_not_create_booking(self):
        """查询为只读：重复结果一致，不消耗标识，不自动创建预约。"""
        resource_id = self.prepare_acceptance_resource()

        first = self.free_query(
            resource_id, f"{QUERY_DATE}T09:30", f"{QUERY_DATE}T12:00",
            min_minutes="30", first_only=True,
        )
        second = self.free_query(
            resource_id, f"{QUERY_DATE}T09:30", f"{QUERY_DATE}T12:00",
            min_minutes="30", first_only=True,
        )

        self.assertEqual(first, second)
        # 查询返回的 10:00-10:30 仍然空闲：可以预约成功，且标识未被消耗。
        next_id = self.reserve(resource_id, f"{QUERY_DATE}T10:00", f"{QUERY_DATE}T10:30")
        self.assertEqual(next_id, 3)  # 前两条预约标识为 1、2。

    def test_results_consistent_after_reopen(self):
        """同一数据在独立进程重开后，带开关的查询结果一致。"""
        resource_id = self.prepare_acceptance_resource()

        first = self.free_query(
            resource_id, f"{QUERY_DATE}T09:30", f"{QUERY_DATE}T12:00",
            min_minutes="31", first_only=True,
        )
        # 每条命令本来就是独立进程；再查一次验证重开后结果一致。
        second = self.free_query(
            resource_id, f"{QUERY_DATE}T09:30", f"{QUERY_DATE}T12:00",
            min_minutes="31", first_only=True,
        )

        self.assertEqual(first, second)

    # ---- 失败路径：开关附加值与缺失参数 ----

    def test_flag_with_attached_value_returns_invalid_input(self):
        """--first-only=<值> 形式：invalid_input，退出码 2 且 stderr 为空。"""
        for value in ("1", "true", "0"):
            with self.subTest(value=value):
                self.run_error(
                    "invalid_input",
                    "free-query", "--resource", "1",
                    "--start", f"{QUERY_DATE}T09:30", "--end", f"{QUERY_DATE}T12:00",
                    f"--first-only={value}",
                )

    def test_flag_with_following_value_returns_invalid_input(self):
        """--first-only 后跟随值文本（被当作多余参数）：invalid_input。"""
        self.run_error(
            "invalid_input",
            "free-query", "--resource", "1",
            "--start", f"{QUERY_DATE}T09:30", "--end", f"{QUERY_DATE}T12:00",
            "--first-only", "1",
        )

    def test_missing_required_arguments_return_invalid_input(self):
        """缺少必填参数（--resource/--start/--end 任一）：invalid_input。"""
        cases = [
            ("缺 --resource",
             ["free-query", "--start", f"{QUERY_DATE}T09:30",
              "--end", f"{QUERY_DATE}T12:00", "--first-only"]),
            ("缺 --start",
             ["free-query", "--resource", "1",
              "--end", f"{QUERY_DATE}T12:00", "--first-only"]),
            ("缺 --end",
             ["free-query", "--resource", "1",
              "--start", f"{QUERY_DATE}T09:30", "--first-only"]),
        ]
        for label, args in cases:
            with self.subTest(label=label):
                self.run_error("invalid_input", *args)

    def test_invalid_resource_id_returns_invalid_input(self):
        """非法标识（零、负数、非整数文本）与开关同现：invalid_input。"""
        for value in ("0", "-1", "abc", "1.5", " 1"):
            with self.subTest(value=value):
                self.run_error(
                    "invalid_input",
                    "free-query", "--resource", value,
                    "--start", f"{QUERY_DATE}T09:30", "--end", f"{QUERY_DATE}T12:00",
                    "--first-only",
                )

    def test_invalid_time_returns_invalid_input(self):
        """非法时间（格式错误、无效日期、起止颠倒）与开关同现：invalid_input。"""
        cases = [
            ("带秒的开始时间", f"{QUERY_DATE}T09:30:00", f"{QUERY_DATE}T12:00"),
            ("无效日期", "2026-02-30T09:30", f"{QUERY_DATE}T12:00"),
            ("起止相等", f"{QUERY_DATE}T09:30", f"{QUERY_DATE}T09:30"),
            ("起止颠倒", f"{QUERY_DATE}T12:00", f"{QUERY_DATE}T09:30"),
        ]
        for label, start, end in cases:
            with self.subTest(label=label):
                self.run_error(
                    "invalid_input",
                    "free-query", "--resource", "1",
                    "--start", start, "--end", end, "--first-only",
                )

    def test_invalid_min_minutes_with_flag_returns_invalid_input(self):
        """非法 --min-minutes 与开关同现：invalid_input。"""
        for value in ("0", "-30", "30.5", "1441", "abc", "30 "):
            with self.subTest(value=value):
                self.run_error(
                    "invalid_input",
                    "free-query", "--resource", "1",
                    "--start", f"{QUERY_DATE}T09:30", "--end", f"{QUERY_DATE}T12:00",
                    "--min-minutes", value, "--first-only",
                )

    def test_invalid_input_does_not_create_database_file(self):
        """非法输入指向尚不存在的数据库路径时，不得创建文件。"""
        missing_db = Path(self._tmpdir.name) / "should-not-exist.sqlite"
        self.run_error(
            "invalid_input",
            "free-query", "--resource", "1",
            "--start", f"{QUERY_DATE}T09:30", "--end", f"{QUERY_DATE}T12:00",
            "--first-only=1",
            db=missing_db,
        )
        self.assertFalse(missing_db.exists())

    def test_invalid_input_does_not_migrate_legacy_database(self):
        """非法输入不迁移旧库：表结构与数据保持不变。"""
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
            "--first-only", "extra",
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

    def test_unknown_resource_returns_resource_not_found(self):
        """输入全部合法而资源不存在：resource_not_found，退出码 2。"""
        self.add_resource()
        self.run_error(
            "resource_not_found",
            "free-query", "--resource", "999",
            "--start", f"{QUERY_DATE}T09:30", "--end", f"{QUERY_DATE}T12:00",
            "--first-only",
        )

    def test_oversized_resource_returns_resource_not_found(self):
        """合法开关与超大正整数标识：resource_not_found。"""
        self.add_resource()
        self.run_error(
            "resource_not_found",
            "free-query", "--resource", "9223372036854775808",
            "--start", f"{QUERY_DATE}T09:30", "--end", f"{QUERY_DATE}T12:00",
            "--first-only",
        )


if __name__ == "__main__":
    unittest.main()
