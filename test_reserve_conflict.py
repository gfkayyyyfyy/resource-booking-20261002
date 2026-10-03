"""reserve 时段冲突规则的回归测试（仅标准库，python -m unittest 可发现）。

通过公开命令行入口 `python -m booking --db <文件> <命令>` 准备数据并断言，
每个用例使用独立的临时 SQLite 数据库，结束后自动清理；
不依赖已有数据库、当前日期、机器时区或任何第三方库。
断言均基于解析后的 JSON 内容，不依赖输出对象的键顺序，也不预设 booking_id。

覆盖的公开行为（固定 UTC+08:00，主要样例日期 2026-10-05）：
- 同一资源已有 09:00–10:00 预约时，完全相同、前后端点伸入、完整包含、
  被包含等各类相交请求均返回 {"error": "booking_conflict"}，退出码 2；
- 左闭右开：08:00–09:00 与 10:00–11:00 端点相接不算重叠，可以成功；
- 不同资源的同一时段互不影响，均可成功；
- 冲突失败不改动任何记录：失败前后 day-query 结果中已有预约的标识、
  时段与数量完全一致；冲突失败后同资源相邻时段仍可预约；
- 跨午夜场景一致：已有 2026-10-05T23:30–2026-10-06T00:30 时，
  次日 00:00–01:00 冲突，00:30–01:00 端点相接可成功；两个日期分别核验；
- 原预约与成功新建的预约在重新打开同一数据库文件后仍然保留。
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
NEXT_DAY = "2026-10-06"
SEED_START = f"{DAY}T09:00"
SEED_END = f"{DAY}T10:00"


class ReserveConflictTestCase(unittest.TestCase):
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

    def assert_reserve_conflict(self, resource_id, start, end, db=None):
        """断言预约请求返回 {"error": "booking_conflict"} 且退出码为 2。"""
        proc = self.run_cli(
            "reserve", "--resource", str(resource_id),
            "--start", start, "--end", end, db=db,
        )
        slot = f"[{start}, {end})"
        self.assertEqual(
            proc.returncode, 2,
            msg=f"请求时段 {slot} 应冲突并以退出码 2 失败；实际 rc={proc.returncode} "
                f"stdout={proc.stdout!r} stderr={proc.stderr!r}",
        )
        payload = json.loads(proc.stdout)
        self.assertEqual(
            payload, {"error": "booking_conflict"},
            msg=f"请求时段 {slot} 应返回 {{'error': 'booking_conflict'}}；实际返回 {payload!r}",
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

    def seed_morning_booking(self, db=None):
        """登记一个资源并在其上创建 09:00–10:00 预约，返回 (resource_id, booking_id)。"""
        resource_id = self.add_resource(db=db)
        booking_id = self.reserve_ok(resource_id, SEED_START, SEED_END, db=db)
        return resource_id, booking_id

    # ---- 冲突：同一资源上与 09:00–10:00 相交的各类请求 ----

    def test_overlapping_requests_all_return_booking_conflict(self):
        """完全相同、前后伸入、包含与被包含的五类请求均冲突退出，且不改动已有记录。"""
        conflict_windows = [
            ("完全相同时段", f"{DAY}T09:00", f"{DAY}T10:00"),
            ("开始早于已有开始、结束落在区间内", f"{DAY}T08:30", f"{DAY}T09:30"),
            ("开始落在区间内、结束晚于已有结束", f"{DAY}T09:30", f"{DAY}T10:30"),
            ("完整包含已有时段", f"{DAY}T08:30", f"{DAY}T10:30"),
            ("被已有时段完整包含", f"{DAY}T09:15", f"{DAY}T09:45"),
        ]
        for index, (label, start, end) in enumerate(conflict_windows):
            with self.subTest(conflict_window=label, start=start, end=end):
                # 每个冲突窗口使用彼此独立的数据库文件，互不影响。
                db = self.db_path_for(f"conflict-{index}.sqlite")
                resource_id, seed_id = self.seed_morning_booking(db=db)
                expected_booking = {"booking_id": seed_id, "start": SEED_START, "end": SEED_END}

                before = self.day_bookings(resource_id, DAY, db=db)
                self.assertEqual(before, [expected_booking])

                self.assert_reserve_conflict(resource_id, start, end, db=db)

                # 可观察状态不变：已有预约的标识、时段与数量均与失败前一致。
                after = self.day_bookings(resource_id, DAY, db=db)
                self.assertEqual(
                    after, before,
                    msg=f"冲突窗口 {label}（{start}~{end}）失败后记录发生变化："
                        f"before={before!r} after={after!r}",
                )
                self.assertEqual(len(after), 1)
                self.assertEqual(after[0]["booking_id"], seed_id)
                self.assertEqual((after[0]["start"], after[0]["end"]), (SEED_START, SEED_END))

    # ---- 左闭右开：端点相接不算重叠 ----

    def test_endpoint_touching_slots_do_not_conflict(self):
        """08:00–09:00 与 10:00–11:00 和已有 09:00–10:00 端点相接，均可成功。"""
        resource_id, seed_id = self.seed_morning_booking()

        left_id = self.reserve_ok(resource_id, f"{DAY}T08:00", f"{DAY}T09:00")
        right_id = self.reserve_ok(resource_id, f"{DAY}T10:00", f"{DAY}T11:00")

        # 每个成功结果各自获得新的正整数标识，互不相同且不等于原预约标识。
        self.assertEqual(len({seed_id, left_id, right_id}), 3)

        bookings = self.day_bookings(resource_id, DAY)
        self.assertEqual(
            bookings,
            [
                {"booking_id": left_id, "start": f"{DAY}T08:00", "end": f"{DAY}T09:00"},
                {"booking_id": seed_id, "start": SEED_START, "end": SEED_END},
                {"booking_id": right_id, "start": f"{DAY}T10:00", "end": f"{DAY}T11:00"},
            ],
        )

    def test_conflict_failure_leaves_adjacent_slot_available(self):
        """冲突请求被拒后，同资源上与其端点相接的相邻时段仍可正常预约。"""
        resource_id, seed_id = self.seed_morning_booking()

        self.assert_reserve_conflict(resource_id, f"{DAY}T09:30", f"{DAY}T10:30")

        # 失败后紧邻的 10:00–11:00 不受影响，正常创建。
        adjacent_id = self.reserve_ok(resource_id, f"{DAY}T10:00", f"{DAY}T11:00")
        self.assertNotEqual(adjacent_id, seed_id)

        bookings = self.day_bookings(resource_id, DAY)
        self.assertEqual(len(bookings), 2)
        self.assertEqual(
            bookings,
            [
                {"booking_id": seed_id, "start": SEED_START, "end": SEED_END},
                {"booking_id": adjacent_id, "start": f"{DAY}T10:00", "end": f"{DAY}T11:00"},
            ],
        )

    # ---- 资源隔离 ----

    def test_same_time_range_on_different_resource_succeeds(self):
        """不同资源上的 09:00–10:00 互不冲突，各自只返回本资源的预约。"""
        resource_a, id_a = self.seed_morning_booking()
        resource_b = self.add_resource(name="另一资源")
        id_b = self.reserve_ok(resource_b, SEED_START, SEED_END)

        self.assertNotEqual(id_b, id_a)

        bookings_a = self.day_bookings(resource_a, DAY)
        bookings_b = self.day_bookings(resource_b, DAY)
        self.assertEqual(bookings_a, [{"booking_id": id_a, "start": SEED_START, "end": SEED_END}])
        self.assertEqual(bookings_b, [{"booking_id": id_b, "start": SEED_START, "end": SEED_END}])

    # ---- 跨午夜：规则在午夜两侧保持一致 ----

    def test_cross_midnight_overlap_returns_booking_conflict(self):
        """已有 10-05T23:30–10-06T00:30 时，次日 00:00–01:00 冲突；两天查询均不变。"""
        resource_id = self.add_resource()
        cross_start = f"{DAY}T23:30"
        cross_end = f"{NEXT_DAY}T00:30"
        cross_id = self.reserve_ok(resource_id, cross_start, cross_end)
        expected = {"booking_id": cross_id, "start": cross_start, "end": cross_end}

        # 跨日预约与两个日期都有交集，两个日期的查询都应包含它。
        before_first = self.day_bookings(resource_id, DAY)
        before_second = self.day_bookings(resource_id, NEXT_DAY)
        self.assertEqual(before_first, [expected])
        self.assertEqual(before_second, [expected])

        self.assert_reserve_conflict(
            resource_id, f"{NEXT_DAY}T00:00", f"{NEXT_DAY}T01:00"
        )

        # 分别查询涉及的两个日期：已有预约的标识、时段与数量均不变。
        after_first = self.day_bookings(resource_id, DAY)
        after_second = self.day_bookings(resource_id, NEXT_DAY)
        self.assertEqual(after_first, before_first)
        self.assertEqual(after_second, before_second)
        self.assertEqual(len(after_first), 1)
        self.assertEqual(len(after_second), 1)

    def test_cross_midnight_endpoint_touching_slot_succeeds(self):
        """跨午夜预约之后，次日 00:30–01:00 与端点相接可创建（先经历一次冲突失败）。"""
        resource_id = self.add_resource()
        cross_id = self.reserve_ok(resource_id, f"{DAY}T23:30", f"{NEXT_DAY}T00:30")

        # 相交请求先失败，失败不得影响后续合法预约。
        self.assert_reserve_conflict(
            resource_id, f"{NEXT_DAY}T00:00", f"{NEXT_DAY}T01:00"
        )
        new_id = self.reserve_ok(resource_id, f"{NEXT_DAY}T00:30", f"{NEXT_DAY}T01:00")
        self.assertNotEqual(new_id, cross_id)

        # 前一天只有跨日预约；次日查询包含跨日预约与新建预约，按 start 升序。
        self.assertEqual(
            self.day_bookings(resource_id, DAY),
            [{"booking_id": cross_id, "start": f"{DAY}T23:30", "end": f"{NEXT_DAY}T00:30"}],
        )
        self.assertEqual(
            self.day_bookings(resource_id, NEXT_DAY),
            [
                {"booking_id": cross_id, "start": f"{DAY}T23:30", "end": f"{NEXT_DAY}T00:30"},
                {"booking_id": new_id, "start": f"{NEXT_DAY}T00:30", "end": f"{NEXT_DAY}T01:00"},
            ],
        )

    # ---- 持久化：重新打开同一文件 ----

    def test_bookings_survive_reopening_database_after_conflict_failures(self):
        """原预约与成功创建的预约在重新打开同一数据库后保留，冲突规则依旧生效。"""
        resource_id, seed_id = self.seed_morning_booking()
        # 一次冲突失败，随后两个端点相接的预约成功。
        self.assert_reserve_conflict(resource_id, f"{DAY}T09:15", f"{DAY}T09:45")
        left_id = self.reserve_ok(resource_id, f"{DAY}T08:00", f"{DAY}T09:00")
        right_id = self.reserve_ok(resource_id, f"{DAY}T10:00", f"{DAY}T11:00")

        # 每条命令都是独立进程，以下查询与预约请求即“重新打开同一文件”后的行为。
        bookings = self.day_bookings(resource_id, DAY)
        self.assertEqual(
            bookings,
            [
                {"booking_id": left_id, "start": f"{DAY}T08:00", "end": f"{DAY}T09:00"},
                {"booking_id": seed_id, "start": SEED_START, "end": SEED_END},
                {"booking_id": right_id, "start": f"{DAY}T10:00", "end": f"{DAY}T11:00"},
            ],
        )
        # 重开后相交时段仍被拒绝，且数量不增加。
        self.assert_reserve_conflict(resource_id, f"{DAY}T09:30", f"{DAY}T10:30")
        self.assertEqual(self.day_bookings(resource_id, DAY), bookings)


if __name__ == "__main__":
    unittest.main()
