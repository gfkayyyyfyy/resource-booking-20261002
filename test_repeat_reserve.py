"""reserve --repeat-weeks 每周重复预约的回归测试（仅标准库，python -m unittest 可发现）。

通过公开命令行入口 `python -m booking --db <文件> <命令>` 准备数据并断言，
每个用例使用独立的临时 SQLite 数据库，结束后自动清理；
不依赖已有数据库、当前日期、机器时区或任何第三方库。
断言均基于解析后的 JSON 内容，不依赖输出对象的键顺序，也不预设 booking_id。

覆盖的公开行为（固定 UTC+08:00）：
- 验收主流程：预先占用第 2 周时整批冲突且首周无新增，取消原预约后重试
  得到两个独立且不复用的标识，重开同一 SQLite 文件后可分别按日查询，
  cancel 只取消指定一项，无组标识或组取消；
- 成功返回只含 resource_id 与 bookings，bookings 按发生时间升序，每项只含
  booking_id、start、end，首项使用原起止文本，后续端点分别推进七个本地
  日历日并保持分钟精度（允许过去日期与跨日区间）；
- --repeat-weeks 只接受 2 至 8 的 ASCII 十进制整数文本（允许前导零），
  缺值、空文本、零、1、9、负数、小数、空白、非 ASCII 数字及其他字符、
  超长数字串，以及任一生成端点超出现有日期范围，一律 invalid_input，
  与标识和时间校验一起先于资源检查，不建库、不迁移、不改动数据；
- 整批全部成功或全部失败：同资源未取消预约与任一次区间重叠、或本次生成
  区间彼此重叠时返回 booking_conflict，不保存任何新预约、不改动原记录、
  不消耗预约标识；端点相接（含跨度恰好七天的相邻两次）不冲突，已取消及
  其他资源预约不阻挡；
- 合法输入但资源不存在（含大于 2^63-1 的正整数标识）仍返回
  resource_not_found；省略参数时单次预约语义保持不变。
"""

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# 项目根目录（booking 包所在目录），子进程以此作为工作目录。
PROJECT_ROOT = Path(__file__).resolve().parent

DAY = "2026-10-05"
WEEK2 = "2026-10-12"


