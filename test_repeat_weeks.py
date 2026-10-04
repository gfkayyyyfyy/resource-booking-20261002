"""reserve --repeat-weeks 每周重复预约的回归测试（仅标准库，python -m unittest 可发现）。

通过公开命令行入口 `python -m booking --db <文件> <命令>` 准备数据并断言，
每个用例使用独立的临时 SQLite 数据库，结束后自动清理；
不依赖已有数据库、当前日期、机器时区或任何第三方库。
断言均基于解析后的 JSON 内容，不依赖输出对象的键顺序。
"""

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# 项目根目录（booking 包所在目录），子进程以此作为工作目录，
# 保证 `python -m booking` 与测试运行时的当前目录无关。
PROJECT_ROOT = Path(__file__).resolve().parent

# 验收场景日期：首周 2026-10-05，次周 2026-10-12（固定 UTC+08:00）。
FIRST_DATE = "2026-10-05"
SECOND_DATE = "2026-10-12"


class ReserveRepeatWeeksTestCase(unittest.TestCase):
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
        self.assertEqual(proc.stderr, "")
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

    def reserve_repeat(self, resource_id, start, end, repeat_weeks):
        return self.run_ok(
            "reserve", "--resource", str(resource_id),
            "--start", start, "--end", end,
            "--repeat-weeks", repeat_weeks,
        )

    def day_query(self, resource_id, date):
        return self.run_ok(
            "day-query", "--resource", str(resource_id), "--date", date
        )

    # ---- 验收场景 ----

    def test_acceptance_conflict_then_success_after_cancel(self):
        """验收场景：次周已被占用时整批冲突；取消原预约后重试得到两个独立预约。"""
        resource_id = self.add_resource()
        original_id = self.reserve(
            resource_id, f"{SECOND_DATE}T09:00", f"{SECOND_DATE}T10:00"
        )

        # 请求 2026-10-05 起两次重复：第二周 2026-10-12 与原预约重叠 → 冲突。
        self.run_error(
            "booking_conflict",
            "reserve", "--resource", str(resource_id),
            "--start", f"{FIRST_DATE}T09:00", "--end", f"{FIRST_DATE}T10:00",
            "--repeat-weeks", "2",
        )
        # 首周无新增：2026-10-05 查询为空，原预约保持原样。
        self.assertEqual(
            self.day_query(resource_id, FIRST_DATE)["bookings"], []
        )
        self.assertEqual(
            self.day_query(resource_id, SECOND_DATE)["bookings"],
            [
                {
                    "booking_id": original_id,
                    "start": f"{SECOND_DATE}T09:00",
                    "end": f"{SECOND_DATE}T10:00",
                }
            ],
        )

        # 取消原预约后重试：得到两个独立预约。
        self.run_ok("cancel", "--booking", str(original_id))
        payload = self.reserve_repeat(
            resource_id, f"{FIRST_DATE}T09:00", f"{FIRST_DATE}T10:00", "2"
        )

        self.assertEqual(set(payload.keys()), {"resource_id", "bookings"})
        self.assertEqual(payload["resource_id"], resource_id)
        self.assertEqual(len(payload["bookings"]), 2)
        first, second = payload["bookings"]
        self.assertEqual(
            first,
            {
                "booking_id": first["booking_id"],
                "start": f"{FIRST_DATE}T09:00",
                "end": f"{FIRST_DATE}T10:00",
            },
        )
        self.assertEqual(
            second,
            {
                "booking_id": second["booking_id"],
                "start": f"{SECOND_DATE}T09:00",
                "end": f"{SECOND_DATE}T10:00",
            },
        )
        self.assertNotEqual(first["booking_id"], second["booking_id"])
        self.assertEqual(set(first.keys()), {"booking_id", "start", "end"})
        self.assertEqual(set(second.keys()), {"booking_id", "start", "end"})

        # 重开同一 SQLite 文件（每条命令都是独立进程）仍可分别查询。
        self.assertEqual(
            self.day_query(resource_id, FIRST_DATE)["bookings"], [first]
        )
        self.assertEqual(
            self.day_query(resource_id, SECOND_DATE)["bookings"], [second]
        )

    # ---- 成功路径 ----

    def test_success_shape_and_weekly_progression(self):
        """八次重复：端点逐周推进七个日历日，按发生时间升序，标识各自独立。"""
        resource_id = self.add_resource()

        payload = self.reserve_repeat(
            resource_id, "2026-10-05T09:00", "2026-10-05T10:00", "8"
        )

        self.assertEqual(set(payload.keys()), {"resource_id", "bookings"})
        bookings = payload["bookings"]
        self.assertEqual(len(bookings), 8)
        expected_dates = [
            "2026-10-05", "2026-10-12", "2026-10-19", "2026-10-26",
            "2026-11-02", "2026-11-09", "2026-11-16", "2026-11-23",
        ]
        for booking, date in zip(bookings, expected_dates):
            self.assertEqual(
                booking,
                {
                    "booking_id": booking["booking_id"],
                    "start": f"{date}T09:00",
                    "end": f"{date}T10:00",
                },
            )
        ids = [booking["booking_id"] for booking in bookings]
        self.assertEqual(len(set(ids)), 8)  # 各自独立
        self.assertEqual(ids, sorted(ids))  # 按发生时间升序分配

    def test_leading_zeros_accepted(self):
        """前导零按数值解释：\"002\" 即两次。"""
        resource_id = self.add_resource()

        payload = self.reserve_repeat(
            resource_id, f"{FIRST_DATE}T09:00", f"{FIRST_DATE}T10:00", "002"
        )

        self.assertEqual(len(payload["bookings"]), 2)

    def test_cross_midnight_interval_repeats_by_calendar_days(self):
        """跨日区间整体推进七个日历日，保持分钟精度。"""
        resource_id = self.add_resource()

        payload = self.reserve_repeat(
            resource_id, "2026-10-05T23:00", "2026-10-06T01:00", "2"
        )

        self.assertEqual(
            [(b["start"], b["end"]) for b in payload["bookings"]],
            [
                ("2026-10-05T23:00", "2026-10-06T01:00"),
                ("2026-10-12T23:00", "2026-10-13T01:00"),
            ],
        )

    def test_past_dates_allowed(self):
        """过去日期同样允许重复预约。"""
        resource_id = self.add_resource()

        payload = self.reserve_repeat(
            resource_id, "2020-01-06T09:00", "2020-01-06T10:00", "3"
        )

        self.assertEqual(
            [b["start"] for b in payload["bookings"]],
            ["2020-01-06T09:00", "2020-01-13T09:00", "2020-01-20T09:00"],
        )

    def test_exactly_seven_day_intervals_do_not_self_conflict(self):
        """时长恰好七天的区间端点相接，不算彼此重叠。"""
        resource_id = self.add_resource()

        payload = self.reserve_repeat(
            resource_id, "2026-10-05T09:00", "2026-10-12T09:00", "2"
        )

        self.assertEqual(
            [(b["start"], b["end"]) for b in payload["bookings"]],
            [
                ("2026-10-05T09:00", "2026-10-12T09:00"),
                ("2026-10-12T09:00", "2026-10-19T09:00"),
            ],
        )

    def test_touching_existing_booking_does_not_conflict(self):
        """与既有预约端点相接（左闭右开）不冲突。"""
        resource_id = self.add_resource()
        self.reserve(resource_id, f"{SECOND_DATE}T10:00", f"{SECOND_DATE}T11:00")

        payload = self.reserve_repeat(
            resource_id, f"{FIRST_DATE}T09:00", f"{FIRST_DATE}T10:00", "2"
        )

        self.assertEqual(len(payload["bookings"]), 2)

    def test_cancelled_and_other_resource_bookings_do_not_block(self):
        """已取消及其他资源的预约不阻挡重复预约。"""
        resource_id = self.add_resource()
        other_id = self.add_resource("其他资源")
        cancelled_id = self.reserve(
            resource_id, f"{SECOND_DATE}T09:00", f"{SECOND_DATE}T10:00"
        )
        self.run_ok("cancel", "--booking", str(cancelled_id))
        self.reserve(other_id, f"{SECOND_DATE}T09:00", f"{SECOND_DATE}T10:00")

        payload = self.reserve_repeat(
            resource_id, f"{FIRST_DATE}T09:00", f"{FIRST_DATE}T10:00", "2"
        )

        self.assertEqual(len(payload["bookings"]), 2)

    def test_repeat_bookings_are_plain_bookings_visible_and_cancellable(self):
        """重复预约持久化为普通预约：查询可见，cancel 只取消指定一项。"""
        resource_id = self.add_resource()
        payload = self.reserve_repeat(
            resource_id, f"{FIRST_DATE}T09:00", f"{FIRST_DATE}T10:00", "2"
        )
        first, second = payload["bookings"]

        # 取消其中第二项，只影响该项。
        self.run_ok("cancel", "--booking", str(second["booking_id"]))

        self.assertEqual(
            self.day_query(resource_id, FIRST_DATE)["bookings"], [first]
        )
        self.assertEqual(
            self.day_query(resource_id, SECOND_DATE)["bookings"], []
        )
        # 被取消的时段恢复可预约；首周仍被第一项占用。
        self.run_error(
            "booking_conflict",
            "reserve", "--resource", str(resource_id),
            "--start", f"{FIRST_DATE}T09:30", "--end", f"{FIRST_DATE}T10:30",
        )
        rebooked = self.reserve(
            resource_id, f"{SECOND_DATE}T09:00", f"{SECOND_DATE}T10:00"
        )
        self.assertGreater(rebooked, second["booking_id"])  # 不复用旧标识

    def test_single_reserve_output_unchanged_without_option(self):
        """省略 --repeat-weeks 时，单次预约的输出结构保持不变。"""
        resource_id = self.add_resource()

        payload = self.run_ok(
            "reserve", "--resource", str(resource_id),
            "--start", f"{FIRST_DATE}T09:00", "--end", f"{FIRST_DATE}T10:00",
        )

        self.assertEqual(
            payload,
            {
                "booking_id": payload["booking_id"],
                "resource_id": resource_id,
                "start": f"{FIRST_DATE}T09:00",
                "end": f"{FIRST_DATE}T10:00",
            },
        )

    # ---- 冲突路径：原子性 ----

    def test_self_overlapping_intervals_return_booking_conflict(self):
        """生成区间彼此重叠（时长大于七天）→ booking_conflict，不保存任何记录。"""
        resource_id = self.add_resource()

        self.run_error(
            "booking_conflict",
            "reserve", "--resource", str(resource_id),
            "--start", "2026-10-01T00:00", "--end", "2026-10-10T00:00",
            "--repeat-weeks", "2",
        )

        self.assertEqual(
            self.day_query(resource_id, "2026-10-01")["bookings"], []
        )
        # 未消耗预约标识：下一条预约仍从 1 开始。
        self.assertEqual(
            self.reserve(resource_id, "2026-10-01T00:00", "2026-10-01T01:00"), 1
        )

    def test_conflict_is_atomic_and_does_not_consume_ids(self):
        """任一次区间冲突即整批失败：不新增记录、不消耗预约标识、原记录不变。"""
        resource_id = self.add_resource()
        existing_id = self.reserve(
            resource_id, "2026-10-19T09:30", "2026-10-19T10:30"
        )

        # 三次重复中第三次（2026-10-19）与既有预约重叠。
        self.run_error(
            "booking_conflict",
            "reserve", "--resource", str(resource_id),
            "--start", f"{FIRST_DATE}T09:00", "--end", f"{FIRST_DATE}T10:00",
            "--repeat-weeks", "3",
        )

        self.assertEqual(
            self.day_query(resource_id, FIRST_DATE)["bookings"], []
        )
        self.assertEqual(
            self.day_query(resource_id, SECOND_DATE)["bookings"], []
        )
        self.assertEqual(
            self.day_query(resource_id, "2026-10-19")["bookings"],
            [
                {
                    "booking_id": existing_id,
                    "start": "2026-10-19T09:30",
                    "end": "2026-10-19T10:30",
                }
            ],
        )
        # 未消耗预约标识：下一条成功预约的标识紧接既有标识。
        self.assertEqual(
            self.reserve(resource_id, f"{FIRST_DATE}T12:00", f"{FIRST_DATE}T13:00"),
            existing_id + 1,
        )

    def test_conflict_failure_persists_nothing_after_reopen(self):
        """冲突失败后重开数据库，仍无任何新记录。"""
        resource_id = self.add_resource()
        self.reserve(resource_id, f"{SECOND_DATE}T09:00", f"{SECOND_DATE}T10:00")

        self.run_error(
            "booking_conflict",
            "reserve", "--resource", str(resource_id),
            "--start", f"{FIRST_DATE}T09:00", "--end", f"{FIRST_DATE}T10:00",
            "--repeat-weeks", "2",
        )

        # 独立进程重开同一文件验证。
        self.assertEqual(
            self.day_query(resource_id, FIRST_DATE)["bookings"], []
        )

    # ---- 失败路径：--repeat-weeks 非法值 ----

    def test_invalid_repeat_weeks_values_return_invalid_input(self):
        """越界、非 ASCII 数字、空白及其他字符：invalid_input，退出码 2。"""
        cases = [
            ("空文本", ""),
            ("零", "0"),
            ("纯零带前导零", "000"),
            ("次数 1（低于下限）", "1"),
            ("带前导零的 1", "01"),
            ("次数 9（高于上限）", "9"),
            ("远超上限", "88"),
            ("超长数字串", "9" * 5000),
            ("带前导零的超长数字串", "0" * 5000 + "9" * 5000),
            ("负数", "-2"),
            ("小数", "2.0"),
            ("前导空格", " 2"),
            ("尾随空格", "2 "),
            ("尾随换行", "2\n"),
            ("尾随回车", "2\r"),
            ("前导引号", "+2"),
            ("非数字文本", "abc"),
            ("全角数字", "２"),
            ("阿拉伯文数字", "٢"),
        ]
        for label, value in cases:
            with self.subTest(label=label):
                self.run_error(
                    "invalid_input",
                    "reserve", "--resource", "1",
                    "--start", f"{FIRST_DATE}T09:00", "--end", f"{FIRST_DATE}T10:00",
                    "--repeat-weeks", value,
                )

    def test_missing_repeat_weeks_value_returns_invalid_input(self):
        """--repeat-weeks 缺值（后面没有参数文本）：invalid_input。"""
        self.run_error(
            "invalid_input",
            "reserve", "--resource", "1",
            "--start", f"{FIRST_DATE}T09:00", "--end", f"{FIRST_DATE}T10:00",
            "--repeat-weeks",
        )

    def test_generated_endpoint_out_of_range_returns_invalid_input(self):
        """任一生成端点超出可表示日期范围：invalid_input。"""
        resource_id = self.add_resource()

        self.run_error(
            "invalid_input",
            "reserve", "--resource", str(resource_id),
            "--start", "9999-12-28T09:00", "--end", "9999-12-28T10:00",
            "--repeat-weeks", "2",
        )
        # 未新增任何记录。
        self.assertEqual(
            self.day_query(resource_id, "9999-12-28")["bookings"], []
        )

    def test_invalid_repeat_weeks_takes_priority_over_resource_not_found(self):
        """非法 --repeat-weeks 与不存在的资源同时出现时，优先 invalid_input。"""
        self.run_error(
            "invalid_input",
            "reserve", "--resource", "999",
            "--start", f"{FIRST_DATE}T09:00", "--end", f"{FIRST_DATE}T10:00",
            "--repeat-weeks", "1",
        )

    def test_invalid_repeat_weeks_takes_priority_over_time_window_errors(self):
        """非法 --repeat-weeks 与非法时间同时出现时，统一 invalid_input。"""
        self.run_error(
            "invalid_input",
            "reserve", "--resource", "1",
            "--start", f"{FIRST_DATE}T10:00", "--end", f"{FIRST_DATE}T09:00",
            "--repeat-weeks", "0",
        )

    def test_invalid_repeat_weeks_does_not_create_database_file(self):
        """非法 --repeat-weeks 指向尚不存在的数据库路径时，不得创建文件。"""
        missing_db = Path(self._tmpdir.name) / "should-not-exist.sqlite"
        self.run_error(
            "invalid_input",
            "reserve", "--resource", "1",
            "--start", f"{FIRST_DATE}T09:00", "--end", f"{FIRST_DATE}T10:00",
            "--repeat-weeks", "9",
            db=missing_db,
        )
        self.assertFalse(missing_db.exists())

    def test_invalid_repeat_weeks_does_not_migrate_legacy_database(self):
        """非法 --repeat-weeks 不迁移旧库：表结构与数据保持不变。"""
        legacy_db = Path(self._tmpdir.name) / "legacy.sqlite"
        conn = sqlite3.connect(legacy_db)
        conn.executescript(
            """
            CREATE TABLE resources (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL);
            CREATE TABLE bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                resource_id INTEGER NOT NULL,
                start TEXT NOT NULL,
                end TEXT NOT NULL,
                FOREIGN KEY (resource_id) REFERENCES resources(id)
            );
            INSERT INTO resources (id, name) VALUES (7, '旧资源');
            INSERT INTO bookings (id, resource_id, start, end)
            VALUES (21, 7, '2026-10-05T09:00', '2026-10-05T10:00');
            """
        )
        conn.commit()
        conn.close()

        self.run_error(
            "invalid_input",
            "reserve", "--resource", "7",
            "--start", f"{FIRST_DATE}T12:00", "--end", f"{FIRST_DATE}T13:00",
            "--repeat-weeks", "abc",
            db=legacy_db,
        )

        conn = sqlite3.connect(legacy_db)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(bookings)")}
            self.assertNotIn("cancelled", columns)  # 未补齐取消标记列
            rows = conn.execute(
                "SELECT id, resource_id, start, end FROM bookings"
            ).fetchall()
            self.assertEqual(rows, [(21, 7, "2026-10-05T09:00", "2026-10-05T10:00")])
        finally:
            conn.close()

    # ---- 失败路径：资源不存在 ----

    def test_valid_repeat_weeks_with_unknown_resource_returns_resource_not_found(self):
        """输入全部合法而资源不存在：resource_not_found，退出码 2。"""
        self.add_resource()
        self.run_error(
            "resource_not_found",
            "reserve", "--resource", "999",
            "--start", f"{FIRST_DATE}T09:00", "--end", f"{FIRST_DATE}T10:00",
            "--repeat-weeks", "2",
        )

    def test_valid_repeat_weeks_with_oversized_resource_returns_resource_not_found(self):
        """合法 --repeat-weeks 与超大正整数标识：resource_not_found。"""
        self.add_resource()
        self.run_error(
            "resource_not_found",
            "reserve", "--resource", "9223372036854775808",
            "--start", f"{FIRST_DATE}T09:00", "--end", f"{FIRST_DATE}T10:00",
            "--repeat-weeks", "2",
        )

    def test_resource_not_found_failure_does_not_create_bookings(self):
        """资源不存在时不新增任何记录。"""
        resource_id = self.add_resource()
        self.run_error(
            "resource_not_found",
            "reserve", "--resource", "999",
            "--start", f"{FIRST_DATE}T09:00", "--end", f"{FIRST_DATE}T10:00",
            "--repeat-weeks", "2",
        )
        # 未消耗预约标识：下一条预约仍从 1 开始。
        self.assertEqual(
            self.reserve(resource_id, f"{FIRST_DATE}T09:00", f"{FIRST_DATE}T10:00"),
            1,
        )


if __name__ == "__main__":
    unittest.main()
