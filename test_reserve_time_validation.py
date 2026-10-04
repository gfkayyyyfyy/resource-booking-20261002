"""reserve 时间输入校验的回归测试（仅标准库，python -m unittest 可发现）。

通过公开命令行入口 `python -m booking --db <文件> <命令>` 准备数据并断言，
每个用例使用独立的临时 SQLite 数据库，结束后自动清理；
不依赖已有数据库、当前日期、机器时区或任何第三方库。
断言均基于解析后的 JSON 内容，不依赖输出对象的键顺序，也不预设资源标识。

覆盖的公开行为（固定 UTC+08:00，分钟级本地时间，定宽 YYYY-MM-DDTHH:mm）：
- 合法样例：2026-10-05T09:00–2026-10-05T10:00 与跨午夜的
  2028-02-29T23:30–2028-03-01T00:30（闰日）均可成功，返回正整数
  booking_id、正确的 resource_id 与原始起止文本，退出码 0，
  按日查询能读回完整时段；
- 拒绝样例：未零填充、日期与时间之间用空格、带秒、带 Z 或 +08:00 后缀、
  首尾空白、2026-02-29 无效日期、小时 24、分钟 60，--start 与 --end
  两个参数受到同样检查；开始等于结束或晚于结束同样被拒绝；
- 所有拒绝只在标准输出返回 {"error": "invalid_input"}，退出码 2，
  标准错误为空；非法时间搭配不存在的正整数资源标识仍返回 invalid_input；
  已有预约上使用带秒的相同时段也返回 invalid_input，不进入冲突结果；
- 拒绝不留数据变化：对尚不存在的数据库路径提交非法时间后文件仍不存在；
  对已有资源和预约的数据库连续提交拒绝样例后，资源目录与按日查询的
  解析结果与提交前一致；随后提交合法的相邻时段成功，booking_id 紧接
  原有最大预约标识，证明失败没有消耗标识。
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

# 固定的样例日期与时间（固定 UTC+08:00 的本地时间，与运行环境无关）。
DAY = "2026-10-05"
VALID_START = f"{DAY}T09:00"
VALID_END = f"{DAY}T10:00"

# 跨午夜的闰日样例：2028 是闰年，2028-02-29 合法。
LEAP_DAY = "2028-02-29"
LEAP_NEXT_DAY = "2028-03-01"
LEAP_START = f"{LEAP_DAY}T23:30"
LEAP_END = f"{LEAP_NEXT_DAY}T00:30"

# 各类非法时间文本：每一行都必须被 --start 与 --end 同样拒绝。
INVALID_TIME_SAMPLES = [
    ("日期未零填充", "2026-10-5T09:00"),
    ("月份未零填充", "2026-1-05T09:00"),
    ("小时未零填充", "2026-10-05T9:00"),
    ("分钟未零填充", "2026-10-05T09:0"),
    ("日期与时间之间用空格", "2026-10-05 09:00"),
    ("带秒", "2026-10-05T09:00:00"),
    ("带 Z 时区后缀", "2026-10-05T09:00Z"),
    ("带 +08:00 时区后缀", "2026-10-05T09:00+08:00"),
    ("首部空白", " 2026-10-05T09:00"),
    ("尾部空白", "2026-10-05T09:00 "),
    ("无效日期 2026-02-29", "2026-02-29T09:00"),
    ("小时 24", "2026-10-05T24:00"),
    ("分钟 60", "2026-10-05T09:60"),
]


class ReserveTimeValidationTestCase(unittest.TestCase):
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

    def add_resource(self, db=None, name="测试资源"):
        payload = self.run_ok("resource-add", "--name", name, db=db)
        self.assertIsInstance(payload["resource_id"], int)
        self.assertGreater(payload["resource_id"], 0)
        self.assertEqual(payload["name"], name)
        return payload["resource_id"]

    def reserve_ok(self, resource_id, start, end, db=None):
        """提交预约并断言成功：退出码 0，返回正确资源、原始起止文本与新的正整数标识。"""
        proc = self.run_cli(
            "reserve", "--resource", str(resource_id),
            "--start", start, "--end", end, db=db,
        )
        slot = f"{start}~{end}"
        self.assertEqual(
            proc.returncode, 0,
            msg=f"请求时段 {slot} 应成功（退出码 0）；实际 rc={proc.returncode} "
                f"stdout={proc.stdout!r} stderr={proc.stderr!r}",
        )
        payload = json.loads(proc.stdout)
        self.assertIsInstance(payload, dict)
        self.assertIsInstance(payload.get("booking_id"), int, msg=f"请求时段 {slot} 返回 {payload!r}")
        self.assertGreater(
            payload["booking_id"], 0,
            msg=f"请求时段 {slot} 的 booking_id 应为正整数；实际 {payload!r}",
        )
        # 字典相等性不依赖键序，同时固定返回字段集合与原始起止文本。
        self.assertEqual(
            payload,
            {
                "booking_id": payload["booking_id"],
                "resource_id": resource_id,
                "start": start,
                "end": end,
            },
            msg=f"请求时段 {slot} 的成功返回内容与公开约定不符：{payload!r}",
        )
        return payload["booking_id"]

    def assert_reserve_invalid_input(self, resource_id, start, end, db=None):
        """断言预约请求只返回 {"error": "invalid_input"}，退出码 2 且标准错误为空。"""
        proc = self.run_cli(
            "reserve", "--resource", str(resource_id),
            "--start", start, "--end", end, db=db,
        )
        slot = f"[{start}, {end})"
        self.assertEqual(
            proc.returncode, 2,
            msg=f"请求时段 {slot} 应以退出码 2 失败；实际 rc={proc.returncode} "
                f"stdout={proc.stdout!r} stderr={proc.stderr!r}",
        )
        self.assertEqual(
            proc.stderr, "",
            msg=f"请求时段 {slot} 被拒绝时标准错误应为空；实际 stderr={proc.stderr!r}",
        )
        payload = json.loads(proc.stdout)
        self.assertEqual(
            payload, {"error": "invalid_input"},
            msg=f"请求时段 {slot} 应返回 {{'error': 'invalid_input'}}；实际返回 {payload!r}",
        )

    def day_query(self, resource_id, date, db=None):
        """按日查询，返回完整解析结果（resource_id/date/bookings）。"""
        return self.run_ok(
            "day-query", "--resource", str(resource_id), "--date", date, db=db
        )

    def day_bookings(self, resource_id, date, db=None):
        payload = self.day_query(resource_id, date, db=db)
        self.assertEqual(payload["resource_id"], resource_id)
        self.assertEqual(payload["date"], date)
        return payload["bookings"]

    def resource_list(self, db=None):
        """资源目录查询，返回完整解析结果（仅含 resources 键）。"""
        payload = self.run_ok("resource-list", db=db)
        self.assertEqual(list(payload), ["resources"])
        return payload

    # ---- 合法样例：接受范围 ----

    def test_valid_same_day_slot_accepted(self):
        """2026-10-05T09:00–10:00 成功，按日查询读回完整时段。"""
        resource_id = self.add_resource()
        booking_id = self.reserve_ok(resource_id, VALID_START, VALID_END)

        bookings = self.day_bookings(resource_id, DAY)
        self.assertEqual(
            bookings,
            [{"booking_id": booking_id, "start": VALID_START, "end": VALID_END}],
        )

    def test_valid_leap_day_cross_midnight_slot_accepted(self):
        """2028-02-29T23:30–2028-03-01T00:30（闰日跨午夜）成功，两天查询均读回。"""
        resource_id = self.add_resource()
        booking_id = self.reserve_ok(resource_id, LEAP_START, LEAP_END)
        expected = {"booking_id": booking_id, "start": LEAP_START, "end": LEAP_END}

        # 跨日预约与两个日期都有交集，两个日期的查询都应包含完整时段。
        self.assertEqual(self.day_bookings(resource_id, LEAP_DAY), [expected])
        self.assertEqual(self.day_bookings(resource_id, LEAP_NEXT_DAY), [expected])

    # ---- 拒绝样例：--start 与 --end 受到同样检查 ----

    def test_invalid_time_formats_rejected_on_both_parameters(self):
        """每种非法时间文本出现在 --start 或 --end 上都被拒绝，且不改动任何记录。"""
        resource_id = self.add_resource()
        seed_id = self.reserve_ok(resource_id, VALID_START, VALID_END)
        before_list = self.resource_list()
        before_bookings = self.day_bookings(resource_id, DAY)

        for label, bad_time in INVALID_TIME_SAMPLES:
            for position, start, end in (
                ("start", bad_time, VALID_END),
                ("end", VALID_START, bad_time),
            ):
                with self.subTest(sample=label, parameter=position, value=bad_time):
                    self.assert_reserve_invalid_input(resource_id, start, end)

        # 全部拒绝之后：资源目录与按日查询的解析结果与提交前完全一致。
        self.assertEqual(
            self.resource_list(), before_list,
            msg=f"非法时间请求后资源目录发生变化：before={before_list!r}",
        )
        self.assertEqual(
            self.day_bookings(resource_id, DAY), before_bookings,
            msg=f"非法时间请求后按日查询结果发生变化：before={before_bookings!r}",
        )
        self.assertEqual(
            self.day_bookings(resource_id, DAY),
            [{"booking_id": seed_id, "start": VALID_START, "end": VALID_END}],
        )

    def test_start_equal_to_end_rejected(self):
        """开始等于结束被拒绝（两端均为合法格式）。"""
        resource_id = self.add_resource()
        self.assert_reserve_invalid_input(resource_id, VALID_START, VALID_START)
        self.assertEqual(self.day_bookings(resource_id, DAY), [])

    def test_start_after_end_rejected(self):
        """开始晚于结束被拒绝（两端均为合法格式）。"""
        resource_id = self.add_resource()
        self.assert_reserve_invalid_input(resource_id, VALID_END, VALID_START)
        self.assertEqual(self.day_bookings(resource_id, DAY), [])

    # ---- 校验优先级：先于资源存在与冲突判断 ----

    def test_invalid_time_with_nonexistent_resource_still_invalid_input(self):
        """非法时间搭配不存在的正整数资源标识，仍返回 invalid_input 而非 resource_not_found。"""
        resource_id = self.add_resource()
        nonexistent_id = resource_id + 1000  # 未登记的正整数标识
        for label, bad_time in INVALID_TIME_SAMPLES:
            with self.subTest(sample=label, value=bad_time):
                self.assert_reserve_invalid_input(nonexistent_id, bad_time, VALID_END)
                self.assert_reserve_invalid_input(nonexistent_id, VALID_START, bad_time)

    def test_invalid_time_takes_priority_over_conflict(self):
        """已有预约上使用带秒的相同时段，返回 invalid_input 而非 booking_conflict。"""
        resource_id = self.add_resource()
        seed_id = self.reserve_ok(resource_id, VALID_START, VALID_END)

        self.assert_reserve_invalid_input(
            resource_id, "2026-10-05T09:00:00", "2026-10-05T10:00:00"
        )

        # 原预约保持原样，且仍按原有时段参与冲突判断。
        self.assertEqual(
            self.day_bookings(resource_id, DAY),
            [{"booking_id": seed_id, "start": VALID_START, "end": VALID_END}],
        )

    # ---- 拒绝不留数据变化 ----

    def test_rejection_creates_no_database_file(self):
        """对尚不存在的数据库路径提交非法时间后，文件仍不存在。"""
        self.assertFalse(self.db_path.exists())
        for label, bad_time in INVALID_TIME_SAMPLES:
            with self.subTest(sample=label, value=bad_time):
                self.assert_reserve_invalid_input(1, bad_time, VALID_END)
                self.assertFalse(
                    self.db_path.exists(),
                    msg=f"非法时间样例 {label}（{bad_time!r}）不应创建数据库文件",
                )

    def test_rejections_do_not_consume_booking_ids(self):
        """连续拒绝后资源目录与按日查询不变，随后合法相邻时段的标识紧接原最大标识。"""
        resource_id = self.add_resource()
        seed_id = self.reserve_ok(resource_id, VALID_START, VALID_END)
        before_list = self.resource_list()
        before_bookings = self.day_bookings(resource_id, DAY)

        # 连续提交全部拒绝样例（两个参数位置各一遍，外加次序非法的两种）。
        for label, bad_time in INVALID_TIME_SAMPLES:
            self.assert_reserve_invalid_input(resource_id, bad_time, VALID_END)
            self.assert_reserve_invalid_input(resource_id, VALID_START, bad_time)
        self.assert_reserve_invalid_input(resource_id, VALID_START, VALID_START)
        self.assert_reserve_invalid_input(resource_id, VALID_END, VALID_START)

        # 资源目录与按日查询的解析结果与提交前一致。
        self.assertEqual(self.resource_list(), before_list)
        self.assertEqual(self.day_bookings(resource_id, DAY), before_bookings)

        # 随后提交合法的相邻时段成功，booking_id 紧接原有最大预约标识，
        # 证明全部失败请求都没有消耗标识。
        adjacent_id = self.reserve_ok(resource_id, f"{DAY}T10:00", f"{DAY}T11:00")
        self.assertEqual(
            adjacent_id, seed_id + 1,
            msg=f"拒绝样例不应消耗预约标识：原最大标识 {seed_id}，"
                f"随后合法预约应得 {seed_id + 1}，实际 {adjacent_id}",
        )
        self.assertEqual(
            self.day_bookings(resource_id, DAY),
            [
                {"booking_id": seed_id, "start": VALID_START, "end": VALID_END},
                {"booking_id": adjacent_id, "start": f"{DAY}T10:00", "end": f"{DAY}T11:00"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
