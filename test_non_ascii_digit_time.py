"""日期/时间参数仅接受 ASCII 数字的回归测试（仅标准库，python -m unittest 可发现）。

通过公开命令行入口 `python -m booking --db <文件> <命令>` 准备资源、提交预约并
读取结果，每个用例使用独立的临时 SQLite 数据库，结束后自动清理；不依赖已有数据库、
当前日期、机器时区或任何第三方库。断言均基于解析后的 JSON 内容，不依赖输出对象的
键顺序，也不预先猜测 resource_id / booking_id 的具体数值。

覆盖的公开行为（reserve/free-query 的起止为 YYYY-MM-DDTHH:mm，day-query 的日期为
YYYY-MM-DD，所有数字只能是 ASCII 0-9）：
- 全角数字、阿拉伯文数字及其与 ASCII 数字混写的时间/日期文本一律按非法输入拒绝，
  不自动转换；reserve 的开始与结束、free-query 的开始与结束分别检查，
  不因另一个端点合法而放行；
- 该规则同样适用于带 --repeat-weeks 的预约与带 --min-minutes 的空闲查询；
- 所有拒绝请求只在标准输出返回 {"error": "invalid_input"}，退出码 2，标准错误为空；
- 校验先于资源存在性与预约冲突判断：搭配不存在的资源标识或超过 SQLite 整数范围的
  标识结果相同；指向尚不存在的数据库路径时文件不被创建；
- 拒绝请求不留下任何数据变化：已有预约的标识与完整起止时间保持不变，失败不消耗
  预约标识；
- 合法 ASCII 时间行为不变：普通重叠请求仍返回 booking_conflict，有效闰日、
  过去日期与跨午夜时段仍可预约并按日读回。
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

# 一组合法的对照起止文本与查询日期。
VALID_START = "2026-10-05T09:00"
VALID_END = "2026-10-05T10:00"
DAY = "2026-10-05"

# 非 ASCII 数字的时间文本：(说明, 文本)。相对合法样例只替换数字写法，
# 其余结构（零填充、分隔符、取值范围）都合法，确保拒绝仅由数字写法触发。
FULLWIDTH_YEAR = "２０２６-10-05T09:00"
FULLWIDTH_YEAR_END = "２０２６-10-05T10:00"
NON_ASCII_TIMES = [
    ("全角年份", FULLWIDTH_YEAR),
    ("全角月份", "2026-１０-05T09:00"),
    ("全角日", "2026-10-０５T09:00"),
    ("全角小时", "2026-10-05T０９:00"),
    ("全角分钟", "2026-10-05T09:００"),
    ("阿拉伯文数字年份", "٢٠٢٦-10-05T09:00"),
    ("阿拉伯文数字小时", "2026-10-05T٠٩:00"),
    ("全角与 ASCII 混写年份", "2０26-10-05T09:00"),
    ("全角与 ASCII 混写分钟", "2026-10-05T09:0０"),
]

# 非 ASCII 数字的日期文本：(说明, 文本)。
NON_ASCII_DATES = [
    ("全角年份", "２０２６-10-05"),
    ("全角月份", "2026-１０-05"),
    ("全角日", "2026-10-０５"),
    ("阿拉伯文数字年份", "٢٠٢٦-10-05"),
    ("全角与 ASCII 混写日", "2026-10-0５"),
]


class NonAsciiDigitTimeTestCase(unittest.TestCase):
    """每个用例一个独立的临时目录和数据库路径，tearDown 自动清理。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="booking-test-")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "test.sqlite"

    def db_path_for(self, name):
        """同一临时目录内再取一个独立数据库路径（供需要彼此隔离的子用例）。"""
        return Path(self._tmpdir.name) / name

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
        self.assertEqual(
            proc.returncode, 0,
            msg=f"命令 {args!r} 预期退出码 0，实际 {proc.returncode}；"
                f"stdout={proc.stdout!r} stderr={proc.stderr!r}",
        )
        payload = json.loads(proc.stdout)  # 若输出不是单个 JSON 文档会抛错
        self.assertIsInstance(payload, dict)
        return payload

    def assert_invalid_input(self, *args, db=None):
        """断言命令只返回 {"error": "invalid_input"}：rc=2、stderr 为空。"""
        proc = self.run_cli(*args, db=db)
        self.assertEqual(
            proc.returncode, 2,
            msg=f"命令 {args!r} 预期退出码 2，实际 {proc.returncode}；"
                f"stdout={proc.stdout!r} stderr={proc.stderr!r}",
        )
        self.assertEqual(
            proc.stderr, "",
            msg=f"命令 {args!r} 被拒时 stderr 预期为空字符串，实际 {proc.stderr!r}",
        )
        try:
            payload = json.loads(proc.stdout)
        except ValueError:
            self.fail(
                f"命令 {args!r} 预期 stdout 为单个 JSON 对象 "
                f"{{'error': 'invalid_input'}}，实际 stdout={proc.stdout!r}"
            )
        self.assertEqual(
            payload, {"error": "invalid_input"},
            msg=f"命令 {args!r} 预期返回 {{'error': 'invalid_input'}}，"
                f"实际返回 {payload!r}",
        )

    def add_resource(self, name="测试资源", db=None):
        payload = self.run_ok("resource-add", "--name", name, db=db)
        self.assertIsInstance(payload["resource_id"], int)
        self.assertGreater(payload["resource_id"], 0)
        return payload["resource_id"]

    def reserve_ok(self, resource_id, start, end, db=None):
        payload = self.run_ok(
            "reserve", "--resource", str(resource_id),
            "--start", start, "--end", end, db=db,
        )
        self.assertIsInstance(payload["booking_id"], int)
        self.assertGreater(payload["booking_id"], 0)
        self.assertEqual(payload["resource_id"], resource_id)
        self.assertEqual(payload["start"], start)
        self.assertEqual(payload["end"], end)
        return payload["booking_id"]

    def day_query(self, resource_id, date, db=None):
        return self.run_ok(
            "day-query", "--resource", str(resource_id), "--date", date, db=db,
        )

    # ---- 验收场景：全角写法不能绕过冲突判断或漏掉查询结果 ----

    def test_fullwidth_year_reserve_and_day_query_rejected(self):
        """已登记资源与 09:00-10:00 预约：全角年份的相同时段与按日查询都被拒。"""
        resource_id = self.add_resource()
        booking_id = self.reserve_ok(resource_id, VALID_START, VALID_END)

        # 与已有预约同一时段、仅年份为全角写法：必须按非法输入拒绝，
        # 不能作为新预约写入（否则会绕过冲突判断形成重复时段）。
        self.assert_invalid_input(
            "reserve", "--resource", str(resource_id),
            "--start", FULLWIDTH_YEAR, "--end", FULLWIDTH_YEAR_END,
        )
        # 全角年份的按日查询同样拒绝，不能返回空结果掩盖已有预约。
        self.assert_invalid_input(
            "day-query", "--resource", str(resource_id), "--date", "２０２６-10-05",
        )

        # 随后用正常日期查询：原预约的标识与完整起止时间保持不变。
        payload = self.day_query(resource_id, DAY)
        self.assertEqual(
            payload,
            {
                "resource_id": resource_id,
                "date": DAY,
                "bookings": [
                    {"booking_id": booking_id,
                     "start": VALID_START, "end": VALID_END},
                ],
            },
            msg=f"全角输入被拒后按日查询结果发生变化：{payload!r}",
        )

    # ---- reserve：开始与结束分别检查 ----

    def test_non_ascii_digit_times_rejected_for_start_and_end(self):
        """每类非 ASCII 数字时间文本分别作为开始、作为结束提交，均为 invalid_input。"""
        resource_id = self.add_resource()
        for label, bad in NON_ASCII_TIMES:
            for position in ("start", "end"):
                with self.subTest(label=label, position=position, value=bad):
                    if position == "start":
                        args = ("--start", bad, "--end", VALID_END)
                    else:
                        # 结束端点用同一写法的 10:00 保持“仅数字写法非法”。
                        args = ("--start", VALID_START, "--end", bad)
                    self.assert_invalid_input(
                        "reserve", "--resource", str(resource_id), *args,
                    )

    def test_non_ascii_digit_times_rejected_with_repeat_weeks(self):
        """带 --repeat-weeks 的预约对非 ASCII 数字时间同样拒绝。"""
        resource_id = self.add_resource()
        for label, bad in NON_ASCII_TIMES:
            with self.subTest(label=label, value=bad):
                self.assert_invalid_input(
                    "reserve", "--resource", str(resource_id),
                    "--start", bad, "--end", VALID_END,
                    "--repeat-weeks", "2",
                )
                self.assert_invalid_input(
                    "reserve", "--resource", str(resource_id),
                    "--start", VALID_START, "--end", bad,
                    "--repeat-weeks", "8",
                )

    # ---- day-query：日期只接受 ASCII 数字 ----

    def test_non_ascii_digit_dates_rejected_for_day_query(self):
        """每类非 ASCII 数字日期文本用于按日查询，均为 invalid_input。"""
        resource_id = self.add_resource()
        for label, bad in NON_ASCII_DATES:
            with self.subTest(label=label, value=bad):
                self.assert_invalid_input(
                    "day-query", "--resource", str(resource_id), "--date", bad,
                )

    # ---- free-query：开始与结束分别检查（含 --min-minutes） ----

    def test_non_ascii_digit_times_rejected_for_free_query(self):
        """free-query 的开始与结束端点分别拒绝非 ASCII 数字时间文本。"""
        resource_id = self.add_resource()
        for label, bad in NON_ASCII_TIMES:
            for position in ("start", "end"):
                with self.subTest(label=label, position=position, value=bad):
                    if position == "start":
                        args = ("--start", bad, "--end", VALID_END)
                    else:
                        args = ("--start", VALID_START, "--end", bad)
                    self.assert_invalid_input(
                        "free-query", "--resource", str(resource_id), *args,
                    )

    def test_non_ascii_digit_times_rejected_for_free_query_with_min_minutes(self):
        """带 --min-minutes 的空闲查询对非 ASCII 数字时间同样拒绝。"""
        resource_id = self.add_resource()
        self.assert_invalid_input(
            "free-query", "--resource", str(resource_id),
            "--start", FULLWIDTH_YEAR, "--end", VALID_END,
            "--min-minutes", "30",
        )
        self.assert_invalid_input(
            "free-query", "--resource", str(resource_id),
            "--start", VALID_START, "--end", FULLWIDTH_YEAR_END,
            "--min-minutes", "1440",
        )

    # ---- 拒绝优先级：先于资源存在性与标识范围判断 ----

    def test_invalid_input_takes_priority_over_resource_checks(self):
        """非 ASCII 数字时间搭配不存在或超范围的资源标识，仍只返回 invalid_input。"""
        # 999 为正整数但在该库中不存在；9223372036854775808 超过 SQLite 整数上限。
        for resource in ("999", "9223372036854775808"):
            with self.subTest(resource=resource, command="reserve"):
                self.assert_invalid_input(
                    "reserve", "--resource", resource,
                    "--start", FULLWIDTH_YEAR, "--end", VALID_END,
                )
            with self.subTest(resource=resource, command="day-query"):
                self.assert_invalid_input(
                    "day-query", "--resource", resource, "--date", "２０２６-10-05",
                )
            with self.subTest(resource=resource, command="free-query"):
                self.assert_invalid_input(
                    "free-query", "--resource", resource,
                    "--start", VALID_START, "--end", FULLWIDTH_YEAR_END,
                )

    # ---- 拒绝不留下数据变化 ----

    def test_invalid_requests_never_create_database_file(self):
        """对尚不存在的数据库路径提交非 ASCII 数字输入，文件不被创建。"""
        missing_db = self.db_path_for("should-not-exist.sqlite")
        commands = [
            ("reserve", "--resource", "1",
             "--start", FULLWIDTH_YEAR, "--end", VALID_END),
            ("reserve", "--resource", "1",
             "--start", "٢٠٢٦-10-05T09:00", "--end", VALID_END,
             "--repeat-weeks", "2"),
            ("day-query", "--resource", "1", "--date", "２０２６-10-05"),
            ("free-query", "--resource", "1",
             "--start", VALID_START, "--end", FULLWIDTH_YEAR_END,
             "--min-minutes", "30"),
        ]
        for args in commands:
            with self.subTest(command=args[0]):
                self.assert_invalid_input(*args, db=missing_db)
                self.assertFalse(
                    missing_db.exists(),
                    msg=f"非法请求 {args!r} 不应创建数据库文件 {missing_db}",
                )
                leftovers = list(Path(self._tmpdir.name).iterdir())
                self.assertEqual(
                    leftovers, [],
                    msg=f"非法请求 {args!r} 在临时目录留下了文件："
                        f"{[str(p) for p in leftovers]}",
                )

    def test_rejections_leave_state_unchanged_and_do_not_consume_booking_id(self):
        """连续拒绝后按日查询与空闲查询不变；随后合法预约获得紧接的标识。"""
        resource_id = self.add_resource()
        seed_id = self.reserve_ok(resource_id, VALID_START, VALID_END)

        day_before = self.day_query(resource_id, DAY)
        free_before = self.run_ok(
            "free-query", "--resource", str(resource_id),
            "--start", VALID_END, "--end", "2026-10-05T12:00",
        )

        # 连续提交各类非 ASCII 数字的拒绝样例（含重复预约与带过滤的空闲查询）。
        self.assert_invalid_input(
            "reserve", "--resource", str(resource_id),
            "--start", FULLWIDTH_YEAR, "--end", FULLWIDTH_YEAR_END,
        )
        self.assert_invalid_input(
            "reserve", "--resource", str(resource_id),
            "--start", "2026-10-05T10:００", "--end", "2026-10-05T11:00",
            "--repeat-weeks", "3",
        )
        self.assert_invalid_input(
            "day-query", "--resource", str(resource_id), "--date", "2026-10-０５",
        )
        self.assert_invalid_input(
            "free-query", "--resource", str(resource_id),
            "--start", "2026-10-05T09:0０", "--end", "2026-10-05T12:00",
            "--min-minutes", "60",
        )

        # 已有数据与查询结果完全不变。
        self.assertEqual(
            self.day_query(resource_id, DAY), day_before,
            msg="连续拒绝后按日查询结果发生变化",
        )
        self.assertEqual(
            self.run_ok(
                "free-query", "--resource", str(resource_id),
                "--start", VALID_END, "--end", "2026-10-05T12:00",
            ),
            free_before,
            msg="连续拒绝后空闲查询结果发生变化",
        )

        # 新标识紧接原有最大预约标识，证明失败请求没有消耗 AUTOINCREMENT 标识。
        adjacent_id = self.reserve_ok(
            resource_id, "2026-10-05T10:00", "2026-10-05T11:00"
        )
        self.assertEqual(
            adjacent_id, seed_id + 1,
            msg=f"拒绝样例后合法预约的 booking_id 预期为 {seed_id + 1}，"
                f"实际为 {adjacent_id}，说明失败请求消耗了标识",
        )

    # ---- 合法 ASCII 行为保持不变 ----

    def test_ascii_overlap_still_returns_booking_conflict(self):
        """合法 ASCII 写法的普通重叠请求仍返回 booking_conflict。"""
        resource_id = self.add_resource()
        self.reserve_ok(resource_id, VALID_START, VALID_END)
        proc = self.run_cli(
            "reserve", "--resource", str(resource_id),
            "--start", "2026-10-05T09:30", "--end", "2026-10-05T10:30",
        )
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr, "")
        self.assertEqual(json.loads(proc.stdout), {"error": "booking_conflict"})

    def test_valid_ascii_times_still_accepted(self):
        """有效闰日、过去日期与跨午夜时段仍可预约并按日读回（左闭右开不变）。"""
        resource_id = self.add_resource()
        cases = [
            ("闰日跨午夜", "2028-02-29T23:30", "2028-03-01T00:30",
             ["2028-02-29", "2028-03-01"]),
            ("过去日期", "2020-01-01T08:00", "2020-01-01T09:00", ["2020-01-01"]),
        ]
        for label, start, end, dates in cases:
            with self.subTest(label=label):
                booking_id = self.reserve_ok(resource_id, start, end)
                for date in dates:
                    payload = self.day_query(resource_id, date)
                    self.assertEqual(
                        payload["bookings"],
                        [{"booking_id": booking_id, "start": start, "end": end}],
                        msg=f"{label} 按日查询 {date} 的结果不符：{payload!r}",
                    )
        # 左闭右开：端点相接的时段不算重叠，仍可预约。
        adjacent_id = self.reserve_ok(
            resource_id, "2028-03-01T00:30", "2028-03-01T01:30"
        )
        self.assertIsInstance(adjacent_id, int)


if __name__ == "__main__":
    unittest.main()
