"""day-query --include-cancelled 可选无值开关的回归测试（仅标准库）。

通过公开命令行入口 `python -m booking --db <文件> <命令>` 准备数据并断言，
每个用例使用独立的临时 SQLite 数据库，结束后自动清理；
不依赖已有数据库、当前日期、机器时区或任何第三方库。
断言均基于解析后的 JSON 内容，不依赖输出对象的键顺序。

覆盖的公开行为：
- 省略开关时筛选、字段与排序与既有结果完全一致，每项不含 cancelled 字段；
- 提供开关时顶层字段不变，bookings 纳入已取消记录，每项仅在原字段之外
  增加 cancelled 布尔字段（未取消 false、已取消 true）；
- 取消记录不单独排到末尾：仍按 start 升序、相同时按 booking_id 数值升序；
- 左闭右开边界、跨日只出现一次且保留完整起止、9999-12-31、空数组语义
  在开启开关时保持不变；
- 取消后重订同一时段：旧记录（cancelled=true）与新记录（cancelled=false）
  各自返回、不合并；
- 给开关附加任何值、与非法日期/标识同时出现：invalid_input，优先于资源
  存在性检查，不新建数据库文件；合法输入而资源不存在（含超大标识）：
  resource_not_found；成功退出码 0、失败 2，stderr 始终为空。
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
LAST_DAY = "9999-12-31"


class DayQueryIncludeCancelledTestCase(unittest.TestCase):
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
        """运行命令并断言退出码 0、stderr 为空、stdout 为单个 JSON 对象。"""
        proc = self.run_cli(*args, db=db)
        self.assertEqual(
            proc.returncode, 0,
            msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}",
        )
        self.assertEqual(proc.stderr, "", msg=f"stderr={proc.stderr!r}")
        # stdout 必须恰好是一个 JSON 对象，其后不得再有内容。
        decoder = json.JSONDecoder()
        payload, end_index = decoder.raw_decode(proc.stdout)
        self.assertEqual(proc.stdout[end_index:].strip(), "")
        self.assertIsInstance(payload, dict)
        return payload

    def run_error(self, expected_error, *args, db=None):
        """运行命令并断言退出码 2、stderr 为空、输出为指定错误对象。"""
        proc = self.run_cli(*args, db=db)
        self.assertEqual(
            proc.returncode, 2,
            msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}",
        )
        self.assertEqual(proc.stderr, "", msg=f"stderr={proc.stderr!r}")
        self.assertEqual(json.loads(proc.stdout), {"error": expected_error})
        return proc

    def add_resource(self, name="测试资源"):
        payload = self.run_ok("resource-add", "--name", name)
        return payload["resource_id"]

    def reserve(self, resource_id, start, end):
        payload = self.run_ok(
            "reserve", "--resource", str(resource_id),
            "--start", start, "--end", end,
        )
        return payload["booking_id"]

    def cancel(self, booking_id):
        payload = self.run_ok("cancel", "--booking", str(booking_id))
        self.assertEqual(payload, {"booking_id": booking_id, "cancelled": True})

    def day_query(self, resource_id, date=QUERY_DATE, include_cancelled=False):
        args = ["day-query", "--resource", str(resource_id), "--date", date]
        if include_cancelled:
            args.append("--include-cancelled")
        return self.run_ok(*args)

    # ---- 验收主场景 ----

    def test_acceptance_active_and_cancelled_same_day(self):
        """资源 1：09:00–10:00 未取消、10:00–11:00 已取消。

        省略开关只返回第一项且无状态字段；开启开关返回两项并依次
        标记 false、true。
        """
        resource_id = self.add_resource()
        self.assertEqual(resource_id, 1)
        active_id = self.reserve(resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")
        cancelled_id = self.reserve(resource_id, f"{QUERY_DATE}T10:00", f"{QUERY_DATE}T11:00")
        self.cancel(cancelled_id)

        without_flag = self.day_query(resource_id)
        self.assertEqual(
            without_flag,
            {
                "resource_id": resource_id,
                "date": QUERY_DATE,
                "bookings": [
                    {"booking_id": active_id,
                     "start": f"{QUERY_DATE}T09:00", "end": f"{QUERY_DATE}T10:00"},
                ],
            },
        )
        # 缺省结果顶层与每项字段与开关上线前完全一致。
        self.assertEqual(set(without_flag.keys()), {"resource_id", "date", "bookings"})
        self.assertEqual(
            set(without_flag["bookings"][0].keys()),
            {"booking_id", "start", "end"},
        )

        with_flag = self.day_query(resource_id, include_cancelled=True)
        self.assertEqual(
            with_flag,
            {
                "resource_id": resource_id,
                "date": QUERY_DATE,
                "bookings": [
                    {"booking_id": active_id,
                     "start": f"{QUERY_DATE}T09:00", "end": f"{QUERY_DATE}T10:00",
                     "cancelled": False},
                    {"booking_id": cancelled_id,
                     "start": f"{QUERY_DATE}T10:00", "end": f"{QUERY_DATE}T11:00",
                     "cancelled": True},
                ],
            },
        )
        # 顶层字段不变；每项只多出一个 cancelled 字段，且为 JSON 布尔值。
        self.assertEqual(set(with_flag.keys()), {"resource_id", "date", "bookings"})
        for booking in with_flag["bookings"]:
            self.assertIsInstance(booking["cancelled"], bool)
            self.assertEqual(
                set(booking.keys()),
                {"booking_id", "start", "end", "cancelled"},
            )

    def test_cancel_then_rebook_same_slot_both_listed_separately(self):
        """取消后重订同一时段：旧记录 true 与新记录 false 各自返回，不合并。"""
        resource_id = self.add_resource()
        start, end = f"{QUERY_DATE}T14:00", f"{QUERY_DATE}T15:00"
        old_id = self.reserve(resource_id, start, end)
        self.cancel(old_id)
        new_id = self.reserve(resource_id, start, end)
        self.assertNotEqual(old_id, new_id)

        # 缺省只显示新预约。
        self.assertEqual(
            self.day_query(resource_id)["bookings"],
            [{"booking_id": new_id, "start": start, "end": end}],
        )

        # 开启开关后两条同一时段的记录各自返回，按 booking_id 数值升序，
        # 已取消的旧记录不与新记录合并。
        self.assertEqual(
            self.day_query(resource_id, include_cancelled=True)["bookings"],
            [
                {"booking_id": old_id, "start": start, "end": end,
                 "cancelled": True},
                {"booking_id": new_id, "start": start, "end": end,
                 "cancelled": False},
            ],
        )

    def test_cancelled_records_are_not_pushed_to_the_end(self):
        """取消记录参与统一排序：按 start 升序，不单独排在末尾。

        已取消的早间预约必须排在未取消的午间预约之前；相同 start 时
        按 booking_id 数值升序。
        """
        resource_id = self.add_resource()
        cancelled_early = self.reserve(
            resource_id, f"{QUERY_DATE}T08:00", f"{QUERY_DATE}T08:30"
        )
        active_noon = self.reserve(
            resource_id, f"{QUERY_DATE}T12:00", f"{QUERY_DATE}T12:30"
        )
        self.cancel(cancelled_early)

        payload = self.day_query(resource_id, include_cancelled=True)
        self.assertEqual(
            [(b["booking_id"], b["cancelled"]) for b in payload["bookings"]],
            [(cancelled_early, True), (active_noon, False)],
        )

    def test_same_start_tie_orders_by_numeric_booking_id(self):
        """相同 start 时按预约标识数值升序（即使取消记录标识更小）。"""
        resource_id = self.add_resource()
        # 同一时段不能并存两条未取消预约：先预订并取消第一条（释放时段），
        # 再预订第二条；于是两条记录 start 相同，已取消的旧记录标识更小。
        first = self.reserve(resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T09:30")
        self.cancel(first)
        second = self.reserve(resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T09:30")
        self.assertEqual(second, first + 1)

        payload = self.day_query(resource_id, include_cancelled=True)
        self.assertEqual(
            [b["booking_id"] for b in payload["bookings"]],
            [first, second],
        )
        self.assertEqual(
            [b["cancelled"] for b in payload["bookings"]],
            [True, False],
        )

    # ---- 边界与既有语义在开启开关时保持不变 ----

    def test_day_boundaries_stay_half_open_with_flag(self):
        """结束于当天 00:00、开始于次日 00:00 的记录即使已取消也不纳入。"""
        resource_id = self.add_resource()
        ending_at_midnight = self.reserve(
            resource_id, f"{PREV_DATE}T22:00", f"{QUERY_DATE}T00:00"
        )
        starting_next_midnight = self.reserve(
            resource_id, f"{NEXT_DATE}T00:00", f"{NEXT_DATE}T02:00"
        )
        self.cancel(ending_at_midnight)
        self.cancel(starting_next_midnight)

        payload = self.day_query(resource_id, include_cancelled=True)
        self.assertEqual(payload["bookings"], [])

    def test_cross_day_cancelled_booking_listed_once_untruncated(self):
        """跨日的已取消记录只出现一次，保留完整起止时间。"""
        resource_id = self.add_resource()
        cross_id = self.reserve(
            resource_id, f"{PREV_DATE}T23:30", f"{QUERY_DATE}T01:00"
        )
        self.cancel(cross_id)

        payload = self.day_query(resource_id, include_cancelled=True)
        self.assertEqual(
            payload["bookings"],
            [
                {"booking_id": cross_id,
                 "start": f"{PREV_DATE}T23:30", "end": f"{QUERY_DATE}T01:00",
                 "cancelled": True},
            ],
        )

    def test_last_day_with_flag(self):
        """9999-12-31 开启开关：当天已取消记录返回，结束于当天零点的不返回。"""
        resource_id = self.add_resource()
        active_id = self.reserve(
            resource_id, f"{LAST_DAY}T09:00", f"{LAST_DAY}T10:00"
        )
        cancelled_id = self.reserve(
            resource_id, f"{LAST_DAY}T14:00", f"{LAST_DAY}T15:00"
        )
        boundary_id = self.reserve(
            resource_id, "9999-12-30T22:00", f"{LAST_DAY}T00:00"
        )
        self.cancel(cancelled_id)
        self.cancel(boundary_id)

        payload = self.day_query(resource_id, date=LAST_DAY, include_cancelled=True)
        self.assertEqual(
            payload,
            {
                "resource_id": resource_id,
                "date": LAST_DAY,
                "bookings": [
                    {"booking_id": active_id,
                     "start": f"{LAST_DAY}T09:00", "end": f"{LAST_DAY}T10:00",
                     "cancelled": False},
                    {"booking_id": cancelled_id,
                     "start": f"{LAST_DAY}T14:00", "end": f"{LAST_DAY}T15:00",
                     "cancelled": True},
                ],
            },
        )

    def test_empty_day_with_flag_returns_empty_list(self):
        """资源存在但当天没有匹配记录：开启开关仍成功返回空数组。"""
        resource_id = self.add_resource()
        self.reserve(resource_id, f"{PREV_DATE}T09:00", f"{PREV_DATE}T10:00")

        payload = self.day_query(resource_id, include_cancelled=True)
        self.assertEqual(
            payload,
            {"resource_id": resource_id, "date": QUERY_DATE, "bookings": []},
        )

    def test_other_resource_cancelled_bookings_are_excluded(self):
        """开启开关也只返回目标资源的记录，其他资源（含已取消）不混入。"""
        target_id = self.add_resource("目标资源")
        other_id = self.add_resource("其他资源")
        target_cancelled = self.reserve(
            target_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00"
        )
        other_cancelled = self.reserve(
            other_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00"
        )
        self.cancel(target_cancelled)
        self.cancel(other_cancelled)

        payload = self.day_query(target_id, include_cancelled=True)
        self.assertEqual(
            [b["booking_id"] for b in payload["bookings"]],
            [target_cancelled],
        )

    def test_repeated_flagged_queries_are_identical_and_read_only(self):
        """开启开关重复查询结果一致且只读：不改动记录、不消耗标识。"""
        resource_id = self.add_resource()
        first_id = self.reserve(
            resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00"
        )
        self.cancel(first_id)

        first = self.day_query(resource_id, include_cancelled=True)
        second = self.day_query(resource_id, include_cancelled=True)
        self.assertEqual(first, second)
        next_id = self.reserve(
            resource_id, f"{QUERY_DATE}T10:00", f"{QUERY_DATE}T11:00"
        )
        self.assertEqual(next_id, first_id + 1)

    # ---- 失败路径与开关参数校验 ----

    def test_flag_with_value_returns_invalid_input(self):
        """给无值开关附加任何形式的值：invalid_input，退出码 2、stderr 为空。"""
        cases = [
            ["day-query", "--resource", "1", "--date", QUERY_DATE,
             "--include-cancelled", "true"],
            ["day-query", "--resource", "1", "--date", QUERY_DATE,
             "--include-cancelled=1"],
            ["day-query", "--resource", "1", "--date", QUERY_DATE,
             "--include-cancelled", "--resource"],
        ]
        for args in cases:
            with self.subTest(args=args):
                self.run_error("invalid_input", *args)

    def test_invalid_input_with_flag_has_priority_and_creates_no_file(self):
        """开关与非法日期/标识同时出现：先报 invalid_input，且不建库。"""
        missing_db = Path(self._tmpdir.name) / "should-not-exist.sqlite"
        cases = [
            ["day-query", "--resource", "1", "--date", "2026-02-30",
             "--include-cancelled"],
            ["day-query", "--resource", "0", "--date", QUERY_DATE,
             "--include-cancelled"],
            ["day-query", "--resource", "1", "--date", QUERY_DATE,
             "--include-cancelled", "--unknown-flag"],
        ]
        for index, args in enumerate(cases):
            with self.subTest(args=args):
                db = Path(self._tmpdir.name) / f"should-not-exist-{index}.sqlite"
                self.run_error("invalid_input", *args, db=db)
                self.assertFalse(db.exists())
        self.assertFalse(missing_db.exists())

    def test_missing_resource_with_flag_returns_resource_not_found(self):
        """输入合法（含开关）而资源不存在：resource_not_found。"""
        resource_id = self.add_resource()
        self.run_error(
            "resource_not_found",
            "day-query", "--resource", str(resource_id + 100),
            "--date", QUERY_DATE, "--include-cancelled",
        )

    def test_oversized_resource_id_with_flag_returns_resource_not_found(self):
        """超过 SQLite 整数上限的正整数标识 + 合法开关：resource_not_found。"""
        self.run_error(
            "resource_not_found",
            "day-query", "--resource", "9223372036854775808",
            "--date", QUERY_DATE, "--include-cancelled",
        )


if __name__ == "__main__":
    unittest.main()
