"""日期/时间参数只接受 ASCII 数字的回归测试（仅标准库，python -m unittest 可发现、可单独执行）。

通过公开命令行入口 `python -m booking --db <文件> <命令>` 准备资源、提交预约并
读取结果，每个用例使用独立的临时 SQLite 数据库，结束后自动清理；不依赖已有数据库、
当前日期、机器时区或任何第三方库。断言均基于解析后的 JSON 内容，不依赖输出对象的
键顺序，也不预先猜测 resource_id / booking_id 的具体数值。

覆盖的公开行为（reserve/free-query 的起止为 YYYY-MM-DDTHH:mm，day-query 的日期为
YYYY-MM-DD，所有数字位只接受 ASCII 0-9）：
- 全角数字（如 ２０２６）、阿拉伯文数字（如 ٢٠٢٦）及其与 ASCII 数字混写的
  年份/月/日/时/分文本，在 reserve 的开始与结束、day-query 的日期、free-query
  的开始与结束上一律返回 {"error": "invalid_input"}、退出码 2、stderr 为空，
  不自动转换为合法时间；起止参数分别检查，一端合法不能放行另一端；
- 同一规则适用于带 --repeat-weeks 的预约与带 --min-minutes 的空闲查询
  （这两个数值参数本就只接受 ASCII 数字，此处固定其与时间参数一致的行为）；
- 非法输入先于资源存在性（含超过 SQLite 整数范围的标识）与预约冲突判断；
- 失败请求不创建数据库文件、不新增记录、不消耗预约标识；随后用正常 ASCII
  日期查询，原预约的标识与完整起止时间保持不变；
- 合法 ASCII 时间行为不变：普通重叠仍返回 booking_conflict，闰日、过去日期、
  跨午夜与左闭右开规则不受影响；资源/预约标识继续允许 Unicode 十进制数字。
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

# 验收场景使用的合法对照文本。
VALID_START = "2026-10-05T09:00"
VALID_END = "2026-10-05T10:00"
DAY = "2026-10-05"

# 含非 ASCII 数字的非法时间文本：(说明, 文本)。
# 结构、零填充与取值都与合法文本一致，唯一问题是数字写法。
NON_ASCII_DIGIT_TIMES = [
    ("全角年份", "２０２６-10-05T09:00"),
    ("全角年份（验收样例的结束端点写法）", "２０２６-10-05T10:00"),
    ("阿拉伯文年份", "٢٠٢٦-10-05T09:00"),
    ("全角与 ASCII 混写的年份", "2０26-10-05T09:00"),
    ("阿拉伯文与 ASCII 混写的年份", "2٠26-10-05T09:00"),
    ("全角月份", "2026-１０-05T09:00"),
    ("全角日", "2026-10-０５T09:00"),
    ("全角小时", "2026-10-05T０９:00"),
    ("全角分钟", "2026-10-05T09:００"),
    ("阿拉伯文小时与分钟", "2026-10-05T٠٩:٠٠"),
]

# 含非 ASCII 数字的非法日期文本：(说明, 文本)。
NON_ASCII_DIGIT_DATES = [
    ("全角年份", "２０２６-10-05"),
    ("阿拉伯文年份", "٢٠٢٦-10-05"),
    ("全角与 ASCII 混写的年份", "20２6-10-05"),
    ("全角月份", "2026-１０-05"),
    ("全角日", "2026-10-０５"),
]


class UnicodeDigitTimeTestCase(unittest.TestCase):
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

    def test_fullwidth_variants_rejected_and_original_booking_unchanged(self):
        """对已有预约提交全角起止、用全角日期查询均拒绝；正常查询结果不变。"""
        resource_id = self.add_resource()
        booking_id = self.reserve_ok(resource_id, VALID_START, VALID_END)

        # 与已有预约同一时段、仅年份写成全角：必须拒绝而非绕过冲突判断。
        self.assert_invalid_input(
            "reserve", "--resource", str(resource_id),
            "--start", "２０２６-10-05T09:00", "--end", "２０２６-10-05T10:00",
        )
        # 同一天的全角日期查询：必须拒绝而非漏掉或另算一天。
        self.assert_invalid_input(
            "day-query", "--resource", str(resource_id), "--date", "２０２６-10-05",
        )

        # 随后用正常 ASCII 日期查询：原预约的标识与完整起止时间保持不变。
        payload = self.day_query(resource_id, DAY)
        self.assertEqual(
            payload,
            {
                "resource_id": resource_id,
                "date": DAY,
                "bookings": [
                    {"booking_id": booking_id,
                     "start": VALID_START, "end": VALID_END}
                ],
            },
            msg=f"全角输入被拒后按日查询结果发生变化：{payload!r}",
        )

    # ---- reserve 起止参数分别检查 ----

    def test_non_ascii_digit_times_rejected_for_start_and_end(self):
        """每类非 ASCII 数字时间分别作为开始、作为结束提交，均为 invalid_input。"""
        resource_id = self.add_resource()
        for label, bad in NON_ASCII_DIGIT_TIMES:
            for position in ("start", "end"):
                with self.subTest(label=label, position=position, value=bad):
                    if position == "start":
                        self.assert_invalid_input(
                            "reserve", "--resource", str(resource_id),
                            "--start", bad, "--end", VALID_END,
                        )
                    else:
                        self.assert_invalid_input(
                            "reserve", "--resource", str(resource_id),
                            "--start", VALID_START, "--end", bad,
                        )

    def test_non_ascii_digit_times_rejected_with_repeat_weeks(self):
        """带 --repeat-weeks 的预约对非 ASCII 数字时间同样拒绝。"""
        resource_id = self.add_resource()
        for label, bad in NON_ASCII_DIGIT_TIMES:
            with self.subTest(label=label, value=bad):
                self.assert_invalid_input(
                    "reserve", "--resource", str(resource_id),
                    "--start", bad, "--end", VALID_END, "--repeat-weeks", "2",
                )
                self.assert_invalid_input(
                    "reserve", "--resource", str(resource_id),
                    "--start", VALID_START, "--end", bad, "--repeat-weeks", "2",
                )

    # ---- day-query 日期参数 ----

    def test_non_ascii_digit_dates_rejected_for_day_query(self):
        """每类非 ASCII 数字日期用于 day-query 均为 invalid_input。"""
        resource_id = self.add_resource()
        for label, bad in NON_ASCII_DIGIT_DATES:
            with self.subTest(label=label, value=bad):
                self.assert_invalid_input(
                    "day-query", "--resource", str(resource_id), "--date", bad,
                )

    # ---- free-query 起止端点 ----

    def test_non_ascii_digit_times_rejected_for_free_query(self):
        """free-query 的开始与结束端点分别检查，含 --min-minutes 时同样拒绝。"""
        resource_id = self.add_resource()
        for label, bad in NON_ASCII_DIGIT_TIMES:
            for position in ("start", "end"):
                with self.subTest(label=label, position=position, value=bad):
                    if position == "start":
                        start, end = bad, VALID_END
                    else:
                        start, end = VALID_START, bad
                    self.assert_invalid_input(
                        "free-query", "--resource", str(resource_id),
                        "--start", start, "--end", end,
                    )
                    self.assert_invalid_input(
                        "free-query", "--resource", str(resource_id),
                        "--start", start, "--end", end, "--min-minutes", "30",
                    )

    # ---- 数值参数本就只有 ASCII 规则：固定其与时间参数一致的行为 ----

    def test_non_ascii_digit_repeat_weeks_and_min_minutes_rejected(self):
        """--repeat-weeks 与 --min-minutes 的非 ASCII 数字文本同样 invalid_input。"""
        resource_id = self.add_resource()
        for label, bad in [("全角 2", "２"), ("阿拉伯文 2", "٢"), ("混写 02", "0２")]:
            with self.subTest(label=label, value=bad):
                self.assert_invalid_input(
                    "reserve", "--resource", str(resource_id),
                    "--start", VALID_START, "--end", VALID_END,
                    "--repeat-weeks", bad,
                )
                self.assert_invalid_input(
                    "free-query", "--resource", str(resource_id),
                    "--start", VALID_START, "--end", VALID_END,
                    "--min-minutes", bad,
                )

    # ---- 拒绝优先级：先于资源存在性（含超大标识）与冲突判断 ----

    def test_invalid_input_takes_priority_over_resource_and_conflict(self):
        """非 ASCII 数字时间搭配不存在/超大标识资源或本会冲突的时段仍 invalid_input。"""
        resource_id = self.add_resource()
        booking_id = self.reserve_ok(resource_id, VALID_START, VALID_END)
        oversized = str(2 ** 63)  # 超过 SQLite INTEGER 上限的合法正整数标识

        for label, resource in [
            ("不存在的正整数资源", "999"),
            ("超过 SQLite 整数范围的标识", oversized),
        ]:
            with self.subTest(label=label):
                self.assert_invalid_input(
                    "reserve", "--resource", resource,
                    "--start", "２０２６-10-05T09:00", "--end", VALID_END,
                )
                self.assert_invalid_input(
                    "day-query", "--resource", resource, "--date", "２０２６-10-05",
                )
                self.assert_invalid_input(
                    "free-query", "--resource", resource,
                    "--start", VALID_START, "--end", "２０２６-10-05T10:00",
                )

        # 与已有预约同一时段、仅年份写成全角：返回 invalid_input 而非冲突。
        self.assert_invalid_input(
            "reserve", "--resource", str(resource_id),
            "--start", "２０２６-10-05T09:00", "--end", "２０２６-10-05T10:00",
        )
        payload = self.day_query(resource_id, DAY)
        self.assertEqual(
            payload["bookings"],
            [{"booking_id": booking_id, "start": VALID_START, "end": VALID_END}],
            msg=f"非法的相同时段请求后按日查询结果发生变化：{payload!r}",
        )

    # ---- 拒绝不留下数据变化 ----

    def test_invalid_requests_never_create_database_file(self):
        """对尚不存在的数据库路径提交非 ASCII 数字输入，文件不被创建。"""
        missing_db = self.db_path_for("should-not-exist.sqlite")
        commands = [
            ("reserve", "--resource", "1",
             "--start", "２０２６-10-05T09:00", "--end", VALID_END),
            ("reserve", "--resource", "1",
             "--start", VALID_START, "--end", "2026-10-05T10:００",
             "--repeat-weeks", "2"),
            ("day-query", "--resource", "1", "--date", "٢٠٢٦-10-05"),
            ("free-query", "--resource", "1",
             "--start", "2026-１０-05T09:00", "--end", VALID_END),
            ("free-query", "--resource", "1",
             "--start", VALID_START, "--end", VALID_END, "--min-minutes", "３０"),
        ]
        for args in commands:
            with self.subTest(args=args):
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

    def test_rejections_do_not_add_records_or_consume_booking_ids(self):
        """连续拒绝后按日查询不变；随后合法预约获得紧接原有最大标识的标识。"""
        resource_id = self.add_resource()
        seed_id = self.reserve_ok(resource_id, VALID_START, VALID_END)
        day_before = self.day_query(resource_id, DAY)

        for label, bad in NON_ASCII_DIGIT_TIMES:
            self.assert_invalid_input(
                "reserve", "--resource", str(resource_id),
                "--start", bad, "--end", VALID_END,
            )
        for label, bad in NON_ASCII_DIGIT_DATES:
            self.assert_invalid_input(
                "day-query", "--resource", str(resource_id), "--date", bad,
            )

        self.assertEqual(
            self.day_query(resource_id, DAY), day_before,
            msg="连续拒绝后按日查询结果发生变化",
        )

        # 新标识必须紧接原有最大标识，证明失败请求没有消耗 AUTOINCREMENT 标识。
        adjacent_id = self.reserve_ok(
            resource_id, "2026-10-05T10:00", "2026-10-05T11:00"
        )
        self.assertEqual(
            adjacent_id, seed_id + 1,
            msg=f"拒绝样例后合法预约的 booking_id 预期紧接 {seed_id}，"
                f"实际为 {adjacent_id}，说明失败请求消耗了标识",
        )

    # ---- 合法行为不变 ----

    def test_ascii_rules_unchanged(self):
        """ASCII 时间的冲突、闰日、过去日期、跨午夜与左闭右开规则不受影响。"""
        resource_id = self.add_resource()
        # 普通重叠仍返回 booking_conflict（左闭右开）。
        self.reserve_ok(resource_id, VALID_START, VALID_END)
        proc = self.run_cli(
            "reserve", "--resource", str(resource_id),
            "--start", "2026-10-05T09:30", "--end", "2026-10-05T10:30",
        )
        self.assertEqual(json.loads(proc.stdout), {"error": "booking_conflict"})
        self.assertEqual(proc.returncode, 2)
        # 端点相接放行。
        self.reserve_ok(resource_id, "2026-10-05T10:00", "2026-10-05T11:00")
        # 有效闰日、过去日期与跨午夜窗口仍合法。
        self.reserve_ok(resource_id, "2028-02-29T23:30", "2028-03-01T00:30")
        self.reserve_ok(resource_id, "2020-01-01T09:00", "2020-01-01T10:00")

    def test_unicode_digit_identifiers_still_accepted(self):
        """资源/预约标识继续允许 Unicode 十进制数字与前导零（按数值解释）。"""
        resource_id = self.add_resource()
        booking_id = self.reserve_ok(resource_id, VALID_START, VALID_END)

        # 全角数字标识按数值解释，指向同一资源。
        fullwidth_id = "".join(chr(0xFF10 + int(d)) for d in str(resource_id))
        payload = self.day_query(fullwidth_id, DAY)
        self.assertEqual(
            payload["bookings"],
            [{"booking_id": booking_id, "start": VALID_START, "end": VALID_END}],
            msg=f"全角数字资源标识的查询结果不符：{payload!r}",
        )
        # 阿拉伯文数字标识按数值解释，可正常取消预约。
        arabic_id = "".join(chr(0x0660 + int(d)) for d in str(booking_id))
        payload = self.run_ok("cancel", "--booking", arabic_id)
        self.assertEqual(payload, {"booking_id": booking_id, "cancelled": True})


if __name__ == "__main__":
    unittest.main()
