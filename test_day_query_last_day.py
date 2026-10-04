"""day-query 日期上界（9999-12-31）的回归测试（仅标准库，python -m unittest 可发现）。

固定四位年份规则允许的最后一天上 day-query 的确定行为：
- 跨日预约（9999-12-30T23:30 至 9999-12-31T00:30）只出现一次、保留完整端点；
- 当天预约正常返回，结果按开始时间升序；
- 结束于 9999-12-31T00:00 的预约不属于最后一天（左闭右开）；
- 其他资源的预约、目标资源已取消的预约不出现；
- 存在但无匹配预约的资源返回空 bookings；
- 未登记正整数资源标识返回 resource_not_found；
- 10000-01-01 超出四位年份规则返回 invalid_input，优先于资源存在性检查，
  且不创建数据库文件；
- 重复查询与独立进程重开同一数据库后结果一致，查询为只读操作。

通过公开命令行入口 `python -m booking --db <文件> <命令>` 准备数据并断言，
每个用例使用独立的临时 SQLite 数据库，结束后自动清理；资源与预约标识均取自
命令实际返回值，不依赖已有数据库、当前日期、机器时区或任何第三方库。
预期端点与结果均以字面量在测试中明确给出，不调用产品内部查询函数生成。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# 项目根目录（booking 包所在目录），子进程以此作为工作目录，
# 保证 `python -m booking` 与测试运行时的当前目录无关。
PROJECT_ROOT = Path(__file__).resolve().parent

# 四位年份规则允许的最后一天及其前一日（固定 UTC+08:00 的本地日期）。
LAST_DAY = "9999-12-31"
PREV_DAY = "9999-12-30"
# 超出四位年份规则的日期。
BEYOND_LAST_DAY = "10000-01-01"


class DayQueryLastDayTestCase(unittest.TestCase):
    """每个用例一个独立的临时目录和数据库路径，tearDown 自动清理。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="booking-test-lastday-")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "test.sqlite"

    # ---- 公开命令的调用辅助 ----

    def run_cli(self, *args, db=None, env=None):
        """运行一条 booking 命令，返回 CompletedProcess。"""
        return subprocess.run(
            [sys.executable, "-m", "booking", "--db", str(db or self.db_path), *args],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

    def _failure_detail(self, args, proc, expected):
        """组装“输入 / 预期 / 实际”诊断文本，供断言失败时直接显示。"""
        return (
            f"\n输入命令参数: {args!r}"
            f"\n预期结果: {expected!r}（退出码见各断言）"
            f"\n实际退出码: {proc.returncode}"
            f"\n实际 stdout: {proc.stdout!r}"
            f"\n实际 stderr: {proc.stderr!r}"
        )

    def assert_single_json_result(self, args, proc, expected, expected_code):
        """断言退出码、空 stderr、stdout 恰好一个 JSON 对象且内容等于预期。"""
        detail = self._failure_detail(args, proc, expected)
        self.assertEqual(proc.returncode, expected_code, msg=detail)
        self.assertEqual(proc.stderr, "", msg=f"stderr 应为空：{detail}")
        # stdout 只能有一个 JSON 行：不许多余的空行或第二个对象。
        lines = proc.stdout.splitlines()
        self.assertEqual(len(lines), 1, msg=f"stdout 应只有一个 JSON 对象：{detail}")
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            self.fail(f"stdout 不是合法 JSON：{detail}")
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload, expected, msg=detail)
        return payload

    def run_ok(self, *args, db=None, env=None):
        """运行命令并断言退出码 0、stderr 为空、stdout 为单个 JSON 对象，返回解析结果。"""
        proc = self.run_cli(*args, db=db, env=env)
        detail = self._failure_detail(args, proc, None)
        self.assertEqual(proc.returncode, 0, msg=detail)
        self.assertEqual(proc.stderr, "", msg=f"stderr 应为空：{detail}")
        # stdout 只能有一个 JSON 行：不许多余的空行或第二个对象。
        lines = proc.stdout.splitlines()
        self.assertEqual(len(lines), 1, msg=f"stdout 应只有一个 JSON 对象：{detail}")
        payload = json.loads(proc.stdout)  # 非单个 JSON 文档会在此抛错
        self.assertIsInstance(payload, dict)
        return payload

    def run_error(self, expected_error, *args, db=None, env=None):
        """运行命令并断言退出码 2、stderr 为空、stdout 仅为对应的错误对象。"""
        args = list(args)
        proc = self.run_cli(*args, db=db, env=env)
        self.assert_single_json_result(
            args, proc, {"error": expected_error}, 2
        )
        return proc

    def add_resource(self, name="测试资源"):
        payload = self.run_ok("resource-add", "--name", name)
        self.assertIsInstance(payload["resource_id"], int)
        self.assertGreater(payload["resource_id"], 0)
        self.assertEqual(payload["name"], name)
        return payload["resource_id"]

    def reserve(self, resource_id, start, end):
        """提交预约并断言成功结果的完整结构（仅四个约定键），返回实际标识。"""
        args = [
            "reserve", "--resource", str(resource_id),
            "--start", start, "--end", end,
        ]
        proc = self.run_cli(*args)
        detail = self._failure_detail(args, proc, "成功的 reserve 结果对象")
        self.assertEqual(proc.returncode, 0, msg=detail)
        self.assertEqual(proc.stderr, "", msg=f"stderr 应为空：{detail}")
        self.assertEqual(len(proc.stdout.splitlines()), 1, msg=detail)
        payload = json.loads(proc.stdout)
        self.assertIsInstance(payload.get("booking_id"), int, msg=detail)
        self.assertGreater(payload["booking_id"], 0, msg=detail)
        self.assertEqual(
            payload,
            {
                "booking_id": payload["booking_id"],
                "resource_id": resource_id,
                "start": start,
                "end": end,
            },
            msg=detail,
        )
        return payload["booking_id"]

    def cancel(self, booking_id):
        payload = self.run_ok("cancel", "--booking", str(booking_id))
        self.assertEqual(payload, {"booking_id": booking_id, "cancelled": True})

    def expect_day_query(self, resource_id, date, expected, db=None, env=None):
        """执行按日查询并断言完整结果、退出码 0、空 stderr、单个 JSON 对象。"""
        args = [
            "day-query", "--resource", str(resource_id), "--date", date,
        ]
        proc = self.run_cli(*args, db=db, env=env)
        return proc, self.assert_single_json_result(args, proc, expected, 0)

    # ---- 成功路径：最后一天 ----

    def test_last_day_cross_day_listed_once_untruncated_and_same_day_sorted(self):
        """9999-12-31：跨日预约只出现一次且端点完整，当天预约按 start 升序返回；
        其他资源同日预约与本资源已取消预约均不出现。"""
        target_id = self.add_resource("目标资源")
        other_id = self.add_resource("其他资源")

        # 创建顺序刻意与开始时间顺序不同，以验证按 start 排序。
        cross_id = self.reserve(
            target_id, f"{PREV_DAY}T23:30", f"{LAST_DAY}T00:30"
        )
        same_day_id = self.reserve(
            target_id, f"{LAST_DAY}T09:00", f"{LAST_DAY}T10:00"
        )
        other_booking_id = self.reserve(
            other_id, f"{LAST_DAY}T09:00", f"{LAST_DAY}T10:00"
        )
        cancelled_id = self.reserve(
            target_id, f"{LAST_DAY}T11:00", f"{LAST_DAY}T12:00"
        )
        self.cancel(cancelled_id)

        # 预期结果逐字给出：跨日完整端点在前，当天预约在后，无其他记录。
        expected = {
            "resource_id": target_id,
            "date": LAST_DAY,
            "bookings": [
                {
                    "booking_id": cross_id,
                    "start": f"{PREV_DAY}T23:30",
                    "end": f"{LAST_DAY}T00:30",
                },
                {
                    "booking_id": same_day_id,
                    "start": f"{LAST_DAY}T09:00",
                    "end": f"{LAST_DAY}T10:00",
                },
            ],
        }
        _, payload = self.expect_day_query(target_id, LAST_DAY, expected)

        # 跨日预约只出现一次；顶层与每项仅含约定的键（已由完整相等断言覆盖）。
        booking_ids = [b["booking_id"] for b in payload["bookings"]]
        self.assertEqual(len(booking_ids), len(set(booking_ids)))
        self.assertNotIn(cancelled_id, booking_ids)
        self.assertNotIn(other_booking_id, booking_ids)
        # 保留完整原始端点，不截断到查询日边界。
        self.assertEqual(payload["bookings"][0]["start"], f"{PREV_DAY}T23:30")
        self.assertEqual(payload["bookings"][0]["end"], f"{LAST_DAY}T00:30")
        # 明确按开始时间升序。
        starts = [b["start"] for b in payload["bookings"]]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(
            starts, [f"{PREV_DAY}T23:30", f"{LAST_DAY}T09:00"]
        )

        # 对照：其他资源同日的预约在其自身资源下正常返回，只是不混入目标资源。
        self.expect_day_query(
            other_id,
            LAST_DAY,
            {
                "resource_id": other_id,
                "date": LAST_DAY,
                "bookings": [
                    {
                        "booking_id": other_booking_id,
                        "start": f"{LAST_DAY}T09:00",
                        "end": f"{LAST_DAY}T10:00",
                    }
                ],
            },
        )

    def test_booking_ending_exactly_at_last_day_midnight_is_excluded(self):
        """独立样例：结束于 9999-12-31T00:00 的预约不属于最后一天（左闭右开）。"""
        resource_id = self.add_resource()
        boundary_id = self.reserve(
            resource_id, f"{PREV_DAY}T22:00", f"{LAST_DAY}T00:00"
        )

        # 最后一天查不到该预约。
        self.expect_day_query(
            resource_id,
            LAST_DAY,
            {"resource_id": resource_id, "date": LAST_DAY, "bookings": []},
        )
        # 对照：同一条预约属于前一日，端点原样保留。
        self.expect_day_query(
            resource_id,
            PREV_DAY,
            {
                "resource_id": resource_id,
                "date": PREV_DAY,
                "bookings": [
                    {
                        "booking_id": boundary_id,
                        "start": f"{PREV_DAY}T22:00",
                        "end": f"{LAST_DAY}T00:00",
                    }
                ],
            },
        )

    def test_registered_resource_without_matches_returns_empty_bookings(self):
        """资源存在但最后一天没有匹配预约：空 bookings，结构与退出码不变。"""
        resource_id = self.add_resource()
        self.reserve(
            resource_id, f"{PREV_DAY}T09:00", f"{PREV_DAY}T10:00"
        )

        self.expect_day_query(
            resource_id,
            LAST_DAY,
            {"resource_id": resource_id, "date": LAST_DAY, "bookings": []},
        )

    # ---- 失败路径：同一日期边界 ----

    def test_unregistered_positive_id_on_last_day_returns_resource_not_found(self):
        """有效日期 9999-12-31 + 未登记的正整数标识：resource_not_found。"""
        registered_id = self.add_resource()
        unknown_id = registered_id + 100
        self.run_error(
            "resource_not_found",
            "day-query",
            "--resource",
            str(unknown_id),
            "--date",
            LAST_DAY,
        )

    def test_year_10000_returns_invalid_input_prior_to_resource_lookup(self):
        """10000-01-01 超出四位年份规则：invalid_input；资源不存在时仍先返回该错误，
        已登记资源与已存在数据库上同样是该错误，且不存在的数据库文件不会被创建。"""
        # 数据库文件与资源均不存在：输入校验先于建库与资源查找，文件不得出现。
        missing_db = Path(self._tmpdir.name) / "should-not-exist.sqlite"
        self.run_error(
            "invalid_input",
            "day-query",
            "--resource",
            "1",
            "--date",
            BEYOND_LAST_DAY,
            db=missing_db,
        )
        self.assertFalse(missing_db.exists())

        # 已有数据库、已登记资源上仍返回 invalid_input（与资源存在性无关）。
        resource_id = self.add_resource()
        self.run_error(
            "invalid_input",
            "day-query",
            "--resource",
            str(resource_id),
            "--date",
            BEYOND_LAST_DAY,
        )
        # 非法查询不改动已有数据：最后一天的正常查询仍为空。
        self.expect_day_query(
            resource_id,
            LAST_DAY,
            {"resource_id": resource_id, "date": LAST_DAY, "bookings": []},
        )

    # ---- 只读与一致性 ----

    def test_repeated_and_reopened_queries_are_identical_and_read_only(self):
        """重复查询、独立进程重开同一数据库（含不同进程时区）结果逐字节一致；
        查询不消耗预约标识、不改变已有预约。"""
        resource_id = self.add_resource("目标资源")
        cross_id = self.reserve(
            resource_id, f"{PREV_DAY}T23:30", f"{LAST_DAY}T00:30"
        )
        same_day_id = self.reserve(
            resource_id, f"{LAST_DAY}T09:00", f"{LAST_DAY}T10:00"
        )

        args = [
            "day-query", "--resource", str(resource_id), "--date", LAST_DAY,
        ]
        # 每次调用都是独立进程重新打开同一 SQLite 文件。
        proc_first = self.run_cli(*args)
        proc_second = self.run_cli(*args)
        # 刻意把进程时区改为与 UTC+08:00 不同：时间由产品固定时区解释。
        shifted_env = {**os.environ, "TZ": "America/Los_Angeles"}
        proc_other_tz = self.run_cli(*args, env=shifted_env)

        for proc in (proc_second, proc_other_tz):
            self.assertEqual(
                (proc.returncode, proc.stdout, proc.stderr),
                (proc_first.returncode, proc_first.stdout, proc_first.stderr),
                msg=(
                    f"\n首次: ({proc_first.returncode}, "
                    f"{proc_first.stdout!r}, {proc_first.stderr!r})"
                    f"\n再次: ({proc.returncode}, {proc.stdout!r}, "
                    f"{proc.stderr!r})"
                ),
            )

        expected = {
            "resource_id": resource_id,
            "date": LAST_DAY,
            "bookings": [
                {
                    "booking_id": cross_id,
                    "start": f"{PREV_DAY}T23:30",
                    "end": f"{LAST_DAY}T00:30",
                },
                {
                    "booking_id": same_day_id,
                    "start": f"{LAST_DAY}T09:00",
                    "end": f"{LAST_DAY}T10:00",
                },
            ],
        }
        self.assert_single_json_result(args, proc_first, expected, 0)

        # 查询不改变已有预约：与 09:00–10:00 重叠的时段仍被拒绝。
        self.run_error(
            "booking_conflict",
            "reserve",
            "--resource",
            str(resource_id),
            "--start",
            f"{LAST_DAY}T09:30",
            "--end",
            f"{LAST_DAY}T09:45",
        )
        # 查询为只读、不消耗标识：端点相接的新预约获得紧接其后的标识。
        next_id = self.reserve(
            resource_id, f"{LAST_DAY}T10:00", f"{LAST_DAY}T11:00"
        )
        self.assertEqual(next_id, same_day_id + 1)
        # 原两条预约端点与取消状态不变，且新预约按开始时间排在最后。
        self.expect_day_query(
            resource_id,
            LAST_DAY,
            {
                "resource_id": resource_id,
                "date": LAST_DAY,
                "bookings": [
                    {
                        "booking_id": cross_id,
                        "start": f"{PREV_DAY}T23:30",
                        "end": f"{LAST_DAY}T00:30",
                    },
                    {
                        "booking_id": same_day_id,
                        "start": f"{LAST_DAY}T09:00",
                        "end": f"{LAST_DAY}T10:00",
                    },
                    {
                        "booking_id": next_id,
                        "start": f"{LAST_DAY}T10:00",
                        "end": f"{LAST_DAY}T11:00",
                    },
                ],
            },
        )


if __name__ == "__main__":
    unittest.main()
