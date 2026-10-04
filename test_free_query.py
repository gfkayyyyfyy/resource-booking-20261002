"""free-query 空闲时段查询的回归测试（仅标准库，python -m unittest 可发现）。

通过公开命令行入口 `python -m booking --db <文件> <命令>` 准备数据并断言，
每个用例使用独立的临时 SQLite 数据库，结束后自动清理；
不依赖已有数据库、当前日期、机器时区或任何第三方库。
断言均基于解析后的 JSON 内容，不依赖输出对象的键顺序。
"""

import json
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


class FreeQueryTestCase(unittest.TestCase):
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
        """运行命令并断言退出码 2、输出为指定的错误对象。"""
        proc = self.run_cli(*args, db=db)
        self.assertEqual(proc.returncode, 2, msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        self.assertEqual(json.loads(proc.stdout), {"error": expected_error})
        return proc

    def add_resource(self, name="测试资源"):
        payload = self.run_ok("resource-add", "--name", name)
        return payload["resource_id"]

    def reserve(self, resource_id, start, end):
        payload = self.run_ok(
            "reserve", "--resource", str(resource_id), "--start", start, "--end", end
        )
        return payload["booking_id"]

    def cancel(self, booking_id):
        payload = self.run_ok("cancel", "--booking", str(booking_id))
        self.assertEqual(payload, {"booking_id": booking_id, "cancelled": True})

    def free_query(self, resource_id, start, end):
        return self.run_ok(
            "free-query", "--resource", str(resource_id), "--start", start, "--end", end
        )

    # ---- 成功路径 ----

    def test_window_split_by_two_bookings(self):
        """验收场景：09:00-10:00 与 10:30-11:00 两条预约，查询 09:30-11:30。"""
        resource_id = self.add_resource()
        self.reserve(resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")
        self.reserve(resource_id, f"{QUERY_DATE}T10:30", f"{QUERY_DATE}T11:00")

        payload = self.free_query(resource_id, f"{QUERY_DATE}T09:30", f"{QUERY_DATE}T11:30")

        self.assertEqual(
            payload,
            {
                "resource_id": resource_id,
                "start": f"{QUERY_DATE}T09:30",
                "end": f"{QUERY_DATE}T11:30",
                "free_slots": [
                    {"start": f"{QUERY_DATE}T10:00", "end": f"{QUERY_DATE}T10:30"},
                    {"start": f"{QUERY_DATE}T11:00", "end": f"{QUERY_DATE}T11:30"},
                ],
            },
        )

    def test_fully_booked_window_returns_empty_list(self):
        """验收场景：查询 09:15-09:45 被 09:00-10:00 完全覆盖，返回空数组。"""
        resource_id = self.add_resource()
        self.reserve(resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")
        self.reserve(resource_id, f"{QUERY_DATE}T10:30", f"{QUERY_DATE}T11:00")

        payload = self.free_query(resource_id, f"{QUERY_DATE}T09:15", f"{QUERY_DATE}T09:45")

        self.assertEqual(
            payload,
            {
                "resource_id": resource_id,
                "start": f"{QUERY_DATE}T09:15",
                "end": f"{QUERY_DATE}T09:45",
                "free_slots": [],
            },
        )

    def test_no_bookings_returns_whole_window(self):
        """窗口内没有任何占用：返回整个窗口，端点保留完整日期时间。"""
        resource_id = self.add_resource()

        payload = self.free_query(resource_id, f"{QUERY_DATE}T09:00", f"{NEXT_DATE}T09:00")

        self.assertEqual(
            payload["free_slots"],
            [{"start": f"{QUERY_DATE}T09:00", "end": f"{NEXT_DATE}T09:00"}],
        )

    def test_bookings_touching_window_edges_are_ignored(self):
        """结束于窗口起点、开始于窗口终点的预约不影响结果（左闭右开）。"""
        resource_id = self.add_resource()
        self.reserve(resource_id, f"{QUERY_DATE}T08:00", f"{QUERY_DATE}T09:00")
        self.reserve(resource_id, f"{QUERY_DATE}T11:00", f"{QUERY_DATE}T12:00")

        payload = self.free_query(resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T11:00")

        self.assertEqual(
            payload["free_slots"],
            [{"start": f"{QUERY_DATE}T09:00", "end": f"{QUERY_DATE}T11:00"}],
        )

    def test_adjacent_bookings_produce_no_zero_length_slot(self):
        """端点相接的两条预约之间不产生空区间，只保留非空项。"""
        resource_id = self.add_resource()
        self.reserve(resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")
        self.reserve(resource_id, f"{QUERY_DATE}T10:00", f"{QUERY_DATE}T11:00")

        payload = self.free_query(resource_id, f"{QUERY_DATE}T08:00", f"{QUERY_DATE}T12:00")

        self.assertEqual(
            payload["free_slots"],
            [
                {"start": f"{QUERY_DATE}T08:00", "end": f"{QUERY_DATE}T09:00"},
                {"start": f"{QUERY_DATE}T11:00", "end": f"{QUERY_DATE}T12:00"},
            ],
        )

    def test_cross_boundary_bookings_are_clipped_to_window(self):
        """越过查询边界的预约只按相交部分截断；跨日窗口同样适用。"""
        resource_id = self.add_resource()
        self.reserve(resource_id, f"{PREV_DATE}T22:00", f"{QUERY_DATE}T01:30")
        self.reserve(resource_id, f"{QUERY_DATE}T23:30", f"{NEXT_DATE}T02:00")

        payload = self.free_query(resource_id, f"{QUERY_DATE}T00:00", f"{NEXT_DATE}T00:00")

        self.assertEqual(
            payload["free_slots"],
            [
                {"start": f"{QUERY_DATE}T01:30", "end": f"{QUERY_DATE}T23:30"},
            ],
        )

    def test_cancelled_and_other_resource_bookings_are_ignored(self):
        """已取消的预约与其他资源的预约不构成占用。"""
        resource_id = self.add_resource("目标资源")
        other_id = self.add_resource("其他资源")
        cancelled_id = self.reserve(resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")
        self.reserve(other_id, f"{QUERY_DATE}T10:00", f"{QUERY_DATE}T11:00")
        self.cancel(cancelled_id)

        payload = self.free_query(resource_id, f"{QUERY_DATE}T08:00", f"{QUERY_DATE}T12:00")

        self.assertEqual(
            payload["free_slots"],
            [{"start": f"{QUERY_DATE}T08:00", "end": f"{QUERY_DATE}T12:00"}],
        )

    def test_bookings_outside_window_are_ignored(self):
        """完全在窗口之外的预约不影响结果。"""
        resource_id = self.add_resource()
        self.reserve(resource_id, f"{PREV_DATE}T09:00", f"{PREV_DATE}T10:00")
        self.reserve(resource_id, f"{NEXT_DATE}T09:00", f"{NEXT_DATE}T10:00")

        payload = self.free_query(resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")

        self.assertEqual(
            payload["free_slots"],
            [{"start": f"{QUERY_DATE}T09:00", "end": f"{QUERY_DATE}T10:00"}],
        )

    def test_repeated_queries_are_identical_and_do_not_consume_ids(self):
        """重复查询结果一致；查询为只读，不消耗预约标识。"""
        resource_id = self.add_resource()
        first_id = self.reserve(resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")

        first = self.free_query(resource_id, f"{QUERY_DATE}T08:00", f"{QUERY_DATE}T12:00")
        second = self.free_query(resource_id, f"{QUERY_DATE}T08:00", f"{QUERY_DATE}T12:00")

        self.assertEqual(first, second)
        # 查询之后新建的预约获得紧随其后的标识，说明查询没有消耗任何 id。
        next_id = self.reserve(resource_id, f"{QUERY_DATE}T10:00", f"{QUERY_DATE}T11:00")
        self.assertEqual(next_id, first_id + 1)

    # ---- 失败路径 ----

    def test_unknown_positive_resource_id_returns_resource_not_found(self):
        """正整数但资源标识不存在：resource_not_found，退出码 2。"""
        resource_id = self.add_resource()
        self.run_error(
            "resource_not_found",
            "free-query", "--resource", str(resource_id + 100),
            "--start", f"{QUERY_DATE}T09:00", "--end", f"{QUERY_DATE}T10:00",
        )

    def test_oversized_resource_id_returns_resource_not_found(self):
        """超过 SQLite 整数上限的正整数标识：resource_not_found，退出码 2。"""
        self.add_resource()
        self.run_error(
            "resource_not_found",
            "free-query", "--resource", "9223372036854775808",
            "--start", f"{QUERY_DATE}T09:00", "--end", f"{QUERY_DATE}T10:00",
        )

    def test_invalid_inputs_return_invalid_input(self):
        """标识非法、缺参、时间格式错误、起止顺序错误：invalid_input。"""
        cases = [
            ("resource id 为零", ["free-query", "--resource", "0",
                                  "--start", f"{QUERY_DATE}T09:00", "--end", f"{QUERY_DATE}T10:00"]),
            ("resource id 为负数", ["free-query", "--resource", "-3",
                                    "--start", f"{QUERY_DATE}T09:00", "--end", f"{QUERY_DATE}T10:00"]),
            ("resource id 含尾随换行", ["free-query", "--resource", "1\n",
                                        "--start", f"{QUERY_DATE}T09:00", "--end", f"{QUERY_DATE}T10:00"]),
            ("缺少 --start", ["free-query", "--resource", "1", "--end", f"{QUERY_DATE}T10:00"]),
            ("缺少 --end", ["free-query", "--resource", "1", "--start", f"{QUERY_DATE}T09:00"]),
            ("缺少 --resource", ["free-query", "--start", f"{QUERY_DATE}T09:00", "--end", f"{QUERY_DATE}T10:00"]),
            ("多余参数", ["free-query", "--resource", "1",
                          "--start", f"{QUERY_DATE}T09:00", "--end", f"{QUERY_DATE}T10:00",
                          "--date", QUERY_DATE]),
            ("时间未零填充", ["free-query", "--resource", "1",
                              "--start", "2026-10-5T09:00", "--end", f"{QUERY_DATE}T10:00"]),
            ("时间带秒", ["free-query", "--resource", "1",
                          "--start", f"{QUERY_DATE}T09:00:00", "--end", f"{QUERY_DATE}T10:00"]),
            ("不存在的日期 2026-02-30", ["free-query", "--resource", "1",
                                         "--start", "2026-02-30T09:00", "--end", f"{QUERY_DATE}T10:00"]),
            ("起止相等", ["free-query", "--resource", "1",
                          "--start", f"{QUERY_DATE}T09:00", "--end", f"{QUERY_DATE}T09:00"]),
            ("起止颠倒", ["free-query", "--resource", "1",
                          "--start", f"{QUERY_DATE}T10:00", "--end", f"{QUERY_DATE}T09:00"]),
        ]
        for label, args in cases:
            with self.subTest(label=label):
                self.run_error("invalid_input", *args)

    def test_invalid_input_takes_priority_over_resource_not_found(self):
        """非法时间与不存在的资源同时出现时，优先报告 invalid_input。"""
        self.run_error(
            "invalid_input",
            "free-query", "--resource", "999",
            "--start", "2026-02-30T09:00", "--end", f"{QUERY_DATE}T10:00",
        )

    def test_invalid_input_does_not_create_database_file(self):
        """非法输入指向尚不存在的数据库路径时，不得创建文件。"""
        missing_db = Path(self._tmpdir.name) / "should-not-exist.sqlite"
        self.run_error(
            "invalid_input",
            "free-query", "--resource", "1",
            "--start", f"{QUERY_DATE}T10:00", "--end", f"{QUERY_DATE}T09:00",
            db=missing_db,
        )
        self.assertFalse(missing_db.exists())


if __name__ == "__main__":
    unittest.main()
