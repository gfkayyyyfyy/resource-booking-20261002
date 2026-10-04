"""free-query 可选参数 --min-minutes 的回归测试（仅标准库，python -m unittest 可发现）。

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
        args = ["free-query", "--resource", str(resource_id),
                "--start", start, "--end", end]
        if min_minutes is not None:
            args += ["--min-minutes", min_minutes]
        return self.run_ok(*args)

    def prepare_acceptance_resource(self):
        """验收场景数据：当天 09:00-10:00 与 10:30-11:00 两条预约。"""
        resource_id = self.add_resource()
        self.reserve(resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")
        self.reserve(resource_id, f"{QUERY_DATE}T10:30", f"{QUERY_DATE}T11:00")
        return resource_id

    # ---- 成功路径 ----

    def test_acceptance_min_30_returns_both_slots(self):
        """验收场景：09:30-12:00 窗口、--min-minutes 30，两段空闲均达标。"""
        resource_id = self.prepare_acceptance_resource()

        payload = self.free_query(
            resource_id, f"{QUERY_DATE}T09:30", f"{QUERY_DATE}T12:00", "30"
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
            resource_id, f"{QUERY_DATE}T09:30", f"{QUERY_DATE}T12:00", "31"
        )

        self.assertEqual(
            payload["free_slots"],
            [{"start": f"{QUERY_DATE}T11:00", "end": f"{QUERY_DATE}T12:00"}],
        )

    def test_omitted_parameter_matches_unfiltered_result(self):
        """省略 --min-minutes 时，返回内容与既有查询完全一致。"""
        resource_id = self.prepare_acceptance_resource()

        payload = self.free_query(resource_id, f"{QUERY_DATE}T09:30", f"{QUERY_DATE}T12:00")

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

    def test_duration_exactly_equal_to_min_is_included(self):
        """时长恰好等于下限的区间保留（大于或等于即合格）。"""
        resource_id = self.prepare_acceptance_resource()

        payload = self.free_query(
            resource_id, f"{QUERY_DATE}T09:30", f"{QUERY_DATE}T12:00", "60"
        )

        self.assertEqual(
            payload["free_slots"],
            [{"start": f"{QUERY_DATE}T11:00", "end": f"{QUERY_DATE}T12:00"}],
        )

    def test_qualifying_slot_keeps_full_endpoints(self):
        """合格区间保留完整起止端点，不截成指定长度、不拆分。"""
        resource_id = self.add_resource()
        self.reserve(resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T09:30")

        payload = self.free_query(
            resource_id, f"{QUERY_DATE}T08:00", f"{QUERY_DATE}T12:00", "30"
        )

        self.assertEqual(
            payload["free_slots"],
            [
                {"start": f"{QUERY_DATE}T08:00", "end": f"{QUERY_DATE}T09:00"},
                {"start": f"{QUERY_DATE}T09:30", "end": f"{QUERY_DATE}T12:00"},
            ],
        )

    def test_no_qualifying_slot_returns_empty_list(self):
        """资源存在但没有达标区间：free_slots 为空数组并成功退出。"""
        resource_id = self.prepare_acceptance_resource()

        payload = self.free_query(
            resource_id, f"{QUERY_DATE}T09:30", f"{QUERY_DATE}T12:00", "61"
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

    def test_no_bookings_returns_whole_window_only_when_long_enough(self):
        """无占用时整个窗口仅在时长达标时返回。"""
        resource_id = self.add_resource()

        enough = self.free_query(
            resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00", "60"
        )
        not_enough = self.free_query(
            resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00", "61"
        )

        self.assertEqual(
            enough["free_slots"],
            [{"start": f"{QUERY_DATE}T09:00", "end": f"{QUERY_DATE}T10:00"}],
        )
        self.assertEqual(not_enough["free_slots"], [])

    def test_duration_counts_only_part_inside_window(self):
        """跨出窗口的占用截断后，按窗口内的连续分钟数筛选。"""
        resource_id = self.add_resource()
        self.reserve(resource_id, f"{QUERY_DATE}T08:00", f"{QUERY_DATE}T09:30")

        # 窗口内空闲仅 09:30-10:00 共 30 分钟：30 达标，31 不达标。
        kept = self.free_query(
            resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00", "30"
        )
        dropped = self.free_query(
            resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00", "31"
        )

        self.assertEqual(
            kept["free_slots"],
            [{"start": f"{QUERY_DATE}T09:30", "end": f"{QUERY_DATE}T10:00"}],
        )
        self.assertEqual(dropped["free_slots"], [])

    def test_cross_midnight_slot_duration_uses_full_datetimes(self):
        """跨午夜区间按完整日期计算经过的分钟数。"""
        resource_id = self.add_resource()
        self.reserve(resource_id, f"{QUERY_DATE}T23:00", f"{NEXT_DATE}T01:00")

        # 空闲为 00:00-23:00（1380 分钟）与次日 01:00-23:00（1320 分钟）。
        payload = self.free_query(
            resource_id, f"{QUERY_DATE}T00:00", f"{NEXT_DATE}T23:00", "1320"
        )

        self.assertEqual(
            payload["free_slots"],
            [
                {"start": f"{QUERY_DATE}T00:00", "end": f"{QUERY_DATE}T23:00"},
                {"start": f"{NEXT_DATE}T01:00", "end": f"{NEXT_DATE}T23:00"},
            ],
        )
        # 1321 分钟时只保留前一段（跨午夜段按完整日期计为 1320 分钟，不达标）。
        longer = self.free_query(
            resource_id, f"{QUERY_DATE}T00:00", f"{NEXT_DATE}T23:00", "1321"
        )
        self.assertEqual(
            longer["free_slots"],
            [{"start": f"{QUERY_DATE}T00:00", "end": f"{QUERY_DATE}T23:00"}],
        )

    def test_leading_zeros_accepted_by_value(self):
        """前导零允许且按数值解释：0030 等价于 30。"""
        resource_id = self.prepare_acceptance_resource()

        payload = self.free_query(
            resource_id, f"{QUERY_DATE}T09:30", f"{QUERY_DATE}T12:00", "0030"
        )

        self.assertEqual(
            payload["free_slots"],
            [
                {"start": f"{QUERY_DATE}T10:00", "end": f"{QUERY_DATE}T10:30"},
                {"start": f"{QUERY_DATE}T11:00", "end": f"{QUERY_DATE}T12:00"},
            ],
        )

    def test_boundary_values_1_and_1440_are_valid(self):
        """边界值 1 与 1440 均为合法输入。"""
        resource_id = self.add_resource()

        one = self.free_query(
            resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T09:01", "1"
        )
        full_day = self.free_query(
            resource_id, f"{QUERY_DATE}T00:00", f"{NEXT_DATE}T00:00", "1440"
        )

        self.assertEqual(
            one["free_slots"],
            [{"start": f"{QUERY_DATE}T09:00", "end": f"{QUERY_DATE}T09:01"}],
        )
        self.assertEqual(
            full_day["free_slots"],
            [{"start": f"{QUERY_DATE}T00:00", "end": f"{NEXT_DATE}T00:00"}],
        )

    def test_repeated_queries_are_identical_and_do_not_consume_ids(self):
        """带参数的重复查询结果一致；查询为只读，不消耗预约标识。"""
        resource_id = self.add_resource()
        first_id = self.reserve(resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")

        first = self.free_query(
            resource_id, f"{QUERY_DATE}T08:00", f"{QUERY_DATE}T12:00", "30"
        )
        second = self.free_query(
            resource_id, f"{QUERY_DATE}T08:00", f"{QUERY_DATE}T12:00", "30"
        )

        self.assertEqual(first, second)
        next_id = self.reserve(resource_id, f"{QUERY_DATE}T10:00", f"{QUERY_DATE}T11:00")
        self.assertEqual(next_id, first_id + 1)

    # ---- 失败路径 ----

    def test_unknown_resource_with_valid_min_minutes_returns_resource_not_found(self):
        """全部输入合法而资源不存在：resource_not_found，退出码 2。"""
        resource_id = self.add_resource()
        self.run_error(
            "resource_not_found",
            "free-query", "--resource", str(resource_id + 100),
            "--start", f"{QUERY_DATE}T09:00", "--end", f"{QUERY_DATE}T10:00",
            "--min-minutes", "30",
        )

    def test_oversized_resource_id_with_valid_min_minutes_returns_resource_not_found(self):
        """超大正整数标识与合法 --min-minutes：resource_not_found。"""
        self.add_resource()
        self.run_error(
            "resource_not_found",
            "free-query", "--resource", "9223372036854775808",
            "--start", f"{QUERY_DATE}T09:00", "--end", f"{QUERY_DATE}T10:00",
            "--min-minutes", "1440",
        )

    def test_invalid_min_minutes_values_return_invalid_input(self):
        """缺值、空文本、零、负数、小数、超范围、含空白及其他字符：invalid_input。"""
        cases = [
            ("缺值", ["free-query", "--resource", "1",
                      "--start", f"{QUERY_DATE}T09:00", "--end", f"{QUERY_DATE}T10:00",
                      "--min-minutes"]),
            ("空文本", ["free-query", "--resource", "1",
                        "--start", f"{QUERY_DATE}T09:00", "--end", f"{QUERY_DATE}T10:00",
                        "--min-minutes", ""]),
            ("零", ["free-query", "--resource", "1",
                    "--start", f"{QUERY_DATE}T09:00", "--end", f"{QUERY_DATE}T10:00",
                    "--min-minutes", "0"]),
            ("纯零文本", ["free-query", "--resource", "1",
                          "--start", f"{QUERY_DATE}T09:00", "--end", f"{QUERY_DATE}T10:00",
                          "--min-minutes", "000"]),
            ("负数", ["free-query", "--resource", "1",
                      "--start", f"{QUERY_DATE}T09:00", "--end", f"{QUERY_DATE}T10:00",
                      "--min-minutes", "-30"]),
            ("小数", ["free-query", "--resource", "1",
                      "--start", f"{QUERY_DATE}T09:00", "--end", f"{QUERY_DATE}T10:00",
                      "--min-minutes", "30.5"]),
            ("超出上限", ["free-query", "--resource", "1",
                          "--start", f"{QUERY_DATE}T09:00", "--end", f"{QUERY_DATE}T10:00",
                          "--min-minutes", "1441"]),
            ("远超上限", ["free-query", "--resource", "1",
                          "--start", f"{QUERY_DATE}T09:00", "--end", f"{QUERY_DATE}T10:00",
                          "--min-minutes", "99999"]),
            ("带前导零的超限值", ["free-query", "--resource", "1",
                                  "--start", f"{QUERY_DATE}T09:00", "--end", f"{QUERY_DATE}T10:00",
                                  "--min-minutes", "01441"]),
            ("前导空格", ["free-query", "--resource", "1",
                          "--start", f"{QUERY_DATE}T09:00", "--end", f"{QUERY_DATE}T10:00",
                          "--min-minutes", " 30"]),
            ("尾随空格", ["free-query", "--resource", "1",
                          "--start", f"{QUERY_DATE}T09:00", "--end", f"{QUERY_DATE}T10:00",
                          "--min-minutes", "30 "]),
            ("尾随换行", ["free-query", "--resource", "1",
                          "--start", f"{QUERY_DATE}T09:00", "--end", f"{QUERY_DATE}T10:00",
                          "--min-minutes", "30\n"]),
            ("含制表符", ["free-query", "--resource", "1",
                          "--start", f"{QUERY_DATE}T09:00", "--end", f"{QUERY_DATE}T10:00",
                          "--min-minutes", "3\t0"]),
            ("带正号", ["free-query", "--resource", "1",
                        "--start", f"{QUERY_DATE}T09:00", "--end", f"{QUERY_DATE}T10:00",
                        "--min-minutes", "+30"]),
            ("非数字文本", ["free-query", "--resource", "1",
                            "--start", f"{QUERY_DATE}T09:00", "--end", f"{QUERY_DATE}T10:00",
                            "--min-minutes", "abc"]),
            ("Unicode 数字", ["free-query", "--resource", "1",
                              "--start", f"{QUERY_DATE}T09:00", "--end", f"{QUERY_DATE}T10:00",
                              "--min-minutes", "٣٠"]),
        ]
        for label, args in cases:
            with self.subTest(label=label):
                self.run_error("invalid_input", *args)

    def test_invalid_min_minutes_takes_priority_over_resource_not_found(self):
        """非法 --min-minutes 与不存在的资源同时出现时，优先报告 invalid_input。"""
        self.run_error(
            "invalid_input",
            "free-query", "--resource", "999",
            "--start", f"{QUERY_DATE}T09:00", "--end", f"{QUERY_DATE}T10:00",
            "--min-minutes", "0",
        )

    def test_invalid_min_minutes_does_not_create_database_file(self):
        """非法 --min-minutes 指向尚不存在的数据库路径时，不得创建文件。"""
        missing_db = Path(self._tmpdir.name) / "should-not-exist.sqlite"
        self.run_error(
            "invalid_input",
            "free-query", "--resource", "1",
            "--start", f"{QUERY_DATE}T09:00", "--end", f"{QUERY_DATE}T10:00",
            "--min-minutes", "1441",
            db=missing_db,
        )
        self.assertFalse(missing_db.exists())

    def test_invalid_min_minutes_does_not_modify_existing_data(self):
        """非法 --min-minutes 不新增记录、不消耗标识，已有数据不变。"""
        resource_id = self.add_resource()
        first_id = self.reserve(resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")
        before = self.free_query(resource_id, f"{QUERY_DATE}T08:00", f"{QUERY_DATE}T12:00")

        self.run_error(
            "invalid_input",
            "free-query", "--resource", str(resource_id),
            "--start", f"{QUERY_DATE}T08:00", "--end", f"{QUERY_DATE}T12:00",
            "--min-minutes", "-1",
        )

        after = self.free_query(resource_id, f"{QUERY_DATE}T08:00", f"{QUERY_DATE}T12:00")
        self.assertEqual(before, after)
        next_id = self.reserve(resource_id, f"{QUERY_DATE}T10:00", f"{QUERY_DATE}T11:00")
        self.assertEqual(next_id, first_id + 1)


if __name__ == "__main__":
    unittest.main()
