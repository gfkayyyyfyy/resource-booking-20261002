"""free-query --first-only 只返回最早达标空闲区间的回归测试（仅标准库）。

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
            args.append("--first-only")
        return self.run_ok(*args)

    def prepare_acceptance_resource(self):
        """验收场景数据：09:00-10:00 与 10:30-11:00 两条预约。"""
        resource_id = self.add_resource()
        self.reserve(resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")
        self.reserve(resource_id, f"{QUERY_DATE}T10:30", f"{QUERY_DATE}T11:00")
        return resource_id

    # ---- 验收场景 ----

    def test_acceptance_first_only_min_30_returns_earliest_slot(self):
        """验收：开关 + 30 分钟只返回 10:00-10:30（端点为完整日期时间）。"""
        resource_id = self.prepare_acceptance_resource()

        payload = self.free_query(
            resource_id,
            f"{QUERY_DATE}T09:30",
            f"{QUERY_DATE}T12:00",
            min_minutes="30",
            first_only=True,
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

    def test_acceptance_first_only_min_31_skips_short_earliest_slot(self):
        """验收：最早的 10:00-10:30 不足 31 分钟，继续取后续达标的 11:00-12:00。"""
        resource_id = self.prepare_acceptance_resource()

        payload = self.free_query(
            resource_id,
            f"{QUERY_DATE}T09:30",
            f"{QUERY_DATE}T12:00",
            min_minutes="31",
            first_only=True,
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

    def test_first_only_without_min_minutes_returns_earliest_maximal_slot(self):
        """不开时长筛选时，直接返回开始最早的最大空闲区间。"""
        resource_id = self.prepare_acceptance_resource()

        payload = self.free_query(
            resource_id,
            f"{QUERY_DATE}T09:30",
            f"{QUERY_DATE}T12:00",
            first_only=True,
        )

        self.assertEqual(
            payload["free_slots"],
            [{"start": f"{QUERY_DATE}T10:00", "end": f"{QUERY_DATE}T10:30"}],
        )

    def test_omitting_flag_preserves_existing_behavior(self):
        """省略开关时，即使同时给出 --min-minutes，也返回全部达标区间。"""
        resource_id = self.prepare_acceptance_resource()

        payload = self.free_query(
            resource_id, f"{QUERY_DATE}T09:30", f"{QUERY_DATE}T12:00", min_minutes="30"
        )

        self.assertEqual(
            payload["free_slots"],
            [
                {"start": f"{QUERY_DATE}T10:00", "end": f"{QUERY_DATE}T10:30"},
                {"start": f"{QUERY_DATE}T11:00", "end": f"{QUERY_DATE}T12:00"},
            ],
        )

    def test_exact_duration_boundary_still_selected(self):
        """时长恰好等于阈值的最早区间可以入选（>= 边界）。"""
        resource_id = self.add_resource()
        self.reserve(resource_id, f"{QUERY_DATE}T09:30", f"{QUERY_DATE}T10:00")
        self.reserve(resource_id, f"{QUERY_DATE}T10:30", f"{QUERY_DATE}T11:00")

        payload = self.free_query(
            resource_id,
            f"{QUERY_DATE}T09:00",
            f"{QUERY_DATE}T12:00",
            min_minutes="30",
            first_only=True,
        )

        self.assertEqual(
            payload["free_slots"],
            [{"start": f"{QUERY_DATE}T09:00", "end": f"{QUERY_DATE}T09:30"}],
        )

    def test_selected_slot_keeps_full_endpoints(self):
        """选中的区间保留完整端点，不截成指定时长，也不自动创建预约。"""
        resource_id = self.add_resource()
        self.reserve(resource_id, f"{QUERY_DATE}T11:00", f"{QUERY_DATE}T12:00")

        payload = self.free_query(
            resource_id,
            f"{QUERY_DATE}T09:00",
            f"{QUERY_DATE}T12:00",
            min_minutes="30",
            first_only=True,
        )

        # 09:00-11:00 为 120 分钟，整体返回而非截成 30 分钟。
        self.assertEqual(
            payload["free_slots"],
            [{"start": f"{QUERY_DATE}T09:00", "end": f"{QUERY_DATE}T11:00"}],
        )

    def test_no_qualifying_slot_returns_empty_array_with_success(self):
        """资源存在但没有达标区间：free_slots 为空数组，退出码 0。"""
        resource_id = self.prepare_acceptance_resource()

        payload = self.free_query(
            resource_id,
            f"{QUERY_DATE}T09:30",
            f"{QUERY_DATE}T12:00",
            min_minutes="61",
            first_only=True,
        )

        self.assertEqual(payload["free_slots"], [])

    def test_fully_busy_window_returns_empty_array(self):
        """窗口完全被占用且无零长度空闲项时，开关下仍是空数组。"""
        resource_id = self.add_resource()
        self.reserve(resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")
        self.reserve(resource_id, f"{QUERY_DATE}T10:00", f"{QUERY_DATE}T11:00")

        payload = self.free_query(
            resource_id,
            f"{QUERY_DATE}T09:00",
            f"{QUERY_DATE}T11:00",
            first_only=True,
        )

        self.assertEqual(payload["free_slots"], [])

    def test_no_bookings_returns_whole_window_as_single_slot(self):
        """无占用时整个窗口作为唯一最大空闲区间返回。"""
        resource_id = self.add_resource()

        payload = self.free_query(
            resource_id,
            f"{QUERY_DATE}T09:00",
            f"{QUERY_DATE}T10:00",
            first_only=True,
        )

        self.assertEqual(
            payload["free_slots"],
            [{"start": f"{QUERY_DATE}T09:00", "end": f"{QUERY_DATE}T10:00"}],
        )

    def test_cross_midnight_earliest_qualified_slot(self):
        """跨午夜场景下先按时长筛选再取最早一项；最早区间过短时取后续。"""
        resource_id = self.add_resource()
        # 窗口 23:30 至次日 03:00，占用 00:00-00:30：
        # 空闲为 23:30-00:00（30 分钟，跨午夜）与 00:30-03:00（150 分钟）。
        self.reserve(resource_id, f"{QUERY_DATE}T00:00", f"{QUERY_DATE}T00:30")

        at_limit = self.free_query(
            resource_id,
            f"{PREV_DATE}T23:30",
            f"{QUERY_DATE}T03:00",
            min_minutes="30",
            first_only=True,
        )
        over_limit = self.free_query(
            resource_id,
            f"{PREV_DATE}T23:30",
            f"{QUERY_DATE}T03:00",
            min_minutes="31",
            first_only=True,
        )

        self.assertEqual(
            at_limit["free_slots"],
            [{"start": f"{PREV_DATE}T23:30", "end": f"{QUERY_DATE}T00:00"}],
        )
        # 最早的 30 分钟区间被滤掉后，继续取后续最早达标的 00:30-03:00。
        self.assertEqual(
            over_limit["free_slots"],
            [{"start": f"{QUERY_DATE}T00:30", "end": f"{QUERY_DATE}T03:00"}],
        )

    def test_flag_accepted_before_other_options(self):
        """开关位置不影响结果（放在子命令后、其他选项前）。"""
        resource_id = self.prepare_acceptance_resource()

        payload = self.run_ok(
            "free-query",
            "--first-only",
            "--resource",
            str(resource_id),
            "--start",
            f"{QUERY_DATE}T09:30",
            "--end",
            f"{QUERY_DATE}T12:00",
            "--min-minutes",
            "31",
        )

        self.assertEqual(
            payload["free_slots"],
            [{"start": f"{QUERY_DATE}T11:00", "end": f"{QUERY_DATE}T12:00"}],
        )

    def test_query_is_read_only_and_consistent_after_reopen(self):
        """开关查询只读：重复结果一致，不消耗标识，重开同一数据库后一致。"""
        resource_id = self.prepare_acceptance_resource()

        args = (
            "free-query", "--resource", str(resource_id),
            "--start", f"{QUERY_DATE}T09:30", "--end", f"{QUERY_DATE}T12:00",
            "--min-minutes", "31", "--first-only",
        )
        first = self.run_ok(*args)
        second = self.run_ok(*args)
        self.assertEqual(first, second)

        next_id = self.reserve(resource_id, f"{QUERY_DATE}T13:00", f"{QUERY_DATE}T14:00")
        self.assertEqual(next_id, 3)  # 前两条预约标识为 1、2，查询未消耗标识。

        third = self.run_ok(*args)
        self.assertEqual(third, second)

    # ---- 失败路径：开关与其他校验 ----

    def test_flag_with_bare_value_returns_invalid_input(self):
        """--first-only 是无值开关：后面跟位置文本一律 invalid_input。"""
        self.run_error(
            "invalid_input",
            "free-query", "--resource", "1",
            "--start", f"{QUERY_DATE}T09:30", "--end", f"{QUERY_DATE}T12:00",
            "--first-only", "30",
        )

    def test_flag_with_explicit_value_returns_invalid_input(self):
        """--first-only=true 之类显式赋值不被接受：invalid_input。"""
        for attached in ("--first-only=true", "--first-only=1", "--first-only="):
            with self.subTest(attached=attached):
                self.run_error(
                    "invalid_input",
                    "free-query", "--resource", "1",
                    "--start", f"{QUERY_DATE}T09:30", "--end", f"{QUERY_DATE}T12:00",
                    attached,
                )

    def test_invalid_inputs_with_flag_return_invalid_input(self):
        """缺必填参数、非法标识、非法时间、非法时长 + 开关：统一 invalid_input。"""
        base = [
            f"{QUERY_DATE}T09:30", f"{QUERY_DATE}T12:00",
        ]
        cases = [
            ("缺少 --end",
             ["free-query", "--resource", "1",
              "--start", f"{QUERY_DATE}T09:30", "--first-only"]),
            ("非法标识",
             ["free-query", "--resource", "0",
              "--start", base[0], "--end", base[1], "--first-only"]),
            ("非法时间",
             ["free-query", "--resource", "1",
              "--start", "2026-10-05T09:61", "--end", base[1], "--first-only"]),
            ("起止相等",
             ["free-query", "--resource", "1",
              "--start", base[0], "--end", base[0], "--first-only"]),
            ("非法时长",
             ["free-query", "--resource", "1",
              "--start", base[0], "--end", base[1],
              "--first-only", "--min-minutes", "0"]),
        ]
        for label, args in cases:
            with self.subTest(label=label):
                self.run_error("invalid_input", *args)

    def test_invalid_input_with_flag_does_not_create_database_file(self):
        """携带开关的非法输入指向尚不存在的数据库路径时，不得创建文件。"""
        missing_db = Path(self._tmpdir.name) / "should-not-exist.sqlite"
        self.run_error(
            "invalid_input",
            "free-query", "--resource", "0",
            "--start", f"{QUERY_DATE}T09:30", "--end", f"{QUERY_DATE}T12:00",
            "--first-only",
            db=missing_db,
        )
        self.assertFalse(missing_db.exists())

    def test_invalid_input_with_flag_does_not_migrate_legacy_database(self):
        """携带开关的非法输入不迁移旧库：表结构与数据保持不变。"""
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
            "--first-only", "--min-minutes", "abc",
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

    def test_flag_with_unknown_resource_returns_resource_not_found(self):
        """输入全部合法（含开关）而资源不存在：resource_not_found，退出码 2。"""
        self.add_resource()
        self.run_error(
            "resource_not_found",
            "free-query", "--resource", "999",
            "--start", f"{QUERY_DATE}T09:30", "--end", f"{QUERY_DATE}T12:00",
            "--first-only",
        )

    def test_flag_with_oversized_resource_returns_resource_not_found(self):
        """开关与超大正整数标识同时给出：resource_not_found。"""
        self.add_resource()
        self.run_error(
            "resource_not_found",
            "free-query", "--resource", "9223372036854775808",
            "--start", f"{QUERY_DATE}T09:30", "--end", f"{QUERY_DATE}T12:00",
            "--first-only",
        )

    def test_cancelled_and_other_resources_do_not_affect_selection(self):
        """只统计同一资源的未取消预约：取消后与他资源占用不影响最早选择。"""
        resource_id = self.add_resource()
        other = self.add_resource("其他资源")
        blocking = self.reserve(
            resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00"
        )
        # 他资源在同一时段有预约，不应算作 resource_id 的占用。
        self.reserve(other, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T11:00")

        busy = self.free_query(
            resource_id,
            f"{QUERY_DATE}T09:00",
            f"{QUERY_DATE}T12:00",
            first_only=True,
        )
        self.assertEqual(
            busy["free_slots"],
            [{"start": f"{QUERY_DATE}T10:00", "end": f"{QUERY_DATE}T12:00"}],
        )

        self.run_ok("cancel", "--booking", str(blocking))
        freed = self.free_query(
            resource_id,
            f"{QUERY_DATE}T09:00",
            f"{QUERY_DATE}T12:00",
            first_only=True,
        )
        self.assertEqual(
            freed["free_slots"],
            [{"start": f"{QUERY_DATE}T09:00", "end": f"{QUERY_DATE}T12:00"}],
        )


if __name__ == "__main__":
    unittest.main()
