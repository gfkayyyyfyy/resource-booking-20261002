"""reserve 时段冲突规则的回归测试（仅标准库，python -m unittest 可发现）。

通过公开命令行入口 `python -m booking --db <文件> <命令>` 准备数据并断言，
每个用例使用独立的临时 SQLite 数据库，结束后自动清理；
不依赖已有数据库、当前日期、机器时区或任何第三方库。
断言均基于解析后的 JSON 内容，不依赖输出对象的键顺序，也不预设 booking_id。

覆盖的公开行为（固定 UTC+08:00，样例日期 2026-10-05）：
- 同一资源已有 09:00–10:00 预约时，完全相同、前压（08:30–09:30）、
  后压（09:30–10:30）、包含（08:30–10:30）、被包含（09:15–09:45）
  五种时段均返回 {"error": "booking_conflict"} 并以退出码 2 失败；
- 08:00–09:00 与 10:00–11:00 可成功预约，证明左闭右开、端点相接不算重叠；
- 不同资源上的同一时段互不影响，可各自预约成功；
- 跨午夜已有 2026-10-05T23:30–2026-10-06T00:30 时，次日 00:00–01:00
  仍被拒绝，00:30–01:00 允许创建，规则跨日一致；
- 每次冲突失败前后用 day-query 比对可观察状态：已有预约的标识、时段、
  数量均不变（跨日案例分别查询涉及的两个日期）；
- 冲突失败后同资源相邻时段仍可预约，重新打开同一文件后原预约与
  成功创建的预约都保留。
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

# 固定的样例日期与基准时段（固定 UTC+08:00 的本地时间，与运行环境无关）。
BOOKING_DATE = "2026-10-05"
NEXT_DATE = "2026-10-06"
BASE_START = f"{BOOKING_DATE}T09:00"
BASE_END = f"{BOOKING_DATE}T10:00"


class ReserveConflictTestCase(unittest.TestCase):
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

    def add_resource(self, name="冲突测试资源"):
        payload = self.run_ok("resource-add", "--name", name)
        self.assertIsInstance(payload["resource_id"], int)
        self.assertGreater(payload["resource_id"], 0)
        self.assertEqual(payload["name"], name)
        return payload["resource_id"]

    def reserve_ok(self, resource_id, start, end):
        """提交预约并断言成功：退出码 0，返回正确资源、原始起止文本与新的正整数标识。"""
        payload = self.run_ok(
            "reserve", "--resource", str(resource_id), "--start", start, "--end", end
        )
        booking_id = payload.get("booking_id")
        self.assertIsInstance(
            booking_id, int, msg=f"预约 {start} -> {end} 应返回正整数 booking_id，实际输出 {payload!r}"
        )
        self.assertNotIsInstance(booking_id, bool)
        self.assertGreater(booking_id, 0, msg=f"预约 {start} -> {end} 的 booking_id 应为正整数，实际为 {booking_id}")
        # 整体比较：资源标识与起止文本必须原样返回，且不依赖键序。
        self.assertEqual(
            payload,
            {
                "booking_id": booking_id,
                "resource_id": resource_id,
                "start": start,
                "end": end,
            },
            msg=f"预约 {start} -> {end} 成功返回结构与预期不符",
        )
        return booking_id

    def assert_conflict(self, resource_id, start, end):
        """提交预约并断言返回 {"error": "booking_conflict"}、退出码为 2。

        失败信息带上请求时段与实际返回，便于定位是哪条公开规则偏差。
        """
        slot = f"{start} -> {end}"
        proc = self.run_cli(
            "reserve", "--resource", str(resource_id), "--start", start, "--end", end
        )
        self.assertEqual(
            proc.returncode, 2,
            msg=f"请求时段 {slot} 应冲突并以退出码 2 失败，实际退出码 {proc.returncode}，"
                f"stdout={proc.stdout!r} stderr={proc.stderr!r}",
        )
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            self.fail(f"请求时段 {slot} 冲突失败应输出单个 JSON 对象，实际 stdout={proc.stdout!r}")
        self.assertEqual(
            payload, {"error": "booking_conflict"},
            msg=f"请求时段 {slot} 应返回 {{\"error\": \"booking_conflict\"}}，实际返回 {payload!r}",
        )

    def day_query(self, resource_id, date=BOOKING_DATE):
        return self.run_ok("day-query", "--resource", str(resource_id), "--date", date)

    def assert_day_bookings(self, resource_id, date, expected):
        """断言某日查询精确等于 expected（[(booking_id, start, end), ...]，按返回顺序）。"""
        payload = self.day_query(resource_id, date)
        self.assertEqual(payload["resource_id"], resource_id)
        self.assertEqual(payload["date"], date)
        expected_bookings = [
            {"booking_id": booking_id, "start": start, "end": end}
            for booking_id, start, end in expected
        ]
        self.assertEqual(
            len(payload["bookings"]), len(expected_bookings),
            msg=f"{date} 的预约数量应为 {len(expected_bookings)}，实际为 {len(payload['bookings'])}",
        )
        self.assertEqual(payload["bookings"], expected_bookings)
        return payload

    # ---- 同日：五种重叠形态全部冲突，且失败不改动数据 ----

    def test_overlapping_requests_return_booking_conflict(self):
        """已有 09:00–10:00 时，相同/前压/后压/包含/被包含五种时段均冲突，数据状态不变。"""
        cases = [
            ("完全相同时段", f"{BOOKING_DATE}T09:00", f"{BOOKING_DATE}T10:00"),
            ("前段重叠 08:30-09:30", f"{BOOKING_DATE}T08:30", f"{BOOKING_DATE}T09:30"),
            ("后段重叠 09:30-10:30", f"{BOOKING_DATE}T09:30", f"{BOOKING_DATE}T10:30"),
            ("包含已有时段 08:30-10:30", f"{BOOKING_DATE}T08:30", f"{BOOKING_DATE}T10:30"),
            ("被已有时段包含 09:15-09:45", f"{BOOKING_DATE}T09:15", f"{BOOKING_DATE}T09:45"),
        ]
        for label, start, end in cases:
            with self.subTest(冲突时段=label, start=start, end=end):
                # 每个子用例使用独立资源，状态互不干扰。
                resource_id = self.add_resource(f"资源-{label}")
                base_id = self.reserve_ok(resource_id, BASE_START, BASE_END)

                # 失败前的可观察状态：恰有基准预约一条。
                before = self.assert_day_bookings(
                    resource_id, BOOKING_DATE,
                    [(base_id, BASE_START, BASE_END)],
                )

                self.assert_conflict(resource_id, start, end)

                # 失败后：标识、时段、数量均不变（整个 day-query 结果逐字段相等）。
                after = self.day_query(resource_id, BOOKING_DATE)
                self.assertEqual(
                    after, before,
                    msg=f"冲突失败（{start} -> {end}）改动了已有预约：before={before!r} after={after!r}",
                )
                self.assertEqual(
                    [b["booking_id"] for b in after["bookings"]], [base_id],
                    msg="冲突失败后已有预约标识发生变化",
                )

    # ---- 左闭右开：端点相接允许预约 ----

    def test_back_to_back_endpoint_slots_succeed(self):
        """已有 09:00–10:00 时，08:00–09:00 与 10:00–11:00 均成功（端点相接不算重叠）。"""
        resource_id = self.add_resource()
        base_id = self.reserve_ok(resource_id, BASE_START, BASE_END)

        left_id = self.reserve_ok(resource_id, f"{BOOKING_DATE}T08:00", BASE_START)
        right_id = self.reserve_ok(resource_id, BASE_END, f"{BOOKING_DATE}T11:00")

        # 三个标识互不相同且均为正整数（不预设具体数值）。
        self.assertEqual(len({base_id, left_id, right_id}), 3)

        # day-query 按 start 升序返回三条完整预约。
        self.assert_day_bookings(
            resource_id, BOOKING_DATE,
            [
                (left_id, f"{BOOKING_DATE}T08:00", BASE_START),
                (base_id, BASE_START, BASE_END),
                (right_id, BASE_END, f"{BOOKING_DATE}T11:00"),
            ],
        )

    # ---- 不同资源互不影响 ----

    def test_same_slot_on_different_resource_succeeds(self):
        """另一资源上的 09:00–10:00 可成功预约，且各自的查询只显示本资源的预约。"""
        resource_a = self.add_resource("资源A")
        resource_b = self.add_resource("资源B")
        self.assertNotEqual(resource_a, resource_b)

        booking_a = self.reserve_ok(resource_a, BASE_START, BASE_END)
        booking_b = self.reserve_ok(resource_b, BASE_START, BASE_END)
        self.assertNotEqual(booking_a, booking_b)

        self.assert_day_bookings(
            resource_a, BOOKING_DATE, [(booking_a, BASE_START, BASE_END)]
        )
        self.assert_day_bookings(
            resource_b, BOOKING_DATE, [(booking_b, BASE_START, BASE_END)]
        )

    # ---- 跨午夜：冲突规则在日期边界后仍一致 ----

    def test_conflict_rule_across_midnight(self):
        """已有 23:30–次日00:30 时，次日 00:00–01:00 冲突，00:30–01:00 成功（端点相接）。"""
        resource_id = self.add_resource()
        cross_start = f"{BOOKING_DATE}T23:30"
        cross_end = f"{NEXT_DATE}T00:30"
        cross_id = self.reserve_ok(resource_id, cross_start, cross_end)

        # 跨日预约在涉及的两个日期查询中都完整出现、不截断。
        before_first = self.assert_day_bookings(
            resource_id, BOOKING_DATE, [(cross_id, cross_start, cross_end)]
        )
        before_second = self.assert_day_bookings(
            resource_id, NEXT_DATE, [(cross_id, cross_start, cross_end)]
        )

        # 次日 00:00–01:00 与跨日预约重叠：冲突。
        overlap_start = f"{NEXT_DATE}T00:00"
        overlap_end = f"{NEXT_DATE}T01:00"
        self.assert_conflict(resource_id, overlap_start, overlap_end)

        # 两个日期的查询结果在失败前后逐字段一致（标识、时段、数量不变）。
        self.assertEqual(self.day_query(resource_id, BOOKING_DATE), before_first)
        self.assertEqual(self.day_query(resource_id, NEXT_DATE), before_second)

        # 次日 00:30–01:00 与跨日预约端点相接：允许创建，返回原文时间与新标识。
        touch_start = f"{NEXT_DATE}T00:30"
        touch_end = f"{NEXT_DATE}T01:00"
        touch_id = self.reserve_ok(resource_id, touch_start, touch_end)
        self.assertNotEqual(touch_id, cross_id)

        # 前一日仍只显示原跨日预约。
        self.assert_day_bookings(
            resource_id, BOOKING_DATE, [(cross_id, cross_start, cross_end)]
        )
        # 次日显示两条：跨日预约（start 在前）与新预约，均保留完整起止文本。
        self.assert_day_bookings(
            resource_id, NEXT_DATE,
            [
                (cross_id, cross_start, cross_end),
                (touch_id, touch_start, touch_end),
            ],
        )

    # ---- 冲突失败后相邻时段仍可预约，重开文件后状态保留 ----

    def test_adjacent_slot_still_works_and_state_survives_reopening(self):
        """冲突被拒后同资源相邻时段仍可预约；重新打开同一文件后原预约与新预约都在。"""
        resource_id = self.add_resource()
        base_id = self.reserve_ok(resource_id, BASE_START, BASE_END)

        # 一次冲突失败，状态不变。
        self.assert_conflict(resource_id, f"{BOOKING_DATE}T09:30", f"{BOOKING_DATE}T10:30")
        self.assert_day_bookings(
            resource_id, BOOKING_DATE, [(base_id, BASE_START, BASE_END)]
        )

        # 相邻时段仍可正常预约。
        left_id = self.reserve_ok(resource_id, f"{BOOKING_DATE}T08:00", BASE_START)
        right_id = self.reserve_ok(resource_id, BASE_END, f"{BOOKING_DATE}T11:00")

        # 每条命令都是独立进程；以下查询即“重新打开同一文件”后的读取：
        # 原预约与两条成功创建的预约都保留，按 start 升序排列。
        self.assert_day_bookings(
            resource_id, BOOKING_DATE,
            [
                (left_id, f"{BOOKING_DATE}T08:00", BASE_START),
                (base_id, BASE_START, BASE_END),
                (right_id, BASE_END, f"{BOOKING_DATE}T11:00"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
