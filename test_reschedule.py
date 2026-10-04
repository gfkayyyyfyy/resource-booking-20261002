"""reschedule 单条预约改期的回归测试（仅标准库，python -m unittest 可发现）。

通过公开命令行入口 `python -m booking --db <文件> <命令>` 准备数据并断言，
每个用例使用独立的临时 SQLite 数据库，结束后自动清理；
不依赖已有数据库、当前日期、机器时区或任何第三方库。
断言均基于解析后的 JSON 内容，不依赖输出对象的键顺序。

覆盖的公开行为（固定 UTC+08:00，主要样例日期 2026-10-05）：
- 验收：资源 1 上预约 1 为 09:00–10:00、预约 2 为 11:00–12:00 时，
  预约 1 改到 10:00–11:00 成功且仍为标识 1；再改到 10:30–11:30 与预约 2
  冲突，保留 10:00–11:00 不变；
- 成功只返回 booking_id、resource_id、start、end 四个字段，标识保持原值，
  时间保留输入文本，资源归属不变；改期不新增预约、不消耗标识；
- 改期后 day-query/free-query 按新时段显示占用、原时段释放，
  重新打开同一数据库后结果一致；
- 冲突只比较同资源“其他”未取消预约（左闭右开，端点相接可成功）：
  目标预约自身不阻挡，新旧时段重叠乃至时段完全不变都允许成功；
- 冲突时 booking_conflict 且原子回滚：原时段、取消状态与其他记录不变，
  不会出现只释放旧时段的结果；他资源预约不阻挡；允许过去日期与跨日区间；
- 预约不存在、已经取消（不能借改期恢复）或标识大于 2^63-1 时
  返回 booking_not_found；
- 缺少参数、不支持的参数（含 --repeat-weeks）、非法标识、非法时间、
  起止顺序错误统一 invalid_input 且优先于存在性检查，非法输入不建库；
- 每周重复预约只改指定一项，不接受 --repeat-weeks，其他各项不受影响；
- 取消功能上线前的旧库直接兼容。
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

DAY = "2026-10-05"
START_09 = f"{DAY}T09:00"
END_10 = f"{DAY}T10:00"
START_10 = f"{DAY}T10:00"
START_1030 = f"{DAY}T10:30"
END_11 = f"{DAY}T11:00"
END_1130 = f"{DAY}T11:30"
START_11 = f"{DAY}T11:00"
END_12 = f"{DAY}T12:00"


class RescheduleTestCase(unittest.TestCase):
    """每个用例一个独立的临时目录和数据库路径，tearDown 自动清理。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="booking-test-")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "test.sqlite"

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
        self.assertEqual(proc.returncode, 0, msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        self.assertEqual(proc.stderr, "")
        payload = json.loads(proc.stdout)
        self.assertIsInstance(payload, dict)
        return payload

    def run_error(self, expected_error, *args, db=None):
        proc = self.run_cli(*args, db=db)
        self.assertEqual(proc.returncode, 2, msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        self.assertEqual(proc.stderr, "")
        self.assertEqual(json.loads(proc.stdout), {"error": expected_error})
        return proc

    def add_resource(self, name="测试资源"):
        return self.run_ok("resource-add", "--name", name)["resource_id"]

    def reserve_ok(self, resource_id, start, end):
        payload = self.run_ok(
            "reserve", "--resource", str(resource_id),
            "--start", start, "--end", end,
        )
        self.assertEqual(
            payload,
            {"booking_id": payload["booking_id"], "resource_id": resource_id,
             "start": start, "end": end},
        )
        return payload["booking_id"]

    def reschedule_ok(self, booking_id, start, end):
        payload = self.run_ok(
            "reschedule", "--booking", str(booking_id),
            "--start", start, "--end", end,
        )
        # 成功对象顶层恰好四个字段：标识保持原值，时间保留输入文本。
        self.assertEqual(
            payload,
            {"booking_id": booking_id, "resource_id": payload["resource_id"],
             "start": start, "end": end},
        )
        return payload

    def day_bookings(self, resource_id, date=DAY):
        return self.run_ok(
            "day-query", "--resource", str(resource_id), "--date", date
        )["bookings"]

    # ---- 验收流程：两次改期 ----

    def test_acceptance_two_reschedules(self):
        resource_id = self.add_resource()
        booking1 = self.reserve_ok(resource_id, START_09, END_10)
        booking2 = self.reserve_ok(resource_id, START_11, END_12)
        self.assertEqual((booking1, booking2), (1, 2))

        # 第一次：10:00–11:00 与两侧均为端点相接，成功且标识仍为 1。
        payload = self.reschedule_ok(booking1, START_10, END_11)
        self.assertEqual(payload["resource_id"], resource_id)
        self.assertEqual(payload["booking_id"], 1)

        # 第二次：10:30–11:30 与预约 2（11:00–12:00）相交，冲突。
        self.run_error(
            "booking_conflict", "reschedule", "--booking", "1",
            "--start", START_1030, "--end", END_1130,
        )
        # 冲突后预约 1 保留 10:00–11:00，原时段仍占用。
        self.assertEqual(
            self.day_bookings(resource_id),
            [
                {"booking_id": 1, "start": START_10, "end": END_11},
                {"booking_id": 2, "start": START_11, "end": END_12},
            ],
        )

    def test_new_window_visible_old_window_released_and_persists(self):
        """改期后查询按新时段占用、原时段释放；重开同一数据库后一致。"""
        resource_id = self.add_resource()
        booking_id = self.reserve_ok(resource_id, START_09, END_10)
        self.reschedule_ok(booking_id, START_1030, END_1130)

        # day-query 只显示新时段。
        self.assertEqual(
            self.day_bookings(resource_id),
            [{"booking_id": booking_id, "start": START_1030, "end": END_1130}],
        )
        # free-query：原 09:00–10:00 空闲，10:30–11:30 被占。
        free = self.run_ok(
            "free-query", "--resource", str(resource_id),
            "--start", START_09, "--end", END_12,
        )["free_slots"]
        self.assertEqual(
            free,
            [
                {"start": START_09, "end": START_1030},
                {"start": END_1130, "end": END_12},
            ],
        )
        # 原时段可被新预约占用；与新时段重叠的请求仍冲突。
        self.reserve_ok(resource_id, START_09, END_10)
        self.run_error(
            "booking_conflict", "reserve", "--resource", str(resource_id),
            "--start", f"{DAY}T11:00", "--end", f"{DAY}T11:30",
        )
        # 每条命令都是独立进程：以上即“重开数据库后一致”，再查一次确认。
        rows = self.day_bookings(resource_id)
        self.assertIn(
            {"booking_id": booking_id, "start": START_1030, "end": END_1130},
            rows,
        )

    def test_reschedule_does_not_create_booking_or_consume_id(self):
        """改期不新增预约、不消耗标识、不改其他记录。"""
        resource_id = self.add_resource()
        target = self.reserve_ok(resource_id, START_09, END_10)
        other = self.reserve_ok(resource_id, START_11, END_12)
        self.reschedule_ok(target, f"{DAY}T07:00", f"{DAY}T08:00")
        # 再新建预约得到紧接当前最大标识的新标识（改期没有插入任何行）。
        new_id = self.reserve_ok(resource_id, f"{DAY}T13:00", f"{DAY}T14:00")
        self.assertEqual(new_id, other + 1)
        self.assertEqual(
            self.day_bookings(resource_id),
            [
                {"booking_id": target, "start": f"{DAY}T07:00", "end": f"{DAY}T08:00"},
                {"booking_id": other, "start": START_11, "end": END_12},
                {"booking_id": new_id, "start": f"{DAY}T13:00", "end": f"{DAY}T14:00"},
            ],
        )

    # ---- 冲突语义：自身不阻挡、端点相接、他资源不阻挡 ----

    def test_unchanged_window_succeeds(self):
        """新时段与原时段完全相同：自身不阻挡，成功。"""
        resource_id = self.add_resource()
        booking_id = self.reserve_ok(resource_id, START_09, END_10)
        self.reschedule_ok(booking_id, START_09, END_10)
        self.assertEqual(
            self.day_bookings(resource_id),
            [{"booking_id": booking_id, "start": START_09, "end": END_10}],
        )

    def test_overlap_with_own_old_window_succeeds(self):
        """新旧时段彼此重叠允许：09:30–10:30 与自身旧区间相交也成功。"""
        resource_id = self.add_resource()
        booking_id = self.reserve_ok(resource_id, START_09, END_10)
        self.reschedule_ok(booking_id, f"{DAY}T09:30", f"{DAY}T10:30")
        self.assertEqual(
            self.day_bookings(resource_id),
            [{"booking_id": booking_id, "start": f"{DAY}T09:30", "end": f"{DAY}T10:30"}],
        )

    def test_endpoint_adjacency_to_other_booking_succeeds(self):
        """与同资源其他预约端点相接（左闭右开）可成功。"""
        resource_id = self.add_resource()
        target = self.reserve_ok(resource_id, START_09, END_10)
        self.reserve_ok(resource_id, START_11, END_12)
        self.reschedule_ok(target, START_10, END_11)
        self.assertEqual(
            self.day_bookings(resource_id),
            [
                {"booking_id": target, "start": START_10, "end": END_11},
                {"booking_id": target + 1, "start": START_11, "end": END_12},
            ],
        )

    def test_other_resource_does_not_block(self):
        """冲突只检查同资源：与其他资源预约重叠不阻挡。"""
        r1 = self.add_resource("一号")
        r2 = self.add_resource("二号")
        booking_id = self.reserve_ok(r1, START_09, END_10)
        self.reserve_ok(r2, START_09, END_10)
        payload = self.reschedule_ok(booking_id, f"{DAY}T09:30", f"{DAY}T10:30")
        self.assertEqual(payload["resource_id"], r1)

    def test_cross_day_window(self):
        """允许跨日区间：改期后两个日期的查询都按新区间显示。"""
        resource_id = self.add_resource()
        booking_id = self.reserve_ok(resource_id, START_09, END_10)
        self.reschedule_ok(booking_id, f"{DAY}T23:00", f"2026-10-06T01:00")
        self.assertEqual(
            self.day_bookings(resource_id, DAY),
            [{"booking_id": booking_id, "start": f"{DAY}T23:00", "end": "2026-10-06T01:00"}],
        )
        self.assertEqual(
            self.day_bookings(resource_id, "2026-10-06"),
            [{"booking_id": booking_id, "start": f"{DAY}T23:00", "end": "2026-10-06T01:00"}],
        )
        self.assertEqual(self.day_bookings(resource_id, "2026-10-07"), [])

    def test_conflict_is_atomic_old_window_retained(self):
        """冲突时只返回 booking_conflict：旧时段仍占用、取消状态不变。"""
        resource_id = self.add_resource()
        target = self.reserve_ok(resource_id, START_09, END_10)
        blocker = self.reserve_ok(resource_id, START_1030, END_1130)
        before = self.day_bookings(resource_id)

        self.run_error(
            "booking_conflict", "reschedule", "--booking", str(target),
            "--start", f"{DAY}T10:00", "--end", f"{DAY}T11:00",
        )
        # 没有出现“旧时段已释放、新时段未写入”的中间结果。
        self.assertEqual(self.day_bookings(resource_id), before)
        # 旧时段仍被目标预约占用，且目标仍可取消（未被改成取消态）。
        self.run_error(
            "booking_conflict", "reserve", "--resource", str(resource_id),
            "--start", START_09, "--end", END_10,
        )
        self.run_ok("cancel", "--booking", str(target))
        # 阻挡方完全不受影响。
        self.assertEqual(
            self.day_bookings(resource_id),
            [{"booking_id": blocker, "start": START_1030, "end": END_1130}],
        )

    # ---- booking_not_found 分支 ----

    def test_nonexistent_booking_returns_not_found_without_changes(self):
        resource_id = self.add_resource()
        existing = self.reserve_ok(resource_id, START_09, END_10)
        self.run_error(
            "booking_not_found", "reschedule", "--booking", "999",
            "--start", START_10, "--end", END_11,
        )
        self.assertEqual(
            self.day_bookings(resource_id),
            [{"booking_id": existing, "start": START_09, "end": END_10}],
        )

    def test_cancelled_booking_cannot_be_rescheduled(self):
        """已取消预约改期返回 not_found，且不能借改期恢复。"""
        resource_id = self.add_resource()
        booking_id = self.reserve_ok(resource_id, START_09, END_10)
        self.run_ok("cancel", "--booking", str(booking_id))
        self.run_error(
            "booking_not_found", "reschedule", "--booking", str(booking_id),
            "--start", START_10, "--end", END_11,
        )
        self.assertEqual(self.day_bookings(resource_id), [])
        # 再次取消仍为 not_found：取消状态未被改期触动。
        self.run_error("booking_not_found", "cancel", "--booking", str(booking_id))

    def test_oversized_booking_id_returns_not_found(self):
        """标识大于 2^63-1：合法输入但必然不存在，返回 booking_not_found。"""
        resource_id = self.add_resource()
        self.reserve_ok(resource_id, START_09, END_10)
        self.run_error(
            "booking_not_found", "reschedule",
            "--booking", "9223372036854775808",
            "--start", START_10, "--end", END_11,
        )
        # 前导零按同一数值处理。
        self.run_error(
            "booking_not_found", "reschedule",
            "--booking", "0009223372036854775808",
            "--start", START_10, "--end", END_11,
        )
        # 边界值 2^63-1 走普通查询：不存在，仍为 not_found。
        self.run_error(
            "booking_not_found", "reschedule",
            "--booking", "9223372036854775807",
            "--start", START_10, "--end", END_11,
        )

    # ---- invalid_input 分支 ----

    def test_invalid_inputs(self):
        """缺参、非法标识、非法时间、起止顺序错误统一 invalid_input。"""
        cases = [
            "reschedule --start 2026-10-05T10:00 --end 2026-10-05T11:00",
            "reschedule --booking 1 --end 2026-10-05T11:00",
            "reschedule --booking 1 --start 2026-10-05T10:00",
            "reschedule --booking 0 --start 2026-10-05T10:00 --end 2026-10-05T11:00",
            "reschedule --booking -1 --start 2026-10-05T10:00 --end 2026-10-05T11:00",
            "reschedule --booking 1.5 --start 2026-10-05T10:00 --end 2026-10-05T11:00",
            "reschedule --booking abc --start 2026-10-05T10:00 --end 2026-10-05T11:00",
            "reschedule --booking 1 --start 2026-10-05T10:00 --end 2026-10-05T10:00",
            "reschedule --booking 1 --start 2026-10-05T11:00 --end 2026-10-05T10:00",
            "reschedule --booking 1 --start 2026-02-30T10:00 --end 2026-10-05T11:00",
            "reschedule --booking 1 --start 2026-10-05T1000 --end 2026-10-05T11:00",
            "reschedule --booking 1 --start 2026-10-05T10:00:00 --end 2026-10-05T11:00",
            "reschedule --booking 1 --start 2026-10-05T10:00Z --end 2026-10-05T11:00",
            # 非 ASCII 数字的时间文本拒绝；不支持的参数（含 --repeat-weeks）拒绝。
            "reschedule --booking 1 --start ２０２６-10-05T10:00 --end 2026-10-05T11:00",
            "reschedule --booking 1 --start 2026-10-05T10:00 --end 2026-10-05T11:00 --repeat-weeks 2",
            "reschedule --booking 1 --start 2026-10-05T10:00 --end 2026-10-05T11:00 --resource 1",
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                proc = self.run_cli(*raw.split())
                self.assertEqual(proc.returncode, 2, msg=raw)
                self.assertEqual(json.loads(proc.stdout), {"error": "invalid_input"})
                self.assertEqual(proc.stderr, "")

    def test_invalid_input_precedence_and_no_database_created(self):
        """invalid_input 优先于存在性检查，且非法输入不建库、不迁移旧库。"""
        missing = Path(self._tmpdir.name) / "never.sqlite"
        # 即使标识恰好不存在/已取消、时间本会冲突，也只报 invalid_input。
        for raw in [
            "reschedule --booking 0 --start 2026-10-05T10:00 --end 2026-10-05T11:00",
            "reschedule --booking 9223372036854775808 --start 2026-10-05T11:00 --end 2026-10-05T10:00",
        ]:
            self.run_error("invalid_input", *raw.split(), db=missing)
            self.assertFalse(missing.exists())

    def test_failed_reschedules_leave_other_data_unchanged(self):
        """已有库上的各类失败前后按日查询逐次一致。"""
        resource_id = self.add_resource()
        target = self.reserve_ok(resource_id, START_09, END_10)
        self.reserve_ok(resource_id, START_11, END_12)
        before = self.run_ok(
            "day-query", "--resource", str(resource_id), "--date", DAY
        )
        failing = [
            ["reschedule", "--booking", "0", "--start", START_10, "--end", END_11],
            ["reschedule", "--booking", "999", "--start", START_10, "--end", END_11],
            ["reschedule", "--booking", str(target), "--start", START_1030, "--end", END_1130],
        ]
        for args in failing:
            proc = self.run_cli(*args)
            self.assertEqual(proc.returncode, 2)
            self.assertIn(
                json.loads(proc.stdout)["error"],
                {"invalid_input", "booking_not_found", "booking_conflict"},
            )
        after = self.run_ok("day-query", "--resource", str(resource_id), "--date", DAY)
        self.assertEqual(after, before)

    # ---- 每周重复预约：只改指定一项 ----

    def test_repeat_booking_reschedules_single_occurrence(self):
        """重复预约持久化为普通预约；改期只动指定一项，其他各项不变。"""
        resource_id = self.add_resource()
        payload = self.run_ok(
            "reserve", "--resource", str(resource_id),
            "--start", START_09, "--end", END_10, "--repeat-weeks", "3",
        )
        ids = [item["booking_id"] for item in payload["bookings"]]
        self.assertEqual(len(ids), 3)
        self.reschedule_ok(ids[0], f"{DAY}T14:00", f"{DAY}T15:00")
        self.assertEqual(
            self.day_bookings(resource_id, DAY),
            [{"booking_id": ids[0], "start": f"{DAY}T14:00", "end": f"{DAY}T15:00"}],
        )
        self.assertEqual(
            self.day_bookings(resource_id, "2026-10-12"),
            [{"booking_id": ids[1], "start": "2026-10-12T09:00", "end": "2026-10-12T10:00"}],
        )
        self.assertEqual(
            self.day_bookings(resource_id, "2026-10-19"),
            [{"booking_id": ids[2], "start": "2026-10-19T09:00", "end": "2026-10-19T10:00"}],
        )

    # ---- 旧库兼容 ----

    def test_legacy_database_without_cancelled_column(self):
        """取消功能上线前的旧库：首次打开自动迁移后可正常改期。"""
        legacy = Path(self._tmpdir.name) / "legacy.sqlite"
        conn = sqlite3.connect(legacy)
        conn.executescript(
            """
            CREATE TABLE resources (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL
            );
            CREATE TABLE bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                resource_id INTEGER NOT NULL,
                start TEXT NOT NULL,
                end TEXT NOT NULL
            );
            INSERT INTO resources VALUES (7, '旧资源');
            INSERT INTO bookings VALUES (21, 7, '2026-10-05T09:00', '2026-10-05T10:00');
            """
        )
        conn.commit()
        conn.close()

        payload = self.run_ok(
            "reschedule", "--booking", "21",
            "--start", START_1030, "--end", END_1130, db=legacy,
        )
        self.assertEqual(
            payload,
            {"booking_id": 21, "resource_id": 7,
             "start": START_1030, "end": END_1130},
        )
        bookings = self.run_ok(
            "day-query", "--resource", "7", "--date", DAY, db=legacy
        )["bookings"]
        self.assertEqual(
            bookings, [{"booking_id": 21, "start": START_1030, "end": END_1130}]
        )


if __name__ == "__main__":
    unittest.main()
