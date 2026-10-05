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


class ResourceListAvailabilityTestCase(unittest.TestCase):
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

    def reserve(self, resource_id, start, end, *extra):
        payload = self.run_ok(
            "reserve", "--resource", str(resource_id),
            "--start", start, "--end", end, *extra,
        )
        return payload

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

    def list_available(self, start, end, *extra_args, db=None):
        """带成对 --start/--end 的 resource-list。"""
        return self.list_resources(
            "--start", start, "--end", end, *extra_args, db=db
        )

    # ---- 成功路径：时段筛选 ----

    def test_window_filters_resources_with_overlapping_bookings(self):
        """规格示例：10:00–11:00 查询只返回资源 1；09:30–10:30 查询返回空数组。"""
        first_id = self.add_resource("一号会议室")
        second_id = self.add_resource("二号会议室")
        self.assertEqual([first_id, second_id], [1, 2])
        self.reserve(first_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")
        self.reserve(second_id, f"{QUERY_DATE}T10:00", f"{QUERY_DATE}T11:00")

        self.assertEqual(
            self.list_available(f"{QUERY_DATE}T10:00", f"{QUERY_DATE}T11:00"),
            [{"resource_id": first_id, "name": "一号会议室"}],
        )
        self.assertEqual(
            self.list_available(f"{QUERY_DATE}T09:30", f"{QUERY_DATE}T10:30"),
            [],
        )

    def test_window_omitted_keeps_full_listing(self):
        """--start/--end 均省略时保留原目录行为：预约不影响目录。"""
        resource_id = self.add_resource("一号会议室")
        self.reserve(resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")

        self.assertEqual(
            self.list_resources(),
            [{"resource_id": resource_id, "name": "一号会议室"}],
        )

    def test_resource_without_bookings_is_returned(self):
        """没有任何预约的资源在时段查询中返回。"""
        self.add_resource("一号会议室")
        self.add_resource("投影仪")

        self.assertEqual(
            self.list_available(f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00"),
            [
                {"resource_id": 1, "name": "一号会议室"},
                {"resource_id": 2, "name": "投影仪"},
            ],
        )

    def test_touching_endpoints_do_not_block(self):
        """左闭右开：预约结束等于查询开始、预约开始等于查询结束都不阻挡。"""
        resource_id = self.add_resource("一号会议室")
        self.reserve(resource_id, f"{QUERY_DATE}T08:00", f"{QUERY_DATE}T09:00")
        self.reserve(resource_id, f"{QUERY_DATE}T10:00", f"{QUERY_DATE}T11:00")

        self.assertEqual(
            self.list_available(f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00"),
            [{"resource_id": resource_id, "name": "一号会议室"}],
        )

    def test_any_positive_overlap_blocks(self):
        """任何正时长重叠都排除：部分重叠、包含、被包含与完全相同。"""
        # 每个资源各有一种与 09:00–10:00 相交的占用方式。
        cases = [
            ("08:30", "09:30"),  # 前段部分重叠
            ("09:30", "10:30"),  # 后段部分重叠
            ("08:00", "11:00"),  # 包含查询窗口
            ("09:15", "09:45"),  # 被查询窗口包含
            ("09:00", "10:00"),  # 完全相同
        ]
        for index, (start, end) in enumerate(cases, start=1):
            resource_id = self.add_resource(f"会议室{index}")
            self.reserve(
                resource_id, f"{QUERY_DATE}T{start}", f"{QUERY_DATE}T{end}"
            )
        free_id = self.add_resource("空闲会议室")

        self.assertEqual(
            self.list_available(f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00"),
            [{"resource_id": free_id, "name": "空闲会议室"}],
        )

    def test_cancelled_booking_does_not_occupy(self):
        """已取消预约不占用：取消同时段的预约后资源恢复可预约。"""
        resource_id = self.add_resource("一号会议室")
        payload = self.reserve(
            resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00"
        )
        self.cancel(payload["booking_id"])

        self.assertEqual(
            self.list_available(f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00"),
            [{"resource_id": resource_id, "name": "一号会议室"}],
        )

    def test_resources_are_judged_independently(self):
        """各资源独立判断：一个资源的预约不影响其他资源。"""
        busy_id = self.add_resource("忙碌会议室")
        free_id = self.add_resource("空闲会议室")
        self.reserve(busy_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")

        self.assertEqual(
            self.list_available(f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00"),
            [{"resource_id": free_id, "name": "空闲会议室"}],
        )

    def test_repeat_weeks_bookings_are_ordinary_occupancy(self):
        """重复预约生成的各条记录按普通预约处理：任一每周区间相交即排除。"""
        resource_id = self.add_resource("一号会议室")
        self.reserve(
            resource_id,
            f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00",
            "--repeat-weeks", "2",
        )

        # 首次区间相交。
        self.assertEqual(
            self.list_available(f"{QUERY_DATE}T09:30", f"{QUERY_DATE}T10:30"),
            [],
        )
        # 一周后生成的第二条区间相交。
        self.assertEqual(
            self.list_available("2026-10-12T09:30", "2026-10-12T10:30"),
            [],
        )
        # 两次之间的日期没有占用。
        self.assertEqual(
            self.list_available("2026-10-08T09:30", "2026-10-08T10:30"),
            [{"resource_id": resource_id, "name": "一号会议室"}],
        )

    def test_cross_midnight_window_uses_full_datetimes(self):
        """跨午夜区间按完整日期时间判断。"""
        resource_id = self.add_resource("一号会议室")
        self.reserve(resource_id, "2026-10-05T23:30", "2026-10-06T00:30")

        # 查询窗口跨午夜，与预约相交。
        self.assertEqual(
            self.list_available("2026-10-05T23:00", "2026-10-06T01:00"),
            [],
        )
        # 仅日期相同但时段不相交的窗口不受阻挡。
        self.assertEqual(
            self.list_available("2026-10-06T09:00", "2026-10-06T10:00"),
            [{"resource_id": resource_id, "name": "一号会议室"}],
        )

    def test_past_dates_are_allowed(self):
        """允许过去日期的查询窗口。"""
        resource_id = self.add_resource("一号会议室")

        self.assertEqual(
            self.list_available("2020-01-01T09:00", "2020-01-01T10:00"),
            [{"resource_id": resource_id, "name": "一号会议室"}],
        )

    def test_same_name_resources_are_independent(self):
        """同名资源各自独立判断、分别返回。"""
        first_id = self.add_resource("会议室")
        second_id = self.add_resource("会议室")
        self.reserve(first_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")

        self.assertEqual(
            self.list_available(f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00"),
            [{"resource_id": second_id, "name": "会议室"}],
        )

    def test_empty_catalog_returns_empty_list(self):
        """目录为空时时段查询成功返回空数组。"""
        self.assertEqual(
            self.list_available(f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00"),
            [],
        )

    def test_window_on_missing_database_returns_empty_list(self):
        """数据库尚不存在时沿用初始化行为，返回空数组并成功退出。"""
        self.assertFalse(self.db_path.exists())

        self.assertEqual(
            self.list_available(f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00"),
            [],
        )
        # 合法查询沿用了初始化行为，数据库文件已被创建。
        self.assertTrue(self.db_path.exists())
        # 空目录查询不消耗标识：首次登记的资源标识仍为 1。
        self.assertEqual(self.add_resource("一号会议室"), 1)

    # ---- 成功路径：与 --contains 同用 ----

    def test_combined_with_contains_requires_both_conditions(self):
        """与 --contains 同用时，名称和时段条件同时满足才返回。"""
        busy_match = self.add_resource("一号会议室")
        free_match = self.add_resource("二号会议室")
        free_other = self.add_resource("投影仪")
        self.reserve(busy_match, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")

        # 名称命中但时段被占用的不返回；时段空闲但名称不命中的也不返回。
        self.assertEqual(
            self.list_available(
                f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00",
                "--contains", "会议室",
            ),
            [{"resource_id": free_match, "name": "二号会议室"}],
        )
        # 两个条件都满足多项时按标识升序全部返回。
        self.assertEqual(
            self.list_available(
                f"{QUERY_DATE}T11:00", f"{QUERY_DATE}T12:00",
                "--contains", "会议室",
            ),
            [
                {"resource_id": busy_match, "name": "一号会议室"},
                {"resource_id": free_match, "name": "二号会议室"},
            ],
        )
        # 名称未命中时即使时段空闲也返回空数组。
        self.assertEqual(
            self.list_available(
                f"{QUERY_DATE}T11:00", f"{QUERY_DATE}T12:00",
                "--contains", "不存在",
            ),
            [],
        )
        self.assertEqual(free_other, 3)

    # ---- 只读语义 ----

    def test_window_query_is_read_only(self):
        """时段查询不新增、修改或删除记录，不消耗资源与预约标识。"""
        resource_id = self.add_resource("一号会议室")
        booking = self.reserve(
            resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00"
        )

        before = self.list_resources()
        self.list_available(f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")
        self.list_available(f"{QUERY_DATE}T10:00", f"{QUERY_DATE}T11:00")

        self.assertEqual(self.list_resources(), before)
        # 查询未消耗标识：下一个资源与下一条预约都紧接原有最大标识。
        self.assertEqual(self.add_resource("投影仪"), resource_id + 1)
        payload = self.reserve(
            resource_id, f"{QUERY_DATE}T10:00", f"{QUERY_DATE}T11:00"
        )
        self.assertEqual(payload["booking_id"], booking["booking_id"] + 1)

    def test_repeated_queries_and_reopen_are_identical(self):
        """重复时段查询结果一致；独立进程重开同一数据库后结果相同。"""
        resource_id = self.add_resource("一号会议室")
        self.reserve(resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")

        first = self.list_available(f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")
        second = self.list_available(f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")
        # 每条命令都是独立进程，本次查询即“重新打开同一数据库”后的读取。
        third = self.list_available(f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")

        self.assertEqual(first, second)
        self.assertEqual(second, third)

    # ---- 失败路径 ----

    def test_only_one_endpoint_returns_invalid_input(self):
        """只提供 --start 或只提供 --end：invalid_input。"""
        self.run_error(
            "invalid_input", "resource-list", "--start", f"{QUERY_DATE}T09:00"
        )
        self.run_error(
            "invalid_input", "resource-list", "--end", f"{QUERY_DATE}T10:00"
        )

    def test_missing_value_returns_invalid_input(self):
        """--start/--end 缺值：invalid_input。"""
        self.run_error("invalid_input", "resource-list", "--start")
        self.run_error(
            "invalid_input",
            "resource-list", "--start", f"{QUERY_DATE}T09:00", "--end",
        )

    def test_invalid_date_returns_invalid_input(self):
        """非法日期（如 2 月 30 日）：invalid_input。"""
        self.run_error(
            "invalid_input",
            "resource-list", "--start", "2026-02-30T09:00",
            "--end", f"{QUERY_DATE}T10:00",
        )

    def test_bad_format_returns_invalid_input(self):
        """格式不符（秒、时区后缀、非零填充、日期分隔符错误）：invalid_input。"""
        bad_values = [
            "2026-10-05T09:00:00",  # 带秒
            "2026-10-05T09:00+08:00",  # 时区后缀
            "2026-10-05T09:00Z",  # Z 后缀
            "2026-10-5T09:00",  # 未零填充
            "2026/10/05T09:00",  # 错误的日期分隔符
            "2026-10-05 09:00",  # 空格代替 T
            "2026-10-05T9:00",  # 小时未零填充
        ]
        for bad in bad_values:
            with self.subTest(bad=bad):
                self.run_error(
                    "invalid_input",
                    "resource-list", "--start", bad,
                    "--end", f"{QUERY_DATE}T10:00",
                )

    def test_non_ascii_digits_return_invalid_input(self):
        """非 ASCII 数字（全角、阿拉伯文）及其混写：invalid_input。"""
        self.run_error(
            "invalid_input",
            "resource-list", "--start", "２０２６-10-05T09:00",
            "--end", f"{QUERY_DATE}T10:00",
        )
        self.run_error(
            "invalid_input",
            "resource-list", "--start", "2026-10-05T09:0０",
            "--end", f"{QUERY_DATE}T10:00",
        )

    def test_surrounding_whitespace_returns_invalid_input(self):
        """首尾空白不自动去除：invalid_input。"""
        self.run_error(
            "invalid_input",
            "resource-list", "--start", f"  {QUERY_DATE}T09:00",
            "--end", f"{QUERY_DATE}T10:00",
        )
        self.run_error(
            "invalid_input",
            "resource-list", "--start", f"{QUERY_DATE}T09:00",
            "--end", f"{QUERY_DATE}T10:00\t",
        )

    def test_start_must_be_strictly_before_end(self):
        """开始不早于结束（相等或颠倒）：invalid_input。"""
        self.run_error(
            "invalid_input",
            "resource-list", "--start", f"{QUERY_DATE}T10:00",
            "--end", f"{QUERY_DATE}T10:00",
        )
        self.run_error(
            "invalid_input",
            "resource-list", "--start", f"{QUERY_DATE}T11:00",
            "--end", f"{QUERY_DATE}T10:00",
        )

    def test_invalid_window_does_not_create_database_file(self):
        """非法时段参数指向尚不存在的数据库路径时，不得创建文件。"""
        missing_db = Path(self._tmpdir.name) / "should-not-exist.sqlite"
        self.run_error(
            "invalid_input",
            "resource-list", "--start", f"{QUERY_DATE}T09:00",
            db=missing_db,
        )
        self.assertFalse(missing_db.exists())
        self.run_error(
            "invalid_input",
            "resource-list", "--start", f"{QUERY_DATE}T11:00",
            "--end", f"{QUERY_DATE}T10:00",
            db=missing_db,
        )
        self.assertFalse(missing_db.exists())

    def test_invalid_window_does_not_change_existing_database(self):
        """已有数据库上非法时段参数失败后，资源目录与预约查询结果不变。"""
        resource_id = self.add_resource("一号会议室")
        self.reserve(resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")
        before = self.list_resources()

        self.run_error(
            "invalid_input",
            "resource-list", "--start", f"{QUERY_DATE}T09:00",
            "--end", f"{QUERY_DATE}T09:00",
        )

        self.assertEqual(self.list_resources(), before)
        # 预约记录同样未被改动：该时段仍被占用。
        self.assertEqual(
            self.list_available(f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00"),
            [],
        )


if __name__ == "__main__":
    unittest.main()
