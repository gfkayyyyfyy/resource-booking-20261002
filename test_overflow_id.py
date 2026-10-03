"""超大正整数标识的回归测试（仅标准库，python -m unittest 可发现）。

通过公开命令行入口 `python -m booking --db <文件> <命令>` 准备数据并断言，
每个用例使用独立的临时 SQLite 数据库，结束后自动清理；
不依赖已有数据库、当前日期、机器时区或任何第三方库。
断言均基于解析后的 JSON 内容，不依赖输出对象的键顺序。

覆盖的公开行为：
- 大于 9223372036854775807 的正整数标识（含连续五千个 9）按“不存在”处理：
  reserve / day-query 返回 resource_not_found，cancel 返回 booking_not_found，
  退出码 2，标准输出仅为该 JSON 对象，标准错误无异常堆栈；
- 前导零不改变数值含义：0001 仍指向标识 1，0009223372036854775808 仍不存在；
- 超大标识与非法日期/时间或起止顺序错误同时出现时，优先返回 invalid_input；
- 越界请求不新增记录、不消耗资源或预约标识，已有预约的查询结果、
  冲突判断与可取消性在越界请求前后保持一致。
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

# 固定的预约日期与起止时间（固定 UTC+08:00 的本地时间，与运行环境无关）。
BOOKING_DATE = "2026-10-05"
START = f"{BOOKING_DATE}T09:00"
END = f"{BOOKING_DATE}T10:00"

# SQLite INTEGER 为 64 位有符号整数：刚好越界的最小值，以及远超范围的文本。
INT64_MAX = 9223372036854775807
OVERFLOW_ID = str(INT64_MAX + 1)  # "9223372036854775808"
HUGE_ID = "9" * 5000


class OverflowIdTestCase(unittest.TestCase):
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
        """运行命令并断言退出码 2、输出为指定的错误对象、标准错误无异常堆栈。"""
        proc = self.run_cli(*args, db=db)
        self.assertEqual(proc.returncode, 2, msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        self.assertEqual(json.loads(proc.stdout), {"error": expected_error})
        self.assertNotIn("Traceback", proc.stderr)
        return proc

    def add_resource(self, name="测试资源"):
        payload = self.run_ok("resource-add", "--name", name)
        self.assertIsInstance(payload["resource_id"], int)
        self.assertGreater(payload["resource_id"], 0)
        return payload["resource_id"]

    def reserve_ok(self, resource_id, start=START, end=END):
        """提交预约并断言成功，返回新的 booking_id。"""
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

    def day_query(self, resource_id, date=BOOKING_DATE):
        return self.run_ok("day-query", "--resource", str(resource_id), "--date", date)

    # ---- 越界标识按“不存在”处理 ----

    def test_overflow_resource_id_returns_resource_not_found(self):
        """9223372036854775808 作为资源标识：day-query 与 reserve 均返回 resource_not_found。"""
        resource_id = self.add_resource()
        self.reserve_ok(resource_id)

        for args in (
            ["day-query", "--resource", OVERFLOW_ID, "--date", BOOKING_DATE],
            ["reserve", "--resource", OVERFLOW_ID, "--start", START, "--end", END],
        ):
            with self.subTest(args=args):
                proc = self.run_error("resource_not_found", *args)
                self.assertEqual(proc.stderr, "")

    def test_overflow_booking_id_returns_booking_not_found(self):
        """9223372036854775808 作为预约标识：cancel 返回 booking_not_found。"""
        resource_id = self.add_resource()
        self.reserve_ok(resource_id)

        proc = self.run_error("booking_not_found", "cancel", "--booking", OVERFLOW_ID)
        self.assertEqual(proc.stderr, "")

    def test_arbitrarily_large_ids_return_not_found(self):
        """连续五千个数字 9 的标识：三条命令分别返回对应的“不存在”错误。"""
        resource_id = self.add_resource()
        self.reserve_ok(resource_id)

        cases = [
            ("day-query", "resource_not_found",
             ["day-query", "--resource", HUGE_ID, "--date", BOOKING_DATE]),
            ("reserve", "resource_not_found",
             ["reserve", "--resource", HUGE_ID, "--start", START, "--end", END]),
            ("cancel", "booking_not_found",
             ["cancel", "--booking", HUGE_ID]),
        ]
        for label, expected_error, args in cases:
            with self.subTest(command=label):
                proc = self.run_error(expected_error, *args)
                self.assertEqual(proc.stderr, "")

    # ---- 前导零不改变数值含义 ----

    def test_leading_zeros_resolve_to_same_existing_id(self):
        """0001 形式的前导零标识仍指向同一资源与同一预约。"""
        resource_id = self.add_resource()
        booking_id = self.reserve_ok(resource_id)
        padded_resource = "000" + str(resource_id)
        padded_booking = "000" + str(booking_id)

        # 前导零标识的查询结果与原始标识一致。
        self.assertEqual(
            self.day_query(padded_resource),
            {
                "resource_id": resource_id,
                "date": BOOKING_DATE,
                "bookings": [{"booking_id": booking_id, "start": START, "end": END}],
            },
        )
        # 前导零标识的预约仍参与冲突判断。
        self.run_error(
            "booking_conflict",
            "reserve", "--resource", padded_resource, "--start", START, "--end", END,
        )
        # 前导零标识可以正常取消，时段随之释放。
        payload = self.run_ok("cancel", "--booking", padded_booking)
        self.assertEqual(payload, {"booking_id": booking_id, "cancelled": True})
        self.assertEqual(self.day_query(resource_id)["bookings"], [])

    def test_leading_zeros_on_overflow_id_still_not_found(self):
        """0009223372036854775808 数值仍越界：按不存在处理。"""
        resource_id = self.add_resource()
        self.reserve_ok(resource_id)
        padded_overflow = "000" + OVERFLOW_ID

        self.run_error(
            "resource_not_found",
            "day-query", "--resource", padded_overflow, "--date", BOOKING_DATE,
        )
        self.run_error(
            "resource_not_found",
            "reserve", "--resource", padded_overflow, "--start", START, "--end", END,
        )
        self.run_error("booking_not_found", "cancel", "--booking", padded_overflow)

    # ---- 校验优先级：非法输入优先于“不存在” ----

    def test_invalid_input_takes_priority_over_overflow_id(self):
        """超大标识与非法日期/时间或起止顺序错误同时出现时，返回 invalid_input。"""
        cases = [
            ("day-query 日期无效",
             ["day-query", "--resource", OVERFLOW_ID, "--date", "2026-02-30"]),
            ("day-query 日期未零填充",
             ["day-query", "--resource", HUGE_ID, "--date", "2026-10-5"]),
            ("reserve 开始时间无效",
             ["reserve", "--resource", OVERFLOW_ID, "--start", "2026-10-05T25:00", "--end", END]),
            ("reserve 结束时间格式不符",
             ["reserve", "--resource", HUGE_ID, "--start", START, "--end", "2026-10-05 10:00"]),
            ("reserve 开始不早于结束",
             ["reserve", "--resource", OVERFLOW_ID, "--start", END, "--end", START]),
            ("reserve 起止相同",
             ["reserve", "--resource", OVERFLOW_ID, "--start", START, "--end", START]),
        ]
        for label, args in cases:
            with self.subTest(label=label):
                self.run_error("invalid_input", *args)

    # ---- 越界请求不影响已有数据与标识分配 ----

    def test_overflow_failures_do_not_disturb_existing_data(self):
        """越界请求前后查询结果一致；原预约仍参与冲突判断、可正常取消。"""
        resource_id = self.add_resource()
        booking_id = self.reserve_ok(resource_id)
        before = self.day_query(resource_id)

        failing_calls = [
            ["day-query", "--resource", OVERFLOW_ID, "--date", BOOKING_DATE],
            ["reserve", "--resource", OVERFLOW_ID, "--start", START, "--end", END],
            ["reserve", "--resource", HUGE_ID, "--start", START, "--end", END],
            ["cancel", "--booking", OVERFLOW_ID],
            ["cancel", "--booking", HUGE_ID],
        ]
        for args in failing_calls:
            proc = self.run_cli(*args)
            self.assertEqual(proc.returncode, 2, msg=f"args={args!r} stdout={proc.stdout!r}")
            self.assertIn(
                json.loads(proc.stdout)["error"],
                {"resource_not_found", "booking_not_found"},
            )

        # 查询结果（标识、起止文本、数量、顺序）与越界请求前完全一致。
        after = self.day_query(resource_id)
        self.assertEqual(after, before)
        self.assertEqual(
            after["bookings"],
            [{"booking_id": booking_id, "start": START, "end": END}],
        )
        # 原预约仍参与冲突判断。
        self.run_error(
            "booking_conflict",
            "reserve", "--resource", str(resource_id), "--start", START, "--end", END,
        )
        # 越界失败不消耗预约标识：下一成功预约仍获得紧随其后的标识。
        next_id = self.reserve_ok(resource_id, f"{BOOKING_DATE}T10:00", f"{BOOKING_DATE}T11:00")
        self.assertEqual(next_id, booking_id + 1)
        # 原预约仍可正常取消，时段随之释放。
        payload = self.run_ok("cancel", "--booking", str(booking_id))
        self.assertEqual(payload, {"booking_id": booking_id, "cancelled": True})
        self.assertEqual(
            self.day_query(resource_id)["bookings"],
            [{"booking_id": next_id, "start": f"{BOOKING_DATE}T10:00", "end": f"{BOOKING_DATE}T11:00"}],
        )

    def test_int64_max_boundary_behaves_as_plain_unknown_id(self):
        """边界值 9223372036854775807 本身可表示但不存在：同样返回“不存在”错误。"""
        resource_id = self.add_resource()
        self.reserve_ok(resource_id)

        self.run_error(
            "resource_not_found",
            "day-query", "--resource", str(INT64_MAX), "--date", BOOKING_DATE,
        )
        self.run_error("booking_not_found", "cancel", "--booking", str(INT64_MAX))


if __name__ == "__main__":
    unittest.main()
