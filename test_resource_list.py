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

# 固定的预约日期（固定 UTC+08:00 的本地日期，与运行环境无关）。
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
        self.assertEqual(proc.stderr, "")
        payload = json.loads(proc.stdout)  # 若输出不是单个 JSON 文档会抛错
        self.assertIsInstance(payload, dict)
        return payload

    def run_error(self, expected_error, *args, db=None):
        """运行命令并断言退出码 2、stderr 为空、输出为指定的错误对象。"""
        proc = self.run_cli(*args, db=db)
        self.assertEqual(proc.returncode, 2, msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        self.assertEqual(proc.stderr, "")
        self.assertEqual(json.loads(proc.stdout), {"error": expected_error})
        return proc

    def add_resource(self, name="测试资源"):
        payload = self.run_ok("resource-add", "--name", name)
        self.assertIsInstance(payload["resource_id"], int)
        self.assertGreater(payload["resource_id"], 0)
        return payload["resource_id"]

    def reserve(self, resource_id, start, end):
        payload = self.run_ok(
            "reserve", "--resource", str(resource_id), "--start", start, "--end", end
        )
        return payload["booking_id"]

    def cancel(self, booking_id):
        payload = self.run_ok("cancel", "--booking", str(booking_id))
        self.assertEqual(payload, {"booking_id": booking_id, "cancelled": True})

    def day_query(self, resource_id, date=QUERY_DATE):
        return self.run_ok("day-query", "--resource", str(resource_id), "--date", date)

    def resource_list(self):
        """运行 resource-list 并固定输出形状：顶层只有 resources，
        数组每项仅含 resource_id（int）与 name（str）。"""
        payload = self.run_ok("resource-list")
        self.assertEqual(set(payload), {"resources"})
        self.assertIsInstance(payload["resources"], list)
        for item in payload["resources"]:
            self.assertEqual(set(item), {"resource_id", "name"})
            self.assertIsInstance(item["resource_id"], int)
            self.assertIsInstance(item["name"], str)
        return payload

    # ---- 成功路径 ----

    def test_missing_database_is_initialized_and_returns_empty_list(self):
        """路径尚不存在时创建数据库并返回空目录；随后首个资源标识为 1。"""
        self.assertFalse(self.db_path.exists())

        payload = self.resource_list()

        self.assertEqual(payload, {"resources": []})
        self.assertTrue(self.db_path.exists())
        # 初始化行为与其他命令一致：首次登记资源获得标识 1。
        first_id = self.add_resource("一号会议室")
        self.assertEqual(first_id, 1)
        self.assertEqual(
            self.resource_list(),
            {"resources": [{"resource_id": 1, "name": "一号会议室"}]},
        )

    def test_names_trimmed_and_duplicates_kept_in_id_order(self):
        """登记名去除首尾空白；同名资源不合并、各保留标识；名称中间的空格原样保留。"""
        first_id = self.add_resource("  二号会议室  ")
        second_id = self.add_resource("一号 会议室")
        third_id = self.add_resource("二号会议室")

        payload = self.resource_list()

        # 按标识数值升序（即登记顺序），不按名称排序、不合并同名项。
        self.assertEqual(
            payload,
            {
                "resources": [
                    {"resource_id": first_id, "name": "二号会议室"},
                    {"resource_id": second_id, "name": "一号 会议室"},
                    {"resource_id": third_id, "name": "二号会议室"},
                ]
            },
        )
        # 两个“二号会议室”均显示去除首尾空白后的名称，且标识不同。
        duplicates = [
            item for item in payload["resources"] if item["name"] == "二号会议室"
        ]
        self.assertEqual(len(duplicates), 2)
        self.assertNotEqual(duplicates[0]["resource_id"], duplicates[1]["resource_id"])

    def test_order_is_numeric_not_textual_across_9_and_10(self):
        """标识跨过 9 到 10 时仍按数值升序：10 排在 9 之后，而非文本序的 2 之前。"""
        ids = [self.add_resource(f"资源{i}") for i in range(1, 11)]

        payload = self.resource_list()

        listed_ids = [item["resource_id"] for item in payload["resources"]]
        # 文本排序会得到 [1, 10, 2, 3, ...]，这里固定为数值升序。
        self.assertEqual(listed_ids, sorted(ids))
        self.assertEqual(listed_ids, list(range(1, 11)))
        self.assertEqual(
            [item["name"] for item in payload["resources"]],
            [f"资源{i}" for i in range(1, 11)],
        )

    def test_repeated_queries_and_reopened_database_are_identical(self):
        """重复查询结果一致；每条命令都是独立进程，即重开同一数据库后解析结果相同。"""
        self.add_resource("  二号会议室  ")
        self.add_resource("一号 会议室")

        first = self.resource_list()
        second = self.resource_list()
        third = self.resource_list()

        self.assertEqual(first, second)
        self.assertEqual(second, third)

    # ---- 只读语义 ----

    def test_query_does_not_consume_ids_or_change_records(self):
        """连续查询不消耗资源/预约标识、不改变业务记录：查询前后 day-query 一致，
        之后新增资源与非重叠时段的新预约各自紧接原有最大标识。"""
        resource_id = self.add_resource("一号会议室")  # 资源标识 1
        other_id = self.add_resource("二号会议室")  # 资源标识 2
        booking_id = self.reserve(
            resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00"
        )  # 预约标识 1

        day_before = self.day_query(resource_id)
        list_before = self.resource_list()
        self.resource_list()
        self.resource_list()
        day_after = self.day_query(resource_id)

        # 查询前后按日查询结果相同（目录查询不改变业务记录）。
        self.assertEqual(day_before, day_after)
        # 连续查询后目录内容不变。
        self.assertEqual(self.resource_list(), list_before)
        # 新增资源紧接原有最大资源标识。
        new_resource_id = self.add_resource("三号会议室")
        self.assertEqual(new_resource_id, max(resource_id, other_id) + 1)
        # 非重叠时段的新预约紧接原有最大预约标识。
        new_booking_id = self.reserve(
            resource_id, f"{QUERY_DATE}T10:00", f"{QUERY_DATE}T11:00"
        )
        self.assertEqual(new_booking_id, booking_id + 1)

    def test_cancel_does_not_change_resource_list(self):
        """取消预约前后资源目录相同。"""
        resource_id = self.add_resource("一号会议室")
        booking_id = self.reserve(resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")

        list_before = self.resource_list()
        self.cancel(booking_id)
        list_after = self.resource_list()

        self.assertEqual(list_before, list_after)

    # ---- 失败路径 ----

    def test_unsupported_argument_returns_invalid_input(self):
        """resource-list 携带不支持的 --name 参数：invalid_input、退出码 2、stderr 为空。"""
        self.run_error("invalid_input", "resource-list", "--name", "一号会议室")

    def test_unsupported_argument_does_not_create_missing_database(self):
        """非法输入指向尚不存在的数据库路径时，不得创建文件。"""
        missing_db = Path(self._tmpdir.name) / "should-not-exist.sqlite"
        self.run_error(
            "invalid_input", "resource-list", "--name", "一号会议室", db=missing_db
        )
        self.assertFalse(missing_db.exists())

    def test_unsupported_argument_leaves_existing_database_unchanged(self):
        """已有数据库上的非法调用失败后，资源目录与按日查询结果均不变。"""
        resource_id = self.add_resource("一号会议室")
        self.reserve(resource_id, f"{QUERY_DATE}T09:00", f"{QUERY_DATE}T10:00")
        list_before = self.resource_list()
        day_before = self.day_query(resource_id)

        self.run_error("invalid_input", "resource-list", "--name", "一号会议室")

        self.assertEqual(self.resource_list(), list_before)
        self.assertEqual(self.day_query(resource_id), day_before)


if __name__ == "__main__":
    unittest.main()
