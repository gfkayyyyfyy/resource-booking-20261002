"""day-query 按日查询的回归测试（仅标准库，python -m unittest 可发现）。

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


class DayQueryTestCase(unittest.TestCase):
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
        self.assertIsInstance(payload["resource_id"], int)
        self.assertGreater(payload["resource_id"], 0)
        self.assertEqual(payload["name"], name)
        return payload["resource_id"]

    def reserve(self, resource_id, start, end):
        payload = self.run_ok(
            "reserve", "--resource", str(resource_id), "--start", start, "--end", end
        )
        self.assertEqual(
            payload,
            {
                "booking_id": payload["booking_id"],
                "resource_id": resource_id,
                "start": start,
                "end": end,
            },
        )
        return payload["booking_id"]

    def cancel(self, booking_id):
        payload = self.run_ok("cancel", "--booking", str(booking_id))
        self.assertEqual(payload, {"booking_id": booking_id, "cancelled": True})

    def day_query(self, resource_id, date=QUERY_DATE):
        return self.run_ok("day-query", "--resource", str(resource_id), "--date", date)

    # ---- 成功路径 ----

    def test_cross_day_bookings_sorted_untruncated_and_listed_once(self):
        """前一晚跨入、当天普通、深夜跨出三类预约：按 start 升序、完整起止、各出现一次。"""
        resource_id = self.add_resource()
        # 创建顺序刻意与开始时间顺序不同。
        late_id = self.reserve(resource_id, f"{QUERY_DATE}T23:30", f"{NEXT_DATE}T01:00")
        early_id = self.reserve(resource_id, f"{PREV_DATE}T22:00", f"{QUERY_DATE}T01:30")
        noon_id = self.reserve(resource_id, f"{QUERY_DATE}T10:00", f"{QUERY_DATE}T11:00")

        payload = self.day_query(resource_id)

        self.assertEqual(payload["resource_id"], resource_id)
        self.assertEqual(payload["date"], QUERY_DATE)
        self.assertEqual(
            payload["bookings"],
            [
                {"booking_id": early_id, "start": f"{PREV_DATE}T22:00", "end": f"{QUERY_DATE}T01:30"},
                {"booking_id": noon_id, "start": f"{QUERY_DATE}T10:00", "end": f"{QUERY_DATE}T11:00"},
                {"booking_id": late_id, "start": f"{QUERY_DATE}T23:30", "end": f"{NEXT_DATE}T01:00"},
            ],
        )
        # 跨日记录只出现一次，且保留完整原始起止时间（不截断到查询日边界）。
        booking_ids = [b["booking_id"] for b in payload["bookings"]]
        self.assertEqual(len(booking_ids), len(set(booking_ids)))
        self.assertEqual(payload["bookings"][0]["start"], f"{PREV_DATE}T22:00")
        self.assertEqual(payload["bookings"][-1]["end"], f"{NEXT_DATE}T01:00")

    def test_bookings_touching_day_boundaries_are_excluded(self):
        """结束于当天 00:00、开始于次日 00:00 的预约不属于该日（左闭右开）。"""
        resource_id = self.add_resource()
        self.reserve(resource_id, f"{PREV_DATE}T22:00", f"{QUERY_DATE}T00:00")
        self.reserve(resource_id, f"{NEXT_DATE}T00:00", f"{NEXT_DATE}T02:00")

        payload = self.day_query(resource_id)

        self.assertEqual(
            payload,
            {"resource_id": resource_id, "date": QUERY_DATE, "bookings": []},
        )

    def test_cancelled_and_other_resource_bookings_are_excluded(self):
        """同资源已取消的预约、其他资源同时段的预约都不出现在结果中。"""
        resource_id = self.add_resource("目标资源")
        other_id = self.add_resource("其他资源")
        cancelled_id = self.reserve(resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")
        kept_id = self.reserve(resource_id, f"{QUERY_DATE}T10:00", f"{QUERY_DATE}T11:00")
        self.reserve(other_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")
        self.cancel(cancelled_id)

        payload = self.day_query(resource_id)

        self.assertEqual(
            payload["bookings"],
            [{"booking_id": kept_id, "start": f"{QUERY_DATE}T10:00", "end": f"{QUERY_DATE}T11:00"}],
        )

    def test_resource_without_matching_bookings_returns_empty_list(self):
        """资源存在但当天没有匹配预约：返回资源标识、原查询日期和空 bookings。"""
        resource_id = self.add_resource()
        self.reserve(resource_id, f"{PREV_DATE}T09:00", f"{PREV_DATE}T10:00")

        payload = self.day_query(resource_id)

        self.assertEqual(
            payload,
            {"resource_id": resource_id, "date": QUERY_DATE, "bookings": []},
        )

    def test_repeated_queries_are_identical_and_do_not_consume_booking_ids(self):
        """重复查询结果一致；查询为只读，不消耗预约标识。"""
        resource_id = self.add_resource()
        first_id = self.reserve(resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")

        first = self.day_query(resource_id)
        second = self.day_query(resource_id)

        self.assertEqual(first, second)
        self.assertEqual([b["booking_id"] for b in first["bookings"]], [first_id])
        # 查询之后新建的预约获得紧随其后的标识，说明查询没有消耗任何 id。
        next_id = self.reserve(resource_id, f"{QUERY_DATE}T10:00", f"{QUERY_DATE}T11:00")
        self.assertEqual(next_id, first_id + 1)

    def test_cancellation_remains_effective_after_reopening_database(self):
        """取消后重新打开同一数据库（新进程）查询，仍排除该记录。"""
        resource_id = self.add_resource()
        cancelled_id = self.reserve(resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")
        kept_id = self.reserve(resource_id, f"{QUERY_DATE}T10:00", f"{QUERY_DATE}T11:00")
        self.cancel(cancelled_id)

        # 每条命令都是独立进程，本次查询即“重新打开同一数据库”后的读取。
        payload = self.day_query(resource_id)

        self.assertEqual(
            payload["bookings"],
            [{"booking_id": kept_id, "start": f"{QUERY_DATE}T10:00", "end": f"{QUERY_DATE}T11:00"}],
        )

    # ---- 失败路径 ----

    def test_unknown_positive_resource_id_returns_resource_not_found(self):
        """正整数但资源标识不存在：resource_not_found，退出码 2。"""
        resource_id = self.add_resource()
        self.run_error(
            "resource_not_found",
            "day-query", "--resource", str(resource_id + 100), "--date", QUERY_DATE,
        )

    def test_invalid_inputs_return_invalid_input(self):
        """资源标识为零/负数、缺少参数、日期未零填充或为 2026-02-30：invalid_input。"""
        cases = [
            ("resource id 为零", ["day-query", "--resource", "0", "--date", QUERY_DATE]),
            ("resource id 为负数", ["day-query", "--resource", "-3", "--date", QUERY_DATE]),
            ("缺少 --date", ["day-query", "--resource", "1"]),
            ("缺少 --resource", ["day-query", "--date", QUERY_DATE]),
            ("日期未零填充", ["day-query", "--resource", "1", "--date", "2026-10-5"]),
            ("不存在的日期 2026-02-30", ["day-query", "--resource", "1", "--date", "2026-02-30"]),
        ]
        for label, args in cases:
            with self.subTest(label=label):
                self.run_error("invalid_input", *args)

    def test_invalid_input_takes_priority_over_resource_not_found(self):
        """非法日期与不存在的资源同时出现时，优先报告 invalid_input。"""
        self.run_error(
            "invalid_input",
            "day-query", "--resource", "999", "--date", "2026-02-30",
        )

    def test_invalid_input_does_not_create_database_file(self):
        """非法输入指向尚不存在的数据库路径时，不得创建文件。"""
        missing_db = Path(self._tmpdir.name) / "should-not-exist.sqlite"
        self.run_error(
            "invalid_input",
            "day-query", "--resource", "1", "--date", "2026-02-30",
            db=missing_db,
        )
        self.assertFalse(missing_db.exists())


if __name__ == "__main__":
    unittest.main()
