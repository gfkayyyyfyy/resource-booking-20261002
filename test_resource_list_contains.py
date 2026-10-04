"""resource-list --contains 名称片段筛选的回归测试（仅标准库，python -m unittest 可发现）。

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


class ResourceListContainsTestCase(unittest.TestCase):
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

    def list_resources(self, *extra_args, db=None):
        """运行 resource-list（可带 --contains）并断言结构，返回 resources 数组。"""
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

    def test_contains_filters_by_name_fragment(self):
        """--contains 只返回名称包含该片段的资源，按标识数值升序。"""
        first_id = self.add_resource("一号会议室")
        second_id = self.add_resource("投影仪")
        third_id = self.add_resource("一号会议室")

        resources = self.list_resources("--contains", "会议室")

        self.assertEqual(
            resources,
            [
                {"resource_id": first_id, "name": "一号会议室"},
                {"resource_id": third_id, "name": "一号会议室"},
            ],
        )
        self.assertEqual([first_id, second_id, third_id], [1, 2, 3])

        # 另一个片段只命中“投影仪”。
        self.assertEqual(
            self.list_resources("--contains", "投影"),
            [{"resource_id": second_id, "name": "投影仪"}],
        )

    def test_contains_fragment_is_stripped_before_matching(self):
        """片段去除首尾空白后参与匹配，结果与未加空白时相同。"""
        self.add_resource("一号会议室")
        self.add_resource("投影仪")
        self.add_resource("一号会议室")

        plain = self.list_resources("--contains", "会议室")
        padded = self.list_resources("--contains", "  会议室  ")

        self.assertEqual(padded, plain)
        self.assertEqual([item["resource_id"] for item in padded], [1, 3])

    def test_contains_no_match_returns_empty_list(self):
        """没有命中时返回空数组并成功退出。"""
        self.add_resource("一号会议室")

        self.assertEqual(self.list_resources("--contains", "不存在"), [])

    def test_contains_omitted_keeps_full_listing(self):
        """省略 --contains 时保留完整目录查询行为。"""
        self.add_resource("一号会议室")
        self.add_resource("投影仪")

        resources = self.list_resources()

        self.assertEqual(
            resources,
            [
                {"resource_id": 1, "name": "一号会议室"},
                {"resource_id": 2, "name": "投影仪"},
            ],
        )

    def test_contains_matches_saved_name_verbatim(self):
        """按保存后的名称匹配：登记时的首尾空白已去除，中间空格保留。"""
        self.add_resource("  一号 会议室  ")  # 保存为“一号 会议室”

        # 中间空格是保存名称的一部分，带空格的片段可以命中。
        self.assertEqual(
            self.list_resources("--contains", "一号 会议室"),
            [{"resource_id": 1, "name": "一号 会议室"}],
        )
        # 连续子串比较：跳过中间空格的片段不命中。
        self.assertEqual(self.list_resources("--contains", "一号会议室"), [])

    def test_contains_is_case_sensitive(self):
        """区分大小写：大小写不同的片段不命中。"""
        self.add_resource("Room A")

        self.assertEqual(
            self.list_resources("--contains", "Room"),
            [{"resource_id": 1, "name": "Room A"}],
        )
        self.assertEqual(self.list_resources("--contains", "room"), [])
        self.assertEqual(self.list_resources("--contains", "ROOM"), [])

    def test_contains_percent_and_underscore_are_literal(self):
        """百分号与下划线按普通字符匹配，不作为通配符。"""
        self.add_resource("50% 折扣券")
        self.add_resource("投影_仪")
        self.add_resource("投影仪")

        self.assertEqual(
            self.list_resources("--contains", "%"),
            [{"resource_id": 1, "name": "50% 折扣券"}],
        )
        self.assertEqual(
            self.list_resources("--contains", "_"),
            [{"resource_id": 2, "name": "投影_仪"}],
        )
        # “_”不是单字符通配符：不能命中“投影仪”。
        self.assertEqual(self.list_resources("--contains", "投影_"), [
            {"resource_id": 2, "name": "投影_仪"},
        ])

    def test_contains_matches_digits_and_punctuation_verbatim(self):
        """数字与标点按原字符匹配。"""
        self.add_resource("会议室3号（小）")

        self.assertEqual(
            self.list_resources("--contains", "3号（小）"),
            [{"resource_id": 1, "name": "会议室3号（小）"}],
        )
        self.assertEqual(self.list_resources("--contains", "3号(小)"), [])

    def test_contains_on_missing_database_returns_empty_list(self):
        """数据库尚不存在时沿用初始化行为，返回空目录并成功退出。"""
        self.assertFalse(self.db_path.exists())

        self.assertEqual(self.list_resources("--contains", "会议室"), [])
        # 合法查询沿用了初始化行为，数据库文件已被创建。
        self.assertTrue(self.db_path.exists())
        # 空目录查询不消耗标识：首次登记的资源标识仍为 1。
        self.assertEqual(self.add_resource("一号会议室"), 1)

    def test_contains_query_is_read_only(self):
        """筛选查询不新增、修改或删除记录，不消耗资源标识。"""
        self.add_resource("一号会议室")
        self.add_resource("投影仪")

        before = self.list_resources()
        self.list_resources("--contains", "会议室")
        self.list_resources("--contains", "不存在")

        self.assertEqual(self.list_resources(), before)
        # 查询未消耗标识：下一个资源获得紧接原有最大标识的标识。
        self.assertEqual(self.add_resource("白板"), 3)

    def test_contains_repeated_queries_and_reopen_are_identical(self):
        """重复筛选查询结果一致；独立进程重开同一数据库后结果相同。"""
        self.add_resource("一号会议室")
        self.add_resource("投影仪")
        self.add_resource("二号会议室")

        first = self.list_resources("--contains", "会议室")
        second = self.list_resources("--contains", "会议室")
        # 每条命令都是独立进程，本次查询即“重新打开同一数据库”后的读取。
        third = self.list_resources("--contains", "会议室")

        self.assertEqual(first, second)
        self.assertEqual(second, third)

    # ---- 失败路径 ----

    def test_contains_missing_value_returns_invalid_input(self):
        """--contains 缺少值：invalid_input、退出码 2、stderr 为空。"""
        self.run_error("invalid_input", "resource-list", "--contains")

    def test_contains_empty_text_returns_invalid_input(self):
        """--contains 传入空文本：invalid_input。"""
        self.run_error("invalid_input", "resource-list", "--contains", "")

    def test_contains_whitespace_only_returns_invalid_input(self):
        """--contains 仅含空白：invalid_input。"""
        self.run_error("invalid_input", "resource-list", "--contains", "   ")
        self.run_error("invalid_input", "resource-list", "--contains", " \t ")

    def test_invalid_contains_does_not_create_database_file(self):
        """非法 --contains 指向尚不存在的数据库路径时，不得创建文件。"""
        missing_db = Path(self._tmpdir.name) / "should-not-exist.sqlite"
        self.run_error(
            "invalid_input",
            "resource-list", "--contains", "  ",
            db=missing_db,
        )
        self.assertFalse(missing_db.exists())

    def test_invalid_contains_does_not_change_existing_database(self):
        """已有数据库上非法 --contains 失败后，资源目录不变。"""
        self.add_resource("一号会议室")
        before = self.list_resources()

        self.run_error("invalid_input", "resource-list", "--contains", "")

        self.assertEqual(self.list_resources(), before)


if __name__ == "__main__":
    unittest.main()
