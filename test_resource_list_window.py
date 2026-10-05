"""resource-list --start/--end 时段可预约筛选的回归测试（仅标准库，python -m unittest 可发现）。

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

# 固定的查询日期（固定 UTC+08:00 的本地日期，与运行环境无关）。
QUERY_DATE = "2026-10-05"


class ResourceListWindowTestCase(unittest.TestCase):
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
        """运行命令并断言退出码 0、stderr 为空、标准输出为单个 JSON 对象。"""
        proc = self.run_cli(*args, db=db)
        self.assertEqual(proc.returncode, 0, msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        self.assertEqual(proc.stderr, "", msg=f"stderr={proc.stderr!r}")
        payload = json.loads(proc.stdout)  # 若输出不是单个 JSON 文档会抛错
        self.assertIsInstance(payload, dict)
        return payload

    def run_error(self, expected_error, *args, db=None):
        """运行命令并断言退出码 2、stderr 为空、输出为指定的错误对象。"""
        proc = self.run_cli(*args, db=db)
        self.assertEqual(proc.returncode, 2, msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        self.assertEqual(proc.stderr, "", msg=f"stderr={proc.stderr!r}")
        self.assertEqual(json.loads(proc.stdout), {"error": expected_error})
        return proc

    def add_resource(self, name):
        payload = self.run_ok("resource-add", "--name", name)
        return payload["resource_id"]

    def reserve(self, resource_id, start, end):
        payload = self.run_ok(
            "reserve", "--resource", str(resource_id), "--start", start, "--end", end
        )
        return payload["booking_id"]

    def cancel(self, booking_id):
        payload = self.run_ok("cancel", "--booking", str(booking_id))
        self.assertEqual(payload, {"booking_id": booking_id, "cancelled": True})

    def list_resources(self, *extra_args, db=None):
        """运行 resource-list（可带 --contains/--start/--end）并断言结构。"""
        payload = self.run_ok("resource-list", *extra_args, db=db)
        # 顶层只有 resources 一个键。
        self.assertEqual(set(payload.keys()), {"resources"})
        resources = payload["resources"]
        self.assertIsInstance(resources, list)
        for item in resources:
            # 每项仅含 resource_id 与 name。
            self.assertEqual(set(item.keys()), {"resource_id", "name"})
            self.assertIsInstance(item["resource_id"], int)
            self.assertIsInstance(item["name"], str)
        return resources

    # ---- 成功路径 ----

    def test_acceptance_scenario(self):
        """验收场景：资源 1 约 09:00–10:00、资源 2 约 10:00–11:00。

        查询 10:00–11:00 只返回资源 1；查询 09:30–10:30 返回空数组。
        """
        first_id = self.add_resource("一号会议室")
        second_id = self.add_resource("投影仪")
        self.assertEqual([first_id, second_id], [1, 2])
        self.reserve(1, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")
        self.reserve(2, f"{QUERY_DATE}T10:00", f"{QUERY_DATE}T11:00")

        self.assertEqual(
            self.list_resources(
                "--start", f"{QUERY_DATE}T10:00", "--end", f"{QUERY_DATE}T11:00"
            ),
            [{"resource_id": 1, "name": "一号会议室"}],
        )
        self.assertEqual(
            self.list_resources(
                "--start", f"{QUERY_DATE}T09:30", "--end", f"{QUERY_DATE}T10:30"
            ),
            [],
        )

    def test_window_omitted_keeps_full_listing(self):
        """两端都省略时保留原目录行为：有预约的资源照样返回。"""
        self.add_resource("一号会议室")
        self.add_resource("投影仪")
        self.reserve(1, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")

        self.assertEqual(
            self.list_resources(),
            [
                {"resource_id": 1, "name": "一号会议室"},
                {"resource_id": 2, "name": "投影仪"},
            ],
        )

    def test_touching_endpoints_do_not_block(self):
        """左闭右开：预约结束等于查询开始、预约开始等于查询结束都不阻挡。"""
        self.add_resource("一号会议室")
        self.reserve(1, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")
        self.reserve(1, f"{QUERY_DATE}T11:00", f"{QUERY_DATE}T12:00")

        # 查询窗口与两条预约分别端点相接，资源仍全程可预约。
        self.assertEqual(
            self.list_resources(
                "--start", f"{QUERY_DATE}T10:00", "--end", f"{QUERY_DATE}T11:00"
            ),
            [{"resource_id": 1, "name": "一号会议室"}],
        )

    def test_any_positive_overlap_excludes_resource(self):
        """任何正时长重叠（含、被含、部分相交、完全相同）都排除该资源。"""
        self.add_resource("一号会议室")
        self.reserve(1, f"{QUERY_DATE}T10:00", f"{QUERY_DATE}T11:00")

        for start, end in [
            ("09:00", "12:00"),  # 窗口包含预约
            ("10:15", "10:45"),  # 窗口被预约包含
            ("09:30", "10:30"),  # 左侧部分相交
            ("10:30", "11:30"),  # 右侧部分相交
            ("10:00", "11:00"),  # 完全相同
        ]:
            self.assertEqual(
                self.list_resources(
                    "--start", f"{QUERY_DATE}T{start}", "--end", f"{QUERY_DATE}T{end}"
                ),
                [],
                msg=f"window {start}-{end} should exclude the resource",
            )

    def test_cancelled_booking_does_not_block(self):
        """已取消预约不占用：取消后同一窗口重新可约。"""
        self.add_resource("一号会议室")
        booking_id = self.reserve(1, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")
        window = ("--start", f"{QUERY_DATE}T09:00", "--end", f"{QUERY_DATE}T10:00")

        self.assertEqual(self.list_resources(*window), [])
        self.cancel(booking_id)
        self.assertEqual(
            self.list_resources(*window),
            [{"resource_id": 1, "name": "一号会议室"}],
        )

    def test_resources_without_bookings_are_returned(self):
        """没有预约的资源可返回；各资源独立判断。"""
        self.add_resource("一号会议室")
        self.add_resource("投影仪")
        self.reserve(1, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")

        self.assertEqual(
            self.list_resources(
                "--start", f"{QUERY_DATE}T09:00", "--end", f"{QUERY_DATE}T10:00"
            ),
            [{"resource_id": 2, "name": "投影仪"}],
        )

    def test_cross_midnight_window_uses_full_datetime(self):
        """跨午夜区间按完整日期时间判断。"""
        self.add_resource("一号会议室")
        self.add_resource("投影仪")
        # 资源 1 的预约跨午夜，与跨日查询窗口相交。
        self.reserve(1, "2026-10-05T23:00", "2026-10-06T01:00")

        self.assertEqual(
            self.list_resources(
                "--start", "2026-10-05T22:00", "--end", "2026-10-06T02:00"
            ),
            [{"resource_id": 2, "name": "投影仪"}],
        )
        # 窗口只覆盖 23:00 之前的部分，不与预约相交。
        self.assertEqual(
            self.list_resources(
                "--start", "2026-10-05T21:00", "--end", "2026-10-05T23:00"
            ),
            [
                {"resource_id": 1, "name": "一号会议室"},
                {"resource_id": 2, "name": "投影仪"},
            ],
        )

    def test_past_dates_are_allowed(self):
        """允许过去日期的查询窗口。"""
        self.add_resource("一号会议室")
        self.reserve(1, "2020-01-01T09:00", "2020-01-01T10:00")

        self.assertEqual(
            self.list_resources("--start", "2020-01-01T09:30", "--end", "2020-01-01T10:30"),
            [],
        )
        self.assertEqual(
            self.list_resources("--start", "2020-01-01T10:00", "--end", "2020-01-01T11:00"),
            [{"resource_id": 1, "name": "一号会议室"}],
        )

    def test_combined_with_contains_requires_both_conditions(self):
        """与 --contains 同用时，名称和时段条件同时满足才返回。"""
        self.add_resource("一号会议室")
        self.add_resource("二号会议室")
        self.add_resource("投影仪")
        # 资源 1 在窗口内被占用；资源 2 名称命中且空闲；资源 3 空闲但名称不命中。
        self.reserve(1, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")

        self.assertEqual(
            self.list_resources(
                "--contains", "会议室",
                "--start", f"{QUERY_DATE}T09:00", "--end", f"{QUERY_DATE}T10:00",
            ),
            [{"resource_id": 2, "name": "二号会议室"}],
        )

    def test_same_name_resources_are_independent(self):
        """同名资源各自独立判断、分别返回，按标识数值升序。"""
        self.add_resource("会议室")
        self.add_resource("会议室")
        self.reserve(1, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")

        self.assertEqual(
            self.list_resources(
                "--start", f"{QUERY_DATE}T09:00", "--end", f"{QUERY_DATE}T10:00"
            ),
            [{"resource_id": 2, "name": "会议室"}],
        )

    def test_repeat_bookings_are_ordinary_bookings(self):
        """重复预约生成的记录按普通预约处理：任一区间相交即排除。"""
        self.add_resource("一号会议室")
        self.run_ok(
            "reserve", "--resource", "1",
            "--start", f"{QUERY_DATE}T09:00", "--end", f"{QUERY_DATE}T10:00",
            "--repeat-weeks", "2",
        )

        # 第二周（2026-10-12）的重复预约同样占用。
        self.assertEqual(
            self.list_resources(
                "--start", "2026-10-12T09:30", "--end", "2026-10-12T10:30"
            ),
            [],
        )

    def test_empty_catalog_returns_empty_list(self):
        """目录为空时带窗口查询成功返回空数组。"""
        self.assertEqual(
            self.list_resources(
                "--start", f"{QUERY_DATE}T09:00", "--end", f"{QUERY_DATE}T10:00"
            ),
            [],
        )

    def test_all_booked_returns_empty_list(self):
        """全部被占用时成功返回空数组。"""
        self.add_resource("一号会议室")
        self.add_resource("投影仪")
        self.reserve(1, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")
        self.reserve(2, f"{QUERY_DATE}T09:30", f"{QUERY_DATE}T10:30")

        self.assertEqual(
            self.list_resources(
                "--start", f"{QUERY_DATE}T09:00", "--end", f"{QUERY_DATE}T10:00"
            ),
            [],
        )

    def test_window_query_is_read_only(self):
        """时段筛选查询不增改删记录，不消耗资源与预约标识。"""
        self.add_resource("一号会议室")
        booking_id = self.reserve(1, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")

        before = self.list_resources()
        self.list_resources("--start", f"{QUERY_DATE}T09:00", "--end", f"{QUERY_DATE}T10:00")
        self.list_resources("--start", f"{QUERY_DATE}T10:00", "--end", f"{QUERY_DATE}T11:00")

        self.assertEqual(self.list_resources(), before)
        # 查询未消耗标识：下一个资源与下一条预约都紧接原有最大标识。
        self.assertEqual(self.add_resource("投影仪"), 2)
        self.assertEqual(
            self.reserve(1, f"{QUERY_DATE}T10:00", f"{QUERY_DATE}T11:00"),
            booking_id + 1,
        )

    def test_window_query_on_missing_database_returns_empty_list(self):
        """数据库尚不存在时沿用初始化行为，返回空数组并成功退出。"""
        self.assertFalse(self.db_path.exists())

        self.assertEqual(
            self.list_resources(
                "--start", f"{QUERY_DATE}T09:00", "--end", f"{QUERY_DATE}T10:00"
            ),
            [],
        )
        # 合法查询沿用了初始化行为，数据库文件已被创建。
        self.assertTrue(self.db_path.exists())
        # 空目录查询不消耗标识：首次登记的资源标识仍为 1。
        self.assertEqual(self.add_resource("一号会议室"), 1)

    # ---- 失败路径 ----

    def test_only_one_endpoint_returns_invalid_input(self):
        """只提供 --start 或只提供 --end：invalid_input、退出码 2、stderr 为空。"""
        self.run_error("invalid_input", "resource-list", "--start", f"{QUERY_DATE}T09:00")
        self.run_error("invalid_input", "resource-list", "--end", f"{QUERY_DATE}T10:00")

    def test_missing_value_returns_invalid_input(self):
        """--start 或 --end 缺少值：invalid_input。"""
        self.run_error(
            "invalid_input", "resource-list",
            "--start", f"{QUERY_DATE}T09:00", "--end",
        )
        self.run_error("invalid_input", "resource-list", "--start")

    def test_invalid_date_returns_invalid_input(self):
        """非法日期（不存在的日历日）：invalid_input。"""
        self.run_error(
            "invalid_input", "resource-list",
            "--start", "2026-02-30T09:00", "--end", "2026-03-01T10:00",
        )
        self.run_error(
            "invalid_input", "resource-list",
            "--start", f"{QUERY_DATE}T09:00", "--end", "2026-13-01T10:00",
        )

    def test_bad_format_returns_invalid_input(self):
        """非严格 YYYY-MM-DDTHH:mm 格式：invalid_input。"""
        good = f"{QUERY_DATE}T10:00"
        for bad in [
            "2026-10-05",             # 只有日期
            "2026-10-05 09:00",       # 空格分隔
            "2026-10-05T09:00:00",    # 带秒
            "2026-10-05T09:00+08:00",  # 带时区后缀
            "2026-10-05T9:00",        # 未零填充
            "2026/10/05T09:00",       # 错误分隔符
        ]:
            self.run_error(
                "invalid_input", "resource-list", "--start", bad, "--end", good,
            )
            self.run_error(
                "invalid_input", "resource-list", "--start", good, "--end", bad,
            )

    def test_non_ascii_digits_return_invalid_input(self):
        """非 ASCII 数字（全角、阿拉伯文等）：invalid_input。"""
        good = f"{QUERY_DATE}T10:00"
        for bad in ["２０２６-10-05T09:00", "2026-10-05T09:٠٠"]:
            self.run_error(
                "invalid_input", "resource-list", "--start", bad, "--end", good,
            )

    def test_surrounding_whitespace_returns_invalid_input(self):
        """首尾空白不去除：invalid_input。"""
        good = f"{QUERY_DATE}T10:00"
        for bad in [f" {QUERY_DATE}T09:00", f"{QUERY_DATE}T09:00 ", f"{QUERY_DATE}T09:00\n"]:
            self.run_error(
                "invalid_input", "resource-list", "--start", bad, "--end", good,
            )

    def test_start_not_before_end_returns_invalid_input(self):
        """开始不早于结束（相等或颠倒）：invalid_input。"""
        self.run_error(
            "invalid_input", "resource-list",
            "--start", f"{QUERY_DATE}T10:00", "--end", f"{QUERY_DATE}T10:00",
        )
        self.run_error(
            "invalid_input", "resource-list",
            "--start", f"{QUERY_DATE}T11:00", "--end", f"{QUERY_DATE}T10:00",
        )

    def test_invalid_window_does_not_create_database_file(self):
        """非法窗口指向尚不存在的数据库路径时，不得创建文件。"""
        missing_db = Path(self._tmpdir.name) / "should-not-exist.sqlite"
        self.run_error(
            "invalid_input",
            "resource-list", "--start", f"{QUERY_DATE}T10:00",
            db=missing_db,
        )
        self.run_error(
            "invalid_input",
            "resource-list",
            "--start", f"{QUERY_DATE}T11:00", "--end", f"{QUERY_DATE}T10:00",
            db=missing_db,
        )
        self.assertFalse(missing_db.exists())

    def test_invalid_window_does_not_change_existing_database(self):
        """已有数据库上非法窗口失败后，资源目录与预约查询结果不变。"""
        self.add_resource("一号会议室")
        self.reserve(1, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")
        before = self.list_resources()

        self.run_error(
            "invalid_input", "resource-list",
            "--start", f"{QUERY_DATE}T09:00", "--end", f"{QUERY_DATE}T09:00",
        )

        self.assertEqual(self.list_resources(), before)
        # 失败不消耗标识：下一条预约仍获得紧接原有最大标识的标识。
        self.assertEqual(
            self.reserve(1, f"{QUERY_DATE}T10:00", f"{QUERY_DATE}T11:00"), 2
        )


if __name__ == "__main__":
    unittest.main()
