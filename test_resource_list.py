"""resource-list 资源目录查询的回归测试（仅标准库，python -m unittest 可发现）。

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


class ResourceListTestCase(unittest.TestCase):
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

    def add_resource(self, name="测试资源"):
        payload = self.run_ok("resource-add", "--name", name)
        self.assertIsInstance(payload["resource_id"], int)
        self.assertGreater(payload["resource_id"], 0)
        self.assertEqual(payload["name"], name.strip())
        return payload["resource_id"]

    def reserve(self, resource_id, start, end):
        payload = self.run_ok(
            "reserve", "--resource", str(resource_id), "--start", start, "--end", end
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

    def cancel(self, booking_id):
        payload = self.run_ok("cancel", "--booking", str(booking_id))
        self.assertEqual(payload, {"booking_id": booking_id, "cancelled": True})

    def day_query(self, resource_id, date=QUERY_DATE):
        return self.run_ok("day-query", "--resource", str(resource_id), "--date", date)

    def list_resources(self, db=None):
        """运行 resource-list 并断言顶层结构，返回 resources 数组。"""
        payload = self.run_ok("resource-list", db=db)
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

    def test_missing_path_initializes_database_and_returns_empty_list(self):
        """路径尚不存在时创建数据库并返回空目录；随后首个资源标识仍为 1。"""
        self.assertFalse(self.db_path.exists())

        resources = self.list_resources()

        self.assertEqual(resources, [])
        # 查询沿用了初始化行为，数据库文件已被创建。
        self.assertTrue(self.db_path.exists())
        # 空目录查询不消耗标识：首次登记的资源标识仍为 1。
        self.assertEqual(self.add_resource("一号会议室"), 1)

    def test_sorted_by_numeric_id_not_name_and_same_names_not_merged(self):
        """按标识数值升序，不按名称排序、不合并同名项；名称按保存结果原样返回。"""
        first_id = self.add_resource("  二号会议室  ")  # 保存时去除首尾空白
        second_id = self.add_resource("一号 会议室")  # 名称中间的空格原样保留
        third_id = self.add_resource("二号会议室")  # 与第一条同名，独立标识

        resources = self.list_resources()

        self.assertEqual(
            resources,
            [
                {"resource_id": first_id, "name": "二号会议室"},
                {"resource_id": second_id, "name": "一号 会议室"},
                {"resource_id": third_id, "name": "二号会议室"},
            ],
        )
        # 两个同名“二号会议室”各自保留不同标识，未被合并。
        self.assertNotEqual(first_id, third_id)
        self.assertEqual(
            [item["resource_id"] for item in resources],
            [first_id, second_id, third_id],
        )

    def test_ordering_is_numeric_across_9_and_10_not_textual(self):
        """标识跨过 9 到 10 时按数值升序（1..10），而非文本顺序（1,10,2,...）。"""
        expected_ids = [self.add_resource(f"会议室{i:02d}") for i in range(1, 11)]

        resources = self.list_resources()

        self.assertEqual([item["resource_id"] for item in resources], expected_ids)
        self.assertEqual([item["resource_id"] for item in resources], list(range(1, 11)))
        # 文本排序会把 10 排在 2 之前；此处明确结果不是文本顺序。
        self.assertNotEqual(
            [str(item["resource_id"]) for item in resources],
            sorted(str(i) for i in range(1, 11)),
        )

    def test_repeated_queries_and_reopen_are_identical(self):
        """重复查询结果一致；独立进程重开同一数据库后解析结果相同。"""
        self.add_resource("  二号会议室  ")
        self.add_resource("一号 会议室")
        self.add_resource("二号会议室")

        first = self.list_resources()
        second = self.list_resources()
        # 每条命令都是独立进程，本次查询即“重新打开同一数据库”后的读取。
        third = self.list_resources()

        self.assertEqual(first, second)
        self.assertEqual(second, third)

    # ---- 只读语义 ----

    def test_queries_do_not_consume_ids_or_change_records(self):
        """查询不消耗资源与预约标识，不改变业务记录；取消预约不影响目录。"""
        resource_id = self.add_resource("一号会议室")
        booking_id = self.reserve(resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")

        day_before = self.day_query(resource_id)
        list_before = self.list_resources()
        # 在已有预约的数据库上连续查询。
        self.list_resources()
        self.list_resources()

        # 查询前后 day-query 的结果相同，资源目录也不变。
        self.assertEqual(self.day_query(resource_id), day_before)
        self.assertEqual(self.list_resources(), list_before)
        # 新增资源获得紧接原有最大标识的标识。
        next_resource_id = self.add_resource("二号会议室")
        self.assertEqual(next_resource_id, resource_id + 1)
        # 非重叠时段的新预约同样获得紧接原有最大标识的标识。
        next_booking_id = self.reserve(
            resource_id, f"{QUERY_DATE}T10:00", f"{QUERY_DATE}T11:00"
        )
        self.assertEqual(next_booking_id, booking_id + 1)
        # 取消预约前后资源目录相同。
        list_with_two = list_before + [
            {"resource_id": next_resource_id, "name": "二号会议室"},
        ]
        self.assertEqual(self.list_resources(), list_with_two)
        self.cancel(booking_id)
        self.assertEqual(self.list_resources(), list_with_two)

    # ---- 失败路径 ----

    def test_unsupported_name_argument_returns_invalid_input(self):
        """resource-list 携带不支持的 --name 参数：invalid_input、退出码 2、stderr 为空。"""
        self.run_error("invalid_input", "resource-list", "--name", "一号会议室")

    def test_invalid_input_does_not_create_database_file(self):
        """非法参数指向尚不存在的数据库路径时，不得创建文件。"""
        missing_db = Path(self._tmpdir.name) / "should-not-exist.sqlite"
        self.run_error(
            "invalid_input",
            "resource-list", "--name", "一号会议室",
            db=missing_db,
        )
        self.assertFalse(missing_db.exists())

    def test_invalid_input_does_not_change_existing_database(self):
        """已有数据库上非法参数失败后，资源目录与预约查询结果不变。"""
        resource_id = self.add_resource("一号会议室")
        self.reserve(resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")
        list_before = self.list_resources()
        day_before = self.day_query(resource_id)

        self.run_error("invalid_input", "resource-list", "--name", "二号会议室")

        self.assertEqual(self.list_resources(), list_before)
        self.assertEqual(self.day_query(resource_id), day_before)


if __name__ == "__main__":
    unittest.main()
