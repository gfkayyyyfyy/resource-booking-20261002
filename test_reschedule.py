"""reschedule 单条预约改期的回归测试（仅标准库，python -m unittest 可发现）。

通过公开命令行入口 `python -m booking --db <文件> <命令>` 准备数据并断言，
每个用例使用独立的临时 SQLite 数据库，结束后自动清理；
不依赖已有数据库、当前日期、机器时区或任何第三方库。
断言均基于解析后的 JSON 内容，不依赖输出对象的键顺序。

覆盖的公开行为：
- 改期成功只返回 booking_id、resource_id、start、end 四个字段：
  标识与资源保持原值，时间保留输入文本，退出码 0、stderr 为空；
- 验收流程：预约 1 由 09:00–10:00 改为 10:00–11:00 成功且标识仍为 1，
  再改为 10:30–11:30 与预约 2（11:00–12:00）冲突，仍保留 10:00–11:00；
- 左闭右开：与他人端点相接可成功，任何重叠即冲突；目标预约自身不阻挡，
  新旧时段重叠、时段未变都成功；冲突只检查同资源的其他未取消预约；
- 改期后 day-query/free-query 按新时段显示占用、原时段释放，
  重新打开同一数据库后一致；冲突时原时段与取消状态不变，不存在只释放
  旧时段的中间结果；
- 改期不新增预约、不消耗标识、不改其他记录（含每周重复预约的其他各项）；
- 预约不存在、已经取消或标识大于 SQLite 整数上限一律 booking_not_found，
  已取消预约不能通过改期恢复；
- 缺少参数、不支持的参数（含 --repeat-weeks）、非法标识、非法时间或
  起止顺序错误统一 invalid_input，优先于预约存在性检查，非法输入不创建
  数据库文件、不迁移旧库、不改数据；
- 允许过去日期与跨日区间。
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

# 固定的预约日期与起止时间（固定 UTC+08:00 的本地时间，与运行环境无关）。
DAY = "2026-10-05"


def t(hour_start, hour_end=None, date=DAY):
    """便捷构造定宽时间文本：t("09:00", "10:00")。"""
    if hour_end is None:
        return f"{date}T{hour_start}"
    return f"{date}T{hour_start}", f"{date}T{hour_end}"


def tw(hour_start, hour_end, date=DAY):
    """直接拼命令行参数：tw(...) 展开为 ["--start", ..., "--end", ...]。"""
    return ["--start", f"{date}T{hour_start}", "--end", f"{date}T{hour_end}"]


# SQLite INTEGER 边界：2^63 起越界，等价于“标识不存在”。
OVER_MAX_ID = "9223372036854775808"


class RescheduleTestCase(unittest.TestCase):
    """每个用例一个独立的临时目录和数据库路径，tearDown 自动清理。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="booking-test-reschedule-")
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
        """运行命令并断言退出码 0、stdout 为单个 JSON 对象且 stderr 为空。"""
        proc = self.run_cli(*args, db=db)
        self.assertEqual(
            proc.returncode, 0,
            msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}",
        )
        self.assertEqual(proc.stderr, "", msg=f"stderr 不应有输出：{proc.stderr!r}")
        lines = proc.stdout.splitlines()
        self.assertEqual(len(lines), 1, msg=f"stdout 应只有一个 JSON 对象：{proc.stdout!r}")
        payload = json.loads(lines[0])
        self.assertIsInstance(payload, dict)
        return payload

    def run_error(self, expected_error, *args, db=None):
        """断言退出码 2、stdout 恰为指定错误对象、stderr 无任何输出。"""
        proc = self.run_cli(*args, db=db)
        self.assertEqual(
            proc.returncode, 2,
            msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}",
        )
        self.assertEqual(proc.stderr, "", msg=f"stderr 不应有输出：{proc.stderr!r}")
        lines = proc.stdout.splitlines()
        self.assertEqual(len(lines), 1, msg=f"stdout 应只有一个 JSON 对象：{proc.stdout!r}")
        payload = json.loads(lines[0])
        self.assertEqual(payload, {"error": expected_error})
        return proc

    def add_resource(self, name="一号会议室"):
        payload = self.run_ok("resource-add", "--name", name)
        self.assertEqual(payload["name"], name)
        return payload["resource_id"]

    def reserve_ok(self, resource_id, start, end):
        payload = self.run_ok(
            "reserve", "--resource", str(resource_id),
            "--start", start, "--end", end,
        )
        self.assertEqual(
            payload,
            {
                "booking_id": payload["booking_id"],
                "resource_id": resource_id,
                "start": start,
                "end": end,
            },
        )
        return payload["booking_id"]

    def reschedule_ok(self, booking_id, start, end):
        """改期成功：退出码 0，恰好返回四个字段，标识/资源不变、时间按原文。"""
        payload = self.run_ok(
            "reschedule", "--booking", str(booking_id),
            "--start", start, "--end", end,
        )
        self.assertEqual(
            payload,
            {
                "booking_id": booking_id,
                "resource_id": payload["resource_id"],
                "start": start,
                "end": end,
            },
        )
        return payload

    def day_query(self, resource_id, date=DAY):
        return self.run_ok(
            "day-query", "--resource", str(resource_id), "--date", date
        )

    def free_query(self, resource_id, start, end, *extra):
        return self.run_ok(
            "free-query", "--resource", str(resource_id),
            "--start", start, "--end", end, *extra,
        )

    def setup_two_bookings(self):
        """验收数据：资源 1 上预约 1 为 09:00–10:00、预约 2 为 11:00–12:00。"""
        resource_id = self.add_resource()
        self.assertEqual(resource_id, 1)
        b1 = self.reserve_ok(resource_id, *t("09:00", "10:00"))
        b2 = self.reserve_ok(resource_id, *t("11:00", "12:00"))
        self.assertEqual((b1, b2), (1, 2))
        return resource_id, b1, b2

    # ---- 验收流程：两次改期 ----

    def test_acceptance_two_reschedules(self):
        """预约 1 改到 10:00–11:00 成功且标识仍为 1；再改 10:30–11:30 冲突且保留 10:00–11:00。"""
        resource_id, b1, b2 = self.setup_two_bookings()

        payload = self.reschedule_ok(b1, *t("10:00", "11:00"))
        self.assertEqual(payload["resource_id"], 1)

        # 与预约 2 的 11:00–12:00 在 11:00–11:30 重叠 → 冲突。
        self.run_error(
            "booking_conflict",
            "reschedule", "--booking", str(b1), *tw("10:30", "11:30"),
        )

        # 预约 1 仍是 10:00–11:00，预约 2 不变；标识不新增。
        self.assertEqual(
            self.day_query(resource_id)["bookings"],
            [
                {"booking_id": 1, "start": f"{DAY}T10:00", "end": f"{DAY}T11:00"},
                {"booking_id": 2, "start": f"{DAY}T11:00", "end": f"{DAY}T12:00"},
            ],
        )

    def test_changes_visible_to_queries_and_persist_across_reopen(self):
        """改期后新时段占用、原时段释放；day-query/free-query 与重开后一致。"""
        resource_id, b1, _ = self.setup_two_bookings()
        self.reschedule_ok(b1, *t("10:00", "11:00"))

        # free-query 按新时段计算：09:00–10:00 已释放为空闲，
        # 10:00–12:00 被预约 1（新时段）与预约 2 端点相接连续占满。
        payload = self.free_query(resource_id, f"{DAY}T09:00", f"{DAY}T12:00")
        self.assertEqual(
            payload["free_slots"],
            [{"start": f"{DAY}T09:00", "end": f"{DAY}T10:00"}],
        )
        # 新时段参与冲突判断：与 10:00–11:00 重叠的预约被拒。
        self.run_error(
            "booking_conflict",
            "reserve", "--resource", "1", *tw("10:30", "11:30"),
        )

        # 原时段可再次预约，获得新标识 3（改期本身不新增预约、不消耗标识）。
        b3 = self.reserve_ok(resource_id, *t("09:00", "10:00"))
        self.assertEqual(b3, 3)
        # 原时段被新预约占用后，09:00–12:00 三段端点相接、窗口全忙。
        self.assertEqual(
            self.free_query(resource_id, f"{DAY}T09:00", f"{DAY}T12:00")["free_slots"],
            [],
        )

        # 重开同一数据库（每条命令都是独立进程）：新时段仍占用、原时段仍释放。
        self.run_error(
            "booking_conflict",
            "reserve", "--resource", "1", *tw("10:00", "11:00"),
        )
        self.assertEqual(
            self.day_query(resource_id)["bookings"],
            [
                {"booking_id": 3, "start": f"{DAY}T09:00", "end": f"{DAY}T10:00"},
                {"booking_id": 1, "start": f"{DAY}T10:00", "end": f"{DAY}T11:00"},
                {"booking_id": 2, "start": f"{DAY}T11:00", "end": f"{DAY}T12:00"},
            ],
        )

    # ---- 区间边界与自身重叠 ----

    def test_endpoint_touching_succeeds(self):
        """与他人预约端点相接（左闭右开）可成功。"""
        resource_id, b1, b2 = self.setup_two_bookings()
        # 09:00–10:00 改为 10:00–11:00：与预约 2 的 11:00 端点相接。
        self.reschedule_ok(b1, *t("10:00", "11:00"))
        # 预约 2 也可以改到与预约 1 的 10:00 端点相接：08:00–10:00。
        self.reschedule_ok(b2, *t("08:00", "10:00"))
        self.assertEqual(
            self.day_query(resource_id)["bookings"],
            [
                {"booking_id": 2, "start": f"{DAY}T08:00", "end": f"{DAY}T10:00"},
                {"booking_id": 1, "start": f"{DAY}T10:00", "end": f"{DAY}T11:00"},
            ],
        )

    def test_unchanged_window_succeeds_without_self_conflict(self):
        """时段未变也成功：目标预约自身不阻挡改期。"""
        resource_id, b1, _ = self.setup_two_bookings()
        self.reschedule_ok(b1, *t("09:00", "10:00"))
        self.assertEqual(
            self.day_query(resource_id)["bookings"],
            [
                {"booking_id": 1, "start": f"{DAY}T09:00", "end": f"{DAY}T10:00"},
                {"booking_id": 2, "start": f"{DAY}T11:00", "end": f"{DAY}T12:00"},
            ],
        )

    def test_overlapping_old_and_new_windows_allowed(self):
        """新旧时段重叠允许（不与任何他人冲突时）。"""
        resource_id = self.add_resource()
        b1 = self.reserve_ok(resource_id, *t("09:00", "10:00"))
        self.reschedule_ok(b1, *t("09:30", "10:30"))
        self.assertEqual(
            self.day_query(resource_id)["bookings"],
            [{"booking_id": 1, "start": f"{DAY}T09:30", "end": f"{DAY}T10:30"}],
        )

    def test_past_date_and_cross_day_window_allowed(self):
        """允许过去日期与跨日区间。"""
        resource_id = self.add_resource()
        b1 = self.reserve_ok(resource_id, *t("09:00", "10:00"))
        self.reschedule_ok(
            b1,
            f"{DAY}T23:30", f"2026-10-06T01:00",
        )
        self.assertEqual(
            self.day_query(resource_id)["bookings"],
            [{"booking_id": 1, "start": f"{DAY}T23:30", "end": "2026-10-06T01:00"}],
        )
        # 跨日预约在次日查询中也只出现一次、保留完整起止。
        self.assertEqual(
            self.day_query(resource_id, date="2026-10-06")["bookings"],
            [{"booking_id": 1, "start": f"{DAY}T23:30", "end": "2026-10-06T01:00"}],
        )

    # ---- 冲突语义 ----

    def test_conflict_keeps_old_window_and_cancelled_state(self):
        """冲突时原时段仍占用、取消状态不变，不存在只释放旧时段的结果。"""
        resource_id, b1, _ = self.setup_two_bookings()

        self.run_error(
            "booking_conflict",
            "reschedule", "--booking", str(b1), *tw("11:30", "12:30"),
        )

        # 原时段仍由预约 1 占用：同时段新预约冲突；且预约 1 仍未取消，
        # 可以再次取消（说明没有被任何中间写入影响）。
        self.run_error(
            "booking_conflict",
            "reserve", "--resource", "1", *tw("09:00", "10:00"),
        )
        payload = self.run_ok("cancel", "--booking", str(b1))
        self.assertEqual(payload, {"booking_id": b1, "cancelled": True})

    def test_conflict_only_checks_same_resource_active_bookings(self):
        """其他资源与已取消的预约不阻挡改期。"""
        r1 = self.add_resource()
        r2 = self.add_resource()
        self.assertEqual(r2, 2)
        b1 = self.reserve_ok(r1, *t("09:00", "10:00"))
        # 资源 2 在同一时段有预约，不阻挡资源 1 的改期。
        self.reserve_ok(r2, *t("10:00", "11:00"))
        self.reschedule_ok(b1, *t("10:00", "11:00"))

        # 资源 1 上另有一条预约，取消后同样不再阻挡。
        b3 = self.reserve_ok(r1, *t("12:00", "13:00"))
        self.run_ok("cancel", "--booking", str(b3))
        self.reschedule_ok(b1, *t("12:00", "13:00"))
        self.assertEqual(
            self.day_query(r1)["bookings"],
            [{"booking_id": 1, "start": f"{DAY}T12:00", "end": f"{DAY}T13:00"}],
        )

    def test_no_new_booking_no_id_consumption_other_records_unchanged(self):
        """成功改期不新增预约、不消耗标识、不改其他记录。"""
        resource_id, b1, b2 = self.setup_two_bookings()
        self.reschedule_ok(b1, *t("10:00", "11:00"))

        # 再次预约相邻时段获得标识 3（改期没有消耗任何标识）。
        b3 = self.reserve_ok(resource_id, *t("08:00", "09:00"))
        self.assertEqual(b3, 3)

        # 预约 2 原样保留，资源目录不变。
        self.assertEqual(
            self.run_ok("resource-list")["resources"],
            [{"resource_id": 1, "name": "一号会议室"}],
        )
        self.assertEqual(
            self.day_query(resource_id)["bookings"],
            [
                {"booking_id": 3, "start": f"{DAY}T08:00", "end": f"{DAY}T09:00"},
                {"booking_id": 1, "start": f"{DAY}T10:00", "end": f"{DAY}T11:00"},
                {"booking_id": 2, "start": f"{DAY}T11:00", "end": f"{DAY}T12:00"},
            ],
        )

    # ---- 不存在 / 已取消 / 超大标识 ----

    def test_missing_and_cancelled_and_oversized_return_not_found(self):
        """预约不存在、已经取消或标识超 SQLite 上限：booking_not_found，不改数据。"""
        resource_id, b1, _ = self.setup_two_bookings()
        before = self.day_query(resource_id)

        self.run_error(
            "booking_not_found",
            "reschedule", "--booking", "999", *tw("08:00", "08:30"),
        )
        self.run_error(
            "booking_not_found",
            "reschedule", "--booking", OVER_MAX_ID, *tw("08:00", "08:30"),
        )
        # 两条失败都未改任何记录。
        self.assertEqual(self.day_query(resource_id), before)

        # 已取消预约不能通过改期恢复。
        self.run_ok("cancel", "--booking", str(b1))
        self.run_error(
            "booking_not_found",
            "reschedule", "--booking", str(b1), *tw("08:00", "08:30"),
        )
        # 取消状态持久：预约 1 仍不出现在按日查询中，预约 2 不受影响。
        self.assertEqual(
            self.day_query(resource_id)["bookings"],
            [{"booking_id": 2, "start": f"{DAY}T11:00", "end": f"{DAY}T12:00"}],
        )

    # ---- 非法输入：统一 invalid_input，优先于存在性检查 ----

    def test_invalid_inputs_return_invalid_input(self):
        """缺参、不支持的参数（含 --repeat-weeks）、非法标识/时间、起止顺序错误。"""
        s, e = t("08:00", "08:30")
        cases = [
            ("缺少 --booking", ["reschedule", "--start", s, "--end", e]),
            ("缺少 --start", ["reschedule", "--booking", "1", "--end", e]),
            ("缺少 --end", ["reschedule", "--booking", "1", "--start", s]),
            ("不支持的 --repeat-weeks",
             ["reschedule", "--booking", "1", "--start", s, "--end", e,
              "--repeat-weeks", "2"]),
            ("不支持的其他参数",
             ["reschedule", "--booking", "1", "--start", s, "--end", e,
              "--resource", "1"]),
            ("标识为零", ["reschedule", "--booking", "0", "--start", s, "--end", e]),
            ("标识为负数", ["reschedule", "--booking", "-1", "--start", s, "--end", e]),
            ("标识为小数", ["reschedule", "--booking", "1.0", "--start", s, "--end", e]),
            ("标识为文本", ["reschedule", "--booking", "x", "--start", s, "--end", e]),
            ("开始格式错误",
             ["reschedule", "--booking", "1", "--start", f"{DAY} 08:00", "--end", e]),
            ("日期不存在",
             ["reschedule", "--booking", "1",
              "--start", "2026-02-30T08:00", "--end", e]),
            ("非 ASCII 数字时间",
             ["reschedule", "--booking", "1",
              "--start", f"{DAY}T０８:００", "--end", e]),
            ("起止相等", ["reschedule", "--booking", "1", "--start", s, "--end", s]),
            ("开始晚于结束",
             ["reschedule", "--booking", "1", "--start", e, "--end", s]),
        ]
        for label, args in cases:
            with self.subTest(label=label):
                self.run_error("invalid_input", *args)

    def test_invalid_input_takes_priority_and_leaves_state_unchanged(self):
        """非法输入优先于预约存在性与冲突检查，失败前后 day-query 一致。"""
        resource_id, b1, _ = self.setup_two_bookings()
        before = self.day_query(resource_id)

        # 未知/超大标识 + 非法时间：仍只报 invalid_input。
        self.run_error(
            "invalid_input",
            "reschedule", "--booking", "999",
            "--start", f"{DAY}T12:00", "--end", f"{DAY}T11:00",
        )
        self.run_error(
            "invalid_input",
            "reschedule", "--booking", OVER_MAX_ID,
            "--start", "not-a-time", "--end", f"{DAY}T11:00",
        )
        # 本会冲突的改期，只要时间非法也只报 invalid_input（不做冲突判断）。
        self.run_error(
            "invalid_input",
            "reschedule", "--booking", str(b1),
            "--start", f"{DAY}T11:30", "--end", f"{DAY}T11:00",
        )

        self.assertEqual(self.day_query(resource_id), before)

    def test_invalid_input_does_not_create_database_or_migrate_legacy(self):
        """非法输入指向不存在的路径时不创建文件。"""
        s, e = t("08:00", "08:30")
        cases = [
            ["reschedule"],
            ["reschedule", "--booking", "0", "--start", s, "--end", e],
            ["reschedule", "--booking", "1"],
            ["reschedule", "--booking", "1", "--start", s, "--end", e,
             "--repeat-weeks", "3"],
        ]
        for index, args in enumerate(cases):
            with self.subTest(args=args):
                missing_db = Path(self._tmpdir.name) / f"should-not-exist-{index}.sqlite"
                self.run_error("invalid_input", *args, db=missing_db)
                self.assertFalse(missing_db.exists())

    # ---- 每周重复预约：只改指定一项 ----

    def test_reschedule_changes_only_one_repeat_occurrence(self):
        """每周重复预约改期只作用于指定 booking_id，其他各项不受影响。"""
        resource_id = self.add_resource()
        payload = self.run_ok(
            "reserve", "--resource", str(resource_id),
            *tw("09:00", "10:00"), "--repeat-weeks", "3",
        )
        ids = [item["booking_id"] for item in payload["bookings"]]
        self.assertEqual(ids, [1, 2, 3])
        for week, booking_id in enumerate(ids):
            date = f"2026-10-{5 + 7 * week:02d}"
            self.assertEqual(
                self.day_query(resource_id, date=date)["bookings"],
                [{"booking_id": booking_id,
                  "start": f"{date}T09:00", "end": f"{date}T10:00"}],
            )

        # 只把第二项改到当天 14:00–15:00。
        self.reschedule_ok(
            ids[1], f"2026-10-12T14:00", f"2026-10-12T15:00"
        )

        self.assertEqual(
            self.day_query(resource_id, date="2026-10-05")["bookings"],
            [{"booking_id": 1, "start": "2026-10-05T09:00",
              "end": "2026-10-05T10:00"}],
        )
        self.assertEqual(
            self.day_query(resource_id, date="2026-10-12")["bookings"],
            [{"booking_id": 2, "start": "2026-10-12T14:00",
              "end": "2026-10-12T15:00"}],
        )
        self.assertEqual(
            self.day_query(resource_id, date="2026-10-19")["bookings"],
            [{"booking_id": 3, "start": "2026-10-19T09:00",
              "end": "2026-10-19T10:00"}],
        )


if __name__ == "__main__":
    unittest.main()
