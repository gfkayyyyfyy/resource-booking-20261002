"""day-query 按日查询的回归测试（仅使用 Python 标准库）。

通过公开入口 ``python -m booking`` 准备数据并发起查询，固定查询
UTC+08:00 下的 2026-10-05。每个用例使用独立的临时 SQLite 数据库，
结束后自动清理；不依赖已有数据库、当前日期或机器时区（子进程统一
注入与 UTC+08:00 不同的 TZ，验证结果不受运行环境时区影响）。

执行方式（在项目根目录）：

    python -m unittest -v          # 发现并运行全部测试
    python -m unittest test_day_query -v
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

# 固定查询日期（UTC+08:00 本地时间）。
QUERY_DATE = "2026-10-05"

# 与 UTC+08:00 不同的 POSIX 时区（UTC-05:00，无需 tzdata），
# 用于证明查询结果不随机器时区变化。
TEST_ENV = dict(os.environ, TZ="EST5")


class DayQueryTestCase(unittest.TestCase):
    """每个用例独享一个临时目录中的全新 SQLite 数据库。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="booking-day-query-")
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "test.sqlite"

    # ------------------------------------------------------------------
    # CLI 辅助方法
    # ------------------------------------------------------------------

    def run_cli(self, *args):
        """运行 python -m booking，返回 CompletedProcess。"""
        return subprocess.run(
            [sys.executable, "-m", "booking", "--db", str(self.db_path), *args],
            cwd=PROJECT_ROOT,
            env=TEST_ENV,
            capture_output=True,
            text=True,
        )

    @staticmethod
    def parse_single_json_object(stdout):
        """标准输出必须恰好是一个 JSON 对象（按内容解析，不依赖键序）。"""
        payload = json.loads(stdout)  # 多余内容会导致解析失败
        if not isinstance(payload, dict):
            raise AssertionError("expected a single JSON object, got: %r" % stdout)
        return payload

    def run_ok(self, *args):
        """运行命令并断言退出码为 0、输出为单个 JSON 对象。"""
        result = self.run_cli(*args)
        self.assertEqual(
            result.returncode, 0,
            "expected exit 0, got %d; stderr: %s" % (result.returncode, result.stderr),
        )
        return self.parse_single_json_object(result.stdout)

    def run_error(self, *args):
        """运行命令并断言退出码为 2、输出为单个 JSON 错误对象。"""
        result = self.run_cli(*args)
        self.assertEqual(
            result.returncode, 2,
            "expected exit 2, got %d; stdout: %s" % (result.returncode, result.stdout),
        )
        return self.parse_single_json_object(result.stdout)

    def add_resource(self, name):
        return self.run_ok("resource-add", "--name", name)["resource_id"]

    def reserve(self, resource_id, start, end):
        return self.run_ok(
            "reserve", "--resource", str(resource_id),
            "--start", start, "--end", end,
        )

    def cancel(self, booking_id):
        return self.run_ok("cancel", "--booking", str(booking_id))

    def day_query(self, resource_id, date=QUERY_DATE):
        return self.run_ok(
            "day-query", "--resource", str(resource_id), "--date", date,
        )

    # ------------------------------------------------------------------
    # 成功路径
    # ------------------------------------------------------------------

    def test_cross_day_bookings_returned_sorted_and_untruncated(self):
        """跨日与普通预约按 start 升序返回，保留完整起止时间。

        创建顺序（普通 → 深夜跨日 → 前夜跨日）与开始时间顺序不同，
        查询结果仍按 start 升序；跨日记录只出现一次且不截断。
        """
        resource_id = self.add_resource("一号会议室")
        same_day = self.reserve(resource_id, "2026-10-05T09:00", "2026-10-05T10:00")
        late_night = self.reserve(resource_id, "2026-10-05T23:00", "2026-10-06T01:00")
        prev_night = self.reserve(resource_id, "2026-10-04T22:00", "2026-10-05T02:00")

        payload = self.day_query(resource_id)

        self.assertEqual(payload["resource_id"], resource_id)
        self.assertEqual(payload["date"], QUERY_DATE)
        self.assertEqual(
            payload["bookings"],
            [
                {
                    "booking_id": prev_night["booking_id"],
                    "start": "2026-10-04T22:00",
                    "end": "2026-10-05T02:00",
                },
                {
                    "booking_id": same_day["booking_id"],
                    "start": "2026-10-05T09:00",
                    "end": "2026-10-05T10:00",
                },
                {
                    "booking_id": late_night["booking_id"],
                    "start": "2026-10-05T23:00",
                    "end": "2026-10-06T01:00",
                },
            ],
        )
        # 跨日记录各出现且仅出现一次。
        ids = [b["booking_id"] for b in payload["bookings"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_boundary_touching_bookings_are_excluded(self):
        """结束于当天 00:00、开始于次日 00:00 的预约不属于该日（左闭右开）。"""
        resource_id = self.add_resource("边界会议室")
        ends_at_midnight = self.reserve(
            resource_id, "2026-10-04T23:00", "2026-10-05T00:00"
        )
        starts_next_midnight = self.reserve(
            resource_id, "2026-10-06T00:00", "2026-10-06T02:00"
        )
        inside = self.reserve(resource_id, "2026-10-05T10:00", "2026-10-05T11:00")

        payload = self.day_query(resource_id)

        self.assertEqual(
            payload["bookings"],
            [
                {
                    "booking_id": inside["booking_id"],
                    "start": "2026-10-05T10:00",
                    "end": "2026-10-05T11:00",
                }
            ],
        )
        excluded_ids = {
            ends_at_midnight["booking_id"],
            starts_next_midnight["booking_id"],
        }
        returned_ids = {b["booking_id"] for b in payload["bookings"]}
        self.assertTrue(excluded_ids.isdisjoint(returned_ids))

    def test_cancelled_and_other_resource_bookings_are_excluded(self):
        """已取消的预约、其他资源同时段的预约均不出现在结果中。"""
        resource_a = self.add_resource("会议室A")
        resource_b = self.add_resource("会议室B")
        cancelled = self.reserve(resource_a, "2026-10-05T09:00", "2026-10-05T10:00")
        other_resource = self.reserve(
            resource_b, "2026-10-05T09:00", "2026-10-05T10:00"
        )
        kept = self.reserve(resource_a, "2026-10-05T14:00", "2026-10-05T15:00")
        self.cancel(cancelled["booking_id"])

        payload = self.day_query(resource_a)

        self.assertEqual(
            payload["bookings"],
            [
                {
                    "booking_id": kept["booking_id"],
                    "start": "2026-10-05T14:00",
                    "end": "2026-10-05T15:00",
                }
            ],
        )
        returned_ids = {b["booking_id"] for b in payload["bookings"]}
        self.assertNotIn(cancelled["booking_id"], returned_ids)
        self.assertNotIn(other_resource["booking_id"], returned_ids)

    def test_resource_without_matching_bookings_returns_empty_list(self):
        """资源存在但当天没有匹配预约：返回资源标识、原查询日期与空数组。"""
        resource_id = self.add_resource("空闲会议室")
        # 其他日期的预约不影响当天结果。
        self.reserve(resource_id, "2026-10-01T09:00", "2026-10-01T10:00")

        payload = self.day_query(resource_id)

        self.assertEqual(
            payload,
            {"resource_id": resource_id, "date": QUERY_DATE, "bookings": []},
        )

    def test_repeated_queries_are_consistent_and_do_not_consume_ids(self):
        """重复查询结果一致；查询为只读操作，不消耗预约标识。"""
        resource_id = self.add_resource("只读会议室")
        first = self.reserve(resource_id, "2026-10-05T09:00", "2026-10-05T10:00")

        first_result = self.day_query(resource_id)
        second_result = self.day_query(resource_id)
        self.assertEqual(first_result, second_result)

        # 两次查询之后再预约，booking_id 紧接上一条，未被查询消耗。
        second = self.reserve(resource_id, "2026-10-05T11:00", "2026-10-05T12:00")
        self.assertEqual(second["booking_id"], first["booking_id"] + 1)

    def test_cancelled_booking_stays_excluded_after_reopen(self):
        """取消结果持久化：重新打开同一数据库查询仍排除该记录。

        每条命令都是独立进程、重新打开数据库，因此取消后的每次查询
        都在验证持久化效果。
        """
        resource_id = self.add_resource("持久会议室")
        booking = self.reserve(resource_id, "2026-10-05T09:00", "2026-10-05T10:00")
        self.cancel(booking["booking_id"])

        for _ in range(2):
            payload = self.day_query(resource_id)
            self.assertEqual(payload["bookings"], [])

    # ------------------------------------------------------------------
    # 失败路径
    # ------------------------------------------------------------------

    def test_nonexistent_resource_returns_resource_not_found(self):
        """正整数资源标识不存在 → resource_not_found，退出码 2。"""
        self.add_resource("占位会议室")  # 已占用 id 1，999 必不存在
        payload = self.run_error(
            "day-query", "--resource", "999", "--date", QUERY_DATE
        )
        self.assertEqual(payload, {"error": "resource_not_found"})

    def test_invalid_input_variants(self):
        """资源标识为零/负数、缺少参数、日期未零填充或无效 → invalid_input。"""
        cases = [
            ("resource id 为零", ("day-query", "--resource", "0", "--date", QUERY_DATE)),
            ("resource id 为负", ("day-query", "--resource", "-1", "--date", QUERY_DATE)),
            ("缺少 --date", ("day-query", "--resource", "1")),
            ("缺少 --resource", ("day-query", "--date", QUERY_DATE)),
            ("日期未零填充", ("day-query", "--resource", "1", "--date", "2026-10-5")),
            ("月份未零填充", ("day-query", "--resource", "1", "--date", "2026-1-05")),
            ("无效日期 2026-02-30", ("day-query", "--resource", "1", "--date", "2026-02-30")),
        ]
        for label, argv in cases:
            with self.subTest(label=label):
                payload = self.run_error(*argv)
                self.assertEqual(payload, {"error": "invalid_input"})

    def test_invalid_input_takes_precedence_over_missing_resource(self):
        """非法日期与不存在的资源同时出现时，优先报告 invalid_input。"""
        self.add_resource("占位会议室")  # 999 必不存在
        payload = self.run_error(
            "day-query", "--resource", "999", "--date", "2026-02-30"
        )
        self.assertEqual(payload, {"error": "invalid_input"})

    def test_invalid_input_does_not_create_database_file(self):
        """非法输入指向尚不存在的数据库路径时，不得创建文件。"""
        self.assertFalse(self.db_path.exists())
        payload = self.run_error(
            "day-query", "--resource", "1", "--date", "2026-02-30"
        )
        self.assertEqual(payload, {"error": "invalid_input"})
        self.assertFalse(self.db_path.exists())


if __name__ == "__main__":
    unittest.main()
