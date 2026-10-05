"""day-query --include-cancelled 开关的回归测试（仅标准库，python -m unittest 可发现）。

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


class DayQueryIncludeCancelledTestCase(unittest.TestCase):
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
        self.assertEqual(proc.stderr, "")
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
        return self.run_ok("resource-add", "--name", name)["resource_id"]

    def reserve(self, resource_id, start, end):
        return self.run_ok(
            "reserve", "--resource", str(resource_id), "--start", start, "--end", end
        )["booking_id"]

    def cancel(self, booking_id):
        payload = self.run_ok("cancel", "--booking", str(booking_id))
        self.assertEqual(payload, {"booking_id": booking_id, "cancelled": True})

    def day_query(self, resource_id, date=QUERY_DATE, include_cancelled=False):
        args = ["day-query", "--resource", str(resource_id), "--date", date]
        if include_cancelled:
            args.append("--include-cancelled")
        return self.run_ok(*args)

    # ---- 验收场景 ----

    def test_acceptance_scenario(self):
        """09:00-10:00 未取消、10:00-11:00 已取消：缺省只返回前者且无状态字段，
        加开关返回两项并依次标记 false、true。"""
        resource_id = self.add_resource()
        kept_id = self.reserve(resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")
        cancelled_id = self.reserve(resource_id, f"{QUERY_DATE}T10:00", f"{QUERY_DATE}T11:00")
        self.cancel(cancelled_id)

        default_payload = self.day_query(resource_id)
        self.assertEqual(
            default_payload,
            {
                "resource_id": resource_id,
                "date": QUERY_DATE,
                "bookings": [
                    {"booking_id": kept_id, "start": f"{QUERY_DATE}T09:00", "end": f"{QUERY_DATE}T10:00"}
                ],
            },
        )
        # 缺省结果不附加任何状态字段。
        for item in default_payload["bookings"]:
            self.assertNotIn("cancelled", item)

        full_payload = self.day_query(resource_id, include_cancelled=True)
        self.assertEqual(full_payload["resource_id"], resource_id)
        self.assertEqual(full_payload["date"], QUERY_DATE)
        self.assertEqual(
            full_payload["bookings"],
            [
                {"booking_id": kept_id, "start": f"{QUERY_DATE}T09:00",
                 "end": f"{QUERY_DATE}T10:00", "cancelled": False},
                {"booking_id": cancelled_id, "start": f"{QUERY_DATE}T10:00",
                 "end": f"{QUERY_DATE}T11:00", "cancelled": True},
            ],
        )

    def test_cancelled_items_sorted_by_start_then_id_not_pushed_to_end(self):
        """取消记录不单独排在末尾：与未取消记录一起按 start 升序、再按标识升序。"""
        resource_id = self.add_resource()
        # 取消的记录开始时间早于未取消的记录。
        cancelled_id = self.reserve(resource_id, f"{QUERY_DATE}T08:00", f"{QUERY_DATE}T09:00")
        kept_id = self.reserve(resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")
        self.cancel(cancelled_id)

        payload = self.day_query(resource_id, include_cancelled=True)

        self.assertEqual(
            payload["bookings"],
            [
                {"booking_id": cancelled_id, "start": f"{QUERY_DATE}T08:00",
                 "end": f"{QUERY_DATE}T09:00", "cancelled": True},
                {"booking_id": kept_id, "start": f"{QUERY_DATE}T09:00",
                 "end": f"{QUERY_DATE}T10:00", "cancelled": False},
            ],
        )

    def test_rebooked_slot_after_cancel_returns_both_records(self):
        """取消后重订同一时段：新旧记录各自返回，不合并。"""
        resource_id = self.add_resource()
        old_id = self.reserve(resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")
        self.cancel(old_id)
        new_id = self.reserve(resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")

        payload = self.day_query(resource_id, include_cancelled=True)

        self.assertEqual(
            payload["bookings"],
            [
                {"booking_id": old_id, "start": f"{QUERY_DATE}T09:00",
                 "end": f"{QUERY_DATE}T10:00", "cancelled": True},
                {"booking_id": new_id, "start": f"{QUERY_DATE}T09:00",
                 "end": f"{QUERY_DATE}T10:00", "cancelled": False},
            ],
        )
        # 缺省查询只返回重订后的未取消记录。
        default_payload = self.day_query(resource_id)
        self.assertEqual(
            default_payload["bookings"],
            [{"booking_id": new_id, "start": f"{QUERY_DATE}T09:00", "end": f"{QUERY_DATE}T10:00"}],
        )

    def test_cross_day_cancelled_booking_listed_once_untruncated(self):
        """跨日的已取消预约：加开关时出现一次且保留完整起止时间。"""
        resource_id = self.add_resource()
        cancelled_id = self.reserve(resource_id, f"{PREV_DATE}T22:00", f"{QUERY_DATE}T01:30")
        self.cancel(cancelled_id)

        payload = self.day_query(resource_id, include_cancelled=True)

        self.assertEqual(
            payload["bookings"],
            [
                {"booking_id": cancelled_id, "start": f"{PREV_DATE}T22:00",
                 "end": f"{QUERY_DATE}T01:30", "cancelled": True}
            ],
        )

    def test_boundary_touching_cancelled_bookings_are_excluded(self):
        """加开关时边界规则不变：结束于当天 00:00、开始于次日 00:00 的取消记录仍不含。"""
        resource_id = self.add_resource()
        before_id = self.reserve(resource_id, f"{PREV_DATE}T22:00", f"{QUERY_DATE}T00:00")
        after_id = self.reserve(resource_id, f"{NEXT_DATE}T00:00", f"{NEXT_DATE}T02:00")
        self.cancel(before_id)
        self.cancel(after_id)

        payload = self.day_query(resource_id, include_cancelled=True)

        self.assertEqual(
            payload,
            {"resource_id": resource_id, "date": QUERY_DATE, "bookings": []},
        )

    def test_empty_day_with_flag_returns_empty_list(self):
        """资源存在但当天没有匹配记录：加开关仍成功返回空数组。"""
        resource_id = self.add_resource()
        other_day_id = self.reserve(resource_id, f"{PREV_DATE}T09:00", f"{PREV_DATE}T10:00")
        self.cancel(other_day_id)

        payload = self.day_query(resource_id, include_cancelled=True)

        self.assertEqual(
            payload,
            {"resource_id": resource_id, "date": QUERY_DATE, "bookings": []},
        )

    def test_last_representable_day_with_flag(self):
        """9999-12-31 仍为合法查询日：加开关时取消记录同样纳入。"""
        resource_id = self.add_resource()
        kept_id = self.reserve(resource_id, "9999-12-31T09:00", "9999-12-31T10:00")
        cancelled_id = self.reserve(resource_id, "9999-12-31T10:00", "9999-12-31T11:00")
        self.cancel(cancelled_id)

        payload = self.day_query(resource_id, date="9999-12-31", include_cancelled=True)

        self.assertEqual(
            payload["bookings"],
            [
                {"booking_id": kept_id, "start": "9999-12-31T09:00",
                 "end": "9999-12-31T10:00", "cancelled": False},
                {"booking_id": cancelled_id, "start": "9999-12-31T10:00",
                 "end": "9999-12-31T11:00", "cancelled": True},
            ],
        )

    # ---- 失败路径 ----

    def test_flag_with_any_value_is_invalid_input(self):
        """给无值开关附加任何值（= 形式或后随文本）都返回 invalid_input。"""
        cases = [
            ("等号赋值", ["day-query", "--resource", "1", "--date", QUERY_DATE,
                          "--include-cancelled=true"]),
            ("后随值", ["day-query", "--resource", "1", "--date", QUERY_DATE,
                        "--include-cancelled", "true"]),
        ]
        for label, args in cases:
            with self.subTest(label=label):
                self.run_error("invalid_input", *args)

    def test_flag_value_invalid_input_does_not_create_database_file(self):
        """开关附加值属于非法输入：不得创建数据库文件。"""
        missing_db = Path(self._tmpdir.name) / "should-not-exist.sqlite"
        self.run_error(
            "invalid_input",
            "day-query", "--resource", "1", "--date", QUERY_DATE,
            "--include-cancelled=x",
            db=missing_db,
        )
        self.assertFalse(missing_db.exists())

    def test_missing_params_and_invalid_date_still_invalid_input_with_flag(self):
        """加开关时缺少必填参数、日期非法、标识非法仍为 invalid_input。"""
        cases = [
            ("缺少 --date", ["day-query", "--resource", "1", "--include-cancelled"]),
            ("缺少 --resource", ["day-query", "--date", QUERY_DATE, "--include-cancelled"]),
            ("日期非法", ["day-query", "--resource", "1", "--date", "2026-02-30",
                          "--include-cancelled"]),
            ("标识为零", ["day-query", "--resource", "0", "--date", QUERY_DATE,
                          "--include-cancelled"]),
        ]
        for label, args in cases:
            with self.subTest(label=label):
                self.run_error("invalid_input", *args)

    def test_unknown_resource_with_flag_returns_resource_not_found(self):
        """输入合法而资源不存在（含超过 SQLite 整数上限的标识）：resource_not_found。"""
        resource_id = self.add_resource()
        self.run_error(
            "resource_not_found",
            "day-query", "--resource", str(resource_id + 100), "--date", QUERY_DATE,
            "--include-cancelled",
        )
        self.run_error(
            "resource_not_found",
            "day-query", "--resource", "9223372036854775808", "--date", QUERY_DATE,
            "--include-cancelled",
        )


if __name__ == "__main__":
    unittest.main()
