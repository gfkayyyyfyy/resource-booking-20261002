"""cancel 取消后再次预约流程的回归测试（仅标准库，python -m unittest 可发现）。

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

# 固定的预约日期与时段（固定 UTC+08:00 的本地时间，与运行环境无关）。
BOOKING_DATE = "2026-10-05"
START = f"{BOOKING_DATE}T09:00"
END = f"{BOOKING_DATE}T10:00"


class CancelRebookTestCase(unittest.TestCase):
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
        """运行命令并断言退出码 2、输出为指定的错误对象。"""
        proc = self.run_cli(*args, db=db)
        self.assertEqual(proc.returncode, 2, msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        self.assertEqual(json.loads(proc.stdout), {"error": expected_error})
        return proc

    def add_resource(self, name="测试资源"):
        payload = self.run_ok("resource-add", "--name", name)
        self.assertIsInstance(payload["resource_id"], int)
        self.assertGreater(payload["resource_id"], 0)
        self.assertEqual(payload["name"], name)
        return payload["resource_id"]

    def reserve_ok(self, resource_id, start=START, end=END):
        """预约成功：返回的起止时间与输入文本完全一致，booking_id 为正整数。"""
        payload = self.run_ok(
            "reserve", "--resource", str(resource_id), "--start", start, "--end", end
        )
        self.assertIsInstance(payload["booking_id"], int)
        self.assertGreater(payload["booking_id"], 0)
        self.assertEqual(payload["resource_id"], resource_id)
        self.assertEqual(payload["start"], start)
        self.assertEqual(payload["end"], end)
        return payload["booking_id"]

    def cancel_ok(self, booking_id):
        """取消成功：返回原 booking_id 且 cancelled 为 true。"""
        payload = self.run_ok("cancel", "--booking", str(booking_id))
        self.assertEqual(payload, {"booking_id": booking_id, "cancelled": True})

    def day_query(self, resource_id, date=BOOKING_DATE):
        return self.run_ok("day-query", "--resource", str(resource_id), "--date", date)

    # ---- 核心流程：取消后再次预约 ----

    def test_cancel_then_rebook_same_slot_succeeds_with_fresh_id(self):
        """取消释放时段后可再次预约：新 booking_id 不复用旧标识，时间保持原文本。"""
        resource_id = self.add_resource()
        old_id = self.reserve_ok(resource_id)

        self.cancel_ok(old_id)

        # 同一资源、原起止时间再次预约成功，返回新的正整数标识。
        new_id = self.reserve_ok(resource_id)
        self.assertNotEqual(new_id, old_id)

    def test_old_id_stays_cancelled_and_does_not_touch_new_booking(self):
        """再次取消旧标识返回 booking_not_found，且不影响后来创建的预约。"""
        resource_id = self.add_resource()
        old_id = self.reserve_ok(resource_id)
        self.cancel_ok(old_id)
        new_id = self.reserve_ok(resource_id)

        # 旧标识已取消，再次取消报 booking_not_found。
        self.run_error("booking_not_found", "cancel", "--booking", str(old_id))

        # 上一步不能取消新预约：同时段再预约应冲突，day-query 只显示新预约。
        self.run_error(
            "booking_conflict",
            "reserve", "--resource", str(resource_id), "--start", START, "--end", END,
        )
        payload = self.day_query(resource_id)
        self.assertEqual(
            payload,
            {
                "resource_id": resource_id,
                "date": BOOKING_DATE,
                "bookings": [{"booking_id": new_id, "start": START, "end": END}],
            },
        )

    def test_cancel_rebook_state_survives_reopening_database(self):
        """重新打开同一数据库（新进程）后，取消状态、重新预约与旧标识隔离保持一致。"""
        resource_id = self.add_resource()
        old_id = self.reserve_ok(resource_id)
        self.cancel_ok(old_id)
        new_id = self.reserve_ok(resource_id)

        # 每条命令都是独立进程，以下断言即“重新打开同一数据库”后的行为：
        # 旧标识仍然不可取消，新预约仍占用时段，按日查询结果不变。
        self.run_error("booking_not_found", "cancel", "--booking", str(old_id))
        self.run_error(
            "booking_conflict",
            "reserve", "--resource", str(resource_id), "--start", START, "--end", END,
        )
        payload = self.day_query(resource_id)
        self.assertEqual(
            payload["bookings"],
            [{"booking_id": new_id, "start": START, "end": END}],
        )
        # 新标识仍可正常取消，进一步确认旧标识的取消没有波及它。
        self.cancel_ok(new_id)
        self.assertEqual(self.day_query(resource_id)["bookings"], [])

    # ---- 失败路径 ----

    def test_unknown_positive_booking_id_returns_booking_not_found(self):
        """正整数但预约标识不存在：booking_not_found，退出码 2。"""
        resource_id = self.add_resource()
        booking_id = self.reserve_ok(resource_id)
        self.run_error("booking_not_found", "cancel", "--booking", str(booking_id + 100))

    def test_invalid_booking_ids_return_invalid_input_and_keep_data(self):
        """缺少参数或标识为零/负数/小数/非整数文本：invalid_input，退出码 2，数据不变。"""
        resource_id = self.add_resource()
        booking_id = self.reserve_ok(resource_id)
        before = self.day_query(resource_id)

        cases = [
            ("缺少 --booking", ["cancel"]),
            ("booking id 为零", ["cancel", "--booking", "0"]),
            ("booking id 为负数", ["cancel", "--booking", "-3"]),
            ("booking id 为小数", ["cancel", "--booking", "1.5"]),
            ("booking id 为非整数文本", ["cancel", "--booking", "abc"]),
        ]
        for label, args in cases:
            with self.subTest(label=label):
                self.run_error("invalid_input", *args)

        # 失败分支不改动任何记录：按日查询结果与失败前一致。
        after = self.day_query(resource_id)
        self.assertEqual(after, before)
        self.assertEqual(
            after["bookings"],
            [{"booking_id": booking_id, "start": START, "end": END}],
        )

    def test_invalid_booking_id_does_not_create_database_file(self):
        """非法标识指向尚不存在的数据库路径时，优先报告输入错误且不得创建文件。"""
        missing_db = Path(self._tmpdir.name) / "should-not-exist.sqlite"
        self.run_error(
            "invalid_input",
            "cancel", "--booking", "abc",
            db=missing_db,
        )
        self.assertFalse(missing_db.exists())


if __name__ == "__main__":
    unittest.main()