class RepeatReserveTestCase(unittest.TestCase):
    """每个用例一个独立的临时目录和数据库路径，tearDown 自动清理。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="booking-test-repeat-")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "test.sqlite"

    def db_path_for(self, name):
        return Path(self._tmpdir.name) / name

    # ---- 公开命令的调用辅助 ----

    def run_cli(self, *args, db=None):
        return subprocess.run(
            [sys.executable, "-m", "booking", "--db", str(db or self.db_path), *args],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def run_ok(self, *args, db=None):
        proc = self.run_cli(*args, db=db)
        self.assertEqual(
            proc.returncode, 0,
            msg=f"args={args!r} rc={proc.returncode} stdout={proc.stdout!r} stderr={proc.stderr!r}",
        )
        self.assertEqual(proc.stderr, "", msg=f"stderr 应为空：{proc.stderr!r}")
        payload = json.loads(proc.stdout)
        self.assertIsInstance(payload, dict)
        return payload

    def run_error(self, expected_error, *args, db=None):
        """断言失败：退出码 2、stdout 仅一个错误 JSON、stderr 为空，返回载荷。"""
        proc = self.run_cli(*args, db=db)
        self.assertEqual(
            proc.returncode, 2,
            msg=f"args={args!r} 应以 2 失败；rc={proc.returncode} stdout={proc.stdout!r} stderr={proc.stderr!r}",
        )
        self.assertEqual(proc.stderr, "", msg=f"stderr 应为空：{proc.stderr!r}")
        lines = proc.stdout.splitlines()
        self.assertEqual(len(lines), 1, msg=f"stdout 应只有一个 JSON 对象：{proc.stdout!r}")
        payload = json.loads(lines[0])
        self.assertEqual(payload, {"error": expected_error})
        return payload

    def add_resource(self, name="测试资源"):
        payload = self.run_ok("resource-add", "--name", name)
        return payload["resource_id"]

    def reserve_single_ok(self, resource_id, start, end):
        payload = self.run_ok(
            "reserve", "--resource", str(resource_id),
            "--start", start, "--end", end,
        )
        self.assertEqual(
            set(payload), {"booking_id", "resource_id", "start", "end"}
        )
        return payload["booking_id"]

    def repeat_ok(self, resource_id, start, end, repeat_weeks):
        """重复预约成功，返回 bookings 列表并校验顶层与每项字段集合。"""
        payload = self.run_ok(
            "reserve", "--resource", str(resource_id),
            "--start", start, "--end", end,
            "--repeat-weeks", str(repeat_weeks),
        )
        self.assertEqual(set(payload), {"resource_id", "bookings"})
        self.assertEqual(payload["resource_id"], resource_id)
        bookings = payload["bookings"]
        self.assertIsInstance(bookings, list)
        for item in bookings:
            self.assertEqual(set(item), {"booking_id", "start", "end"})
            self.assertIsInstance(item["booking_id"], int)
            self.assertGreater(item["booking_id"], 0)
        return bookings

    def day_bookings(self, resource_id, date):
        payload = self.run_ok(
            "day-query", "--resource", str(resource_id), "--date", date
        )
        return payload["bookings"]

    # ---- 验收主流程 ----

    def test_acceptance_conflict_then_cancel_and_two_independent_bookings(self):
        resource_id = self.add_resource()
        start, end = f"{DAY}T09:00", f"{DAY}T10:00"
        week2_start, week2_end = f"{WEEK2}T09:00", f"{WEEK2}T10:00"

        # 预先为资源预约 2026-10-12 09:00–10:00（占用第 2 周）。
        blocker = self.reserve_single_ok(resource_id, week2_start, week2_end)

        # 以 2026-10-05 起请求两次重复：与第 2 周已有预约冲突，整批失败。
        self.run_error(
            "booking_conflict",
            "reserve", "--resource", str(resource_id),
            "--start", start, "--end", end, "--repeat-weeks", "2",
        )
        # 首周无新增，次周仍只有原预约。
        self.assertEqual(self.day_bookings(resource_id, DAY), [])
        self.assertEqual(
            self.day_bookings(resource_id, WEEK2),
            [{"booking_id": blocker, "start": week2_start, "end": week2_end}],
        )

        # 取消原预约后重试，得到两个独立预约，按发生时间升序。
        cancelled = self.run_ok("cancel", "--booking", str(blocker))
        self.assertEqual(cancelled, {"booking_id": blocker, "cancelled": True})
        bookings = self.repeat_ok(resource_id, start, end, 2)
        self.assertEqual(
            bookings,
            [
                {"booking_id": bookings[0]["booking_id"], "start": start, "end": end},
                {"booking_id": bookings[1]["booking_id"], "start": week2_start, "end": week2_end},
            ],
        )
        first_id, second_id = bookings[0]["booking_id"], bookings[1]["booking_id"]
        self.assertNotEqual(first_id, second_id)
        self.assertGreater(first_id, blocker)
        self.assertGreater(second_id, first_id)

        # 每条命令都是独立进程：重开同一 SQLite 文件后仍可分别查询。
        self.assertEqual(
            self.day_bookings(resource_id, DAY),
            [{"booking_id": first_id, "start": start, "end": end}],
        )
        self.assertEqual(
            self.day_bookings(resource_id, WEEK2),
            [{"booking_id": second_id, "start": week2_start, "end": week2_end}],
        )

        # cancel 只取消指定一项，无组取消。
        self.run_ok("cancel", "--booking", str(first_id))
        self.assertEqual(self.day_bookings(resource_id, DAY), [])
        self.assertEqual(
            self.day_bookings(resource_id, WEEK2),
            [{"booking_id": second_id, "start": week2_start, "end": week2_end}],
        )
        # 旧标识再次取消为 not_found，且不影响另一项。
        self.run_error("booking_not_found", "cancel", "--booking", str(first_id))
        self.assertEqual(len(self.day_bookings(resource_id, WEEK2)), 1)

    # ---- 成功结构与每周推进 ----

    def test_success_shape_and_weekly_occurrences_ascending(self):
        resource_id = self.add_resource()
        bookings = self.repeat_ok(
            resource_id, "2027-01-05T09:00", "2027-01-05T10:00", 4
        )
        starts = [
            "2027-01-05T09:00", "2027-01-12T09:00",
            "2027-01-19T09:00", "2027-01-26T09:00",
        ]
        ends = [
            "2027-01-05T10:00", "2027-01-12T10:00",
            "2027-01-19T10:00", "2027-01-26T10:00",
        ]
        self.assertEqual([b["start"] for b in bookings], starts)
        self.assertEqual([b["end"] for b in bookings], ends)
        # 各项获得独立标识，且严格递增（同一事务内依次分配）。
        ids = [b["booking_id"] for b in bookings]
        self.assertEqual(len(set(ids)), 4)
        self.assertEqual(ids, sorted(ids))

    def test_leading_zeros_boundaries_and_eight_occurrences(self):
        resource_id = self.add_resource()
        bookings = self.repeat_ok(
            resource_id, "2027-02-05T09:15", "2027-02-05T10:45", "008"
        )
        self.assertEqual(len(bookings), 8)
        expected_starts = [
            f"2027-02-{day:02d}T09:15"
            for day in (5, 12, 19, 26)
        ] + [
            f"2027-03-{day:02d}T09:15"
            for day in (5, 12, 19, 26)
        ]
        self.assertEqual([b["start"] for b in bookings], expected_starts)
        self.assertTrue(all(b["end"].endswith("T10:45") for b in bookings))

    def test_weekly_shift_uses_local_calendar_days_across_month_and_leap_boundary(self):
        """七个本地日历日推进，跨月末与闰日（2020 为闰年）仍正确。"""
        resource_id = self.add_resource()
        bookings = self.repeat_ok(
            resource_id, "2020-02-26T23:30", "2020-02-27T00:30", 3
        )
        self.assertEqual(
            [(b["start"], b["end"]) for b in bookings],
            [
                ("2020-02-26T23:30", "2020-02-27T00:30"),
                ("2020-03-04T23:30", "2020-03-05T00:30"),
                ("2020-03-11T23:30", "2020-03-12T00:30"),
            ],
        )

    # ---- 非法次数与端点越界 ----

    def test_invalid_repeat_weeks_values(self):
        resource_id = self.add_resource()
        bad_values = [
            "", "0", "000", "1", "01", "9", "10", "-2", "2.5",
            " 2", "2 ", "2\n", "2\t", "２", "٢", "٨",
            "99999999999999999999999999",
        ]
        base = ("reserve", "--resource", str(resource_id),
                "--start", f"{DAY}T09:00", "--end", f"{DAY}T10:00")
        for index, value in enumerate(bad_values):
            with self.subTest(value=value):
                db = self.db_path_for(f"bad-{index}.sqlite")
                self.run_error(
                    "invalid_input", *base, "--repeat-weeks", value, db=db
                )
                # 非法输入不建库、不改动任何数据。
                self.assertFalse(db.exists())

    def test_repeat_weeks_without_value_is_invalid_input(self):
        proc = self.run_cli(
            "reserve", "--resource", "1",
            "--start", f"{DAY}T09:00", "--end", f"{DAY}T10:00",
            "--repeat-weeks",
        )
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stdout), {"error": "invalid_input"})
        self.assertEqual(proc.stderr, "")

    def test_occurrence_beyond_date_range_is_invalid_input(self):
        resource_id = self.add_resource()
        # 第二次区间的起点越界。
        self.run_error(
            "invalid_input",
            "reserve", "--resource", str(resource_id),
            "--start", "9999-12-30T09:00", "--end", "9999-12-30T10:00",
            "--repeat-weeks", "2",
        )
        # 首次区间合法、第二次终点越界（跨日区间）。
        self.run_error(
            "invalid_input",
            "reserve", "--resource", str(resource_id),
            "--start", "9999-12-25T23:30", "--end", "9999-12-26T00:30",
            "--repeat-weeks", "2",
        )

    def test_invalid_input_does_not_create_database(self):
        missing_db = self.db_path_for("never-created.sqlite")
        self.run_error(
            "invalid_input",
            "reserve", "--resource", "1",
            "--start", f"{DAY}T09:00", "--end", f"{DAY}T10:00",
            "--repeat-weeks", "9", db=missing_db,
        )
        self.assertFalse(missing_db.exists())

    def test_input_validation_precedes_resource_check(self):
        # 非法次数 + 不存在资源：先报 invalid_input。
        self.run_error(
            "invalid_input",
            "reserve", "--resource", "999",
            "--start", f"{DAY}T09:00", "--end", f"{DAY}T10:00",
            "--repeat-weeks", "1",
        )
        # 越界端点 + 不存在资源：先报 invalid_input。
        self.run_error(
            "invalid_input",
            "reserve", "--resource", "999",
            "--start", "9999-12-30T09:00", "--end", "9999-12-30T10:00",
            "--repeat-weeks", "2",
        )
        # 合法次数但资源不存在：resource_not_found。
        self.run_error(
            "resource_not_found",
            "reserve", "--resource", "999",
            "--start", f"{DAY}T09:00", "--end", f"{DAY}T10:00",
            "--repeat-weeks", "2",
        )
        # 超大标识是合法输入，仍按不存在处理。
        self.run_error(
            "resource_not_found",
            "reserve", "--resource", "9223372036854775808",
            "--start", f"{DAY}T09:00", "--end", f"{DAY}T10:00",
            "--repeat-weeks", "2",
        )

    # ---- 整批原子性 ----

    def test_conflict_on_any_occurrence_aborts_whole_batch(self):
        resource_id = self.add_resource()
        # 只占用第 3 次（2027-07-19）。
        blocker = self.reserve_single_ok(
            resource_id, "2027-07-19T09:00", "2027-07-19T10:00"
        )
        self.run_error(
            "booking_conflict",
            "reserve", "--resource", str(resource_id),
            "--start", "2027-07-05T09:00", "--end", "2027-07-05T10:00",
            "--repeat-weeks", "4",
        )
        # 其余三次均无新增。
        for date in ("2027-07-05", "2027-07-12", "2027-07-26"):
            self.assertEqual(self.day_bookings(resource_id, date), [])
        self.assertEqual(
            len(self.day_bookings(resource_id, "2027-07-19")), 1
        )
        # 不消耗预约标识：紧接 blocker 之后的成功预约拿到 blocker + 1。
        next_id = self.reserve_single_ok(
            resource_id, "2027-08-05T09:00", "2027-08-05T10:00"
        )
        self.assertEqual(next_id, blocker + 1)

    def test_generated_occurrences_overlapping_each_other_conflict(self):
        resource_id = self.add_resource()
        # 区间跨度 8 天 > 7 天步进：两次生成区间彼此重叠。
        self.run_error(
            "booking_conflict",
            "reserve", "--resource", str(resource_id),
            "--start", f"{DAY}T09:00", "--end", "2026-10-13T09:00",
            "--repeat-weeks", "2",
        )
        self.assertEqual(self.day_bookings(resource_id, DAY), [])
        self.assertEqual(self.day_bookings(resource_id, "2026-10-13"), [])
        # 跨度恰好七天：端点相接（左闭右开），整批成功。
        bookings = self.repeat_ok(
            resource_id, "2027-06-05T09:00", "2027-06-12T09:00", 2
        )
        self.assertEqual(
            [(b["start"], b["end"]) for b in bookings],
            [
                ("2027-06-05T09:00", "2027-06-12T09:00"),
                ("2027-06-12T09:00", "2027-06-19T09:00"),
            ],
        )

    def test_cancelled_and_other_resource_bookings_do_not_block(self):
        resource_a = self.add_resource("甲")
        resource_b = self.add_resource("乙")
        starts = ["2027-09-05T09:00", "2027-09-12T09:00", "2027-09-19T09:00"]
        # 资源乙三次同时段全部占用，不影响资源甲。
        self.repeat_ok(resource_b, starts[0], "2027-09-05T10:00", 3)
        bookings_a = self.repeat_ok(
            resource_a, starts[0], "2027-09-05T10:00", 3
        )
        self.assertEqual([b["start"] for b in bookings_a], starts)

        # 取消资源甲全部三次后，以相同三次重复请求应整批成功；
        # 已取消的预约不再阻挡（其标识不复用）。
        for item in bookings_a:
            self.run_ok("cancel", "--booking", str(item["booking_id"]))
        rebooked = self.repeat_ok(
            resource_a, starts[0], "2027-09-05T10:00", 3
        )
        new_ids = {b["booking_id"] for b in rebooked}
        self.assertTrue(new_ids.isdisjoint(
            {b["booking_id"] for b in bookings_a}
        ))
        self.assertEqual([b["start"] for b in rebooked], starts)

    def test_past_dates_allowed(self):
        resource_id = self.add_resource()
        bookings = self.repeat_ok(
            resource_id, "2001-01-01T08:00", "2001-01-01T09:00", 2
        )
        self.assertEqual(
            [b["start"] for b in bookings],
            ["2001-01-01T08:00", "2001-01-08T08:00"],
        )

    # ---- 省略参数：单次预约语义不变 ----

    def test_without_option_single_booking_semantics_unchanged(self):
        resource_id = self.add_resource()
        payload = self.run_ok(
            "reserve", "--resource", str(resource_id),
            "--start", f"{DAY}T09:00", "--end", f"{DAY}T10:00",
        )
        booking_id = payload["booking_id"]
        # 单次成功返回维持原四字段结构，不含 bookings。
        self.assertEqual(
            payload,
            {"booking_id": booking_id, "resource_id": resource_id,
             "start": f"{DAY}T09:00", "end": f"{DAY}T10:00"},
        )
        # 同时段再次请求仍为单次冲突语义。
        self.run_error(
            "booking_conflict",
            "reserve", "--resource", str(resource_id),
            "--start", f"{DAY}T09:00", "--end", f"{DAY}T10:00",
        )
        self.assertEqual(
            self.day_bookings(resource_id, DAY),
            [{"booking_id": booking_id, "start": f"{DAY}T09:00",
              "end": f"{DAY}T10:00"}],
        )

    # ---- 旧库兼容：无 cancelled 列的库上重复预约照常工作 ----

    def test_repeat_works_on_legacy_database(self):
        db = self.db_path_for("legacy.sqlite")
        conn = sqlite3.connect(str(db))
        conn.executescript(
            """
            CREATE TABLE resources (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL);
            CREATE TABLE bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT, resource_id INTEGER NOT NULL,
                start TEXT NOT NULL, end TEXT NOT NULL);
            INSERT INTO resources VALUES (7, 'old room');
            INSERT INTO bookings VALUES (21, 7, '2026-10-12T09:00', '2026-10-12T10:00');
            """
        )
        conn.commit()
        conn.close()

        # 旧预约占用第 2 周：整批冲突。
        self.run_error(
            "booking_conflict",
            "reserve", "--resource", "7",
            "--start", f"{DAY}T09:00", "--end", f"{DAY}T10:00",
            "--repeat-weeks", "2", db=db,
        )
        # 取消旧预约后重复预约成功，新标识严格大于 21，重开后可分别查询。
        self.run_ok("cancel", "--booking", "21", db=db)
        payload = self.run_ok(
            "reserve", "--resource", "7",
            "--start", f"{DAY}T09:00", "--end", f"{DAY}T10:00",
            "--repeat-weeks", "2", db=db,
        )
        ids = [b["booking_id"] for b in payload["bookings"]]
        self.assertEqual(ids, [22, 23])
        self.assertEqual(
            self.run_ok("day-query", "--resource", "7", "--date", DAY, db=db)["bookings"],
            [{"booking_id": 22, "start": f"{DAY}T09:00", "end": f"{DAY}T10:00"}],
        )
        self.assertEqual(
            self.run_ok("day-query", "--resource", "7", "--date", WEEK2, db=db)["bookings"],
            [{"booking_id": 23, "start": f"{WEEK2}T09:00", "end": f"{WEEK2}T10:00"}],
        )


if __name__ == "__main__":
    unittest.main()
