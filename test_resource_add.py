"""resource-add 资源登记的回归测试（仅标准库，python -m unittest 可发现）。

通过公开命令行入口 `python -m booking --db <文件> <命令>` 准备数据并断言，
每个用例使用独立的临时 SQLite 数据库，结束后自动清理；
不依赖已有数据库、当前日期、机器时区或任何第三方库。
断言均基于解析后的 JSON 内容，不依赖输出对象的键顺序；
失败信息指出具体输入与预期结果。
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
BOOKING_DATE = "2026-10-05"

# 去除首尾空白后为空、应被拒绝的名称：空字符串、单一空白字符及其混合。
BLANK_NAMES = [
    "",
    " ",
    "   ",
    "\t",
    "\r",
    "\n",
    " \t\r\n",
    "\t \n \r ",
    " \t \t ",
]


class ResourceAddTestCase(unittest.TestCase):
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

    def run_invalid_input(self, *args, db=None):
        """运行命令并断言非法输入协议：退出码 2、stderr 为空（无异常堆栈）、
        标准输出只有 {"error": "invalid_input"} 这一个 JSON 对象。"""
        proc = self.run_cli(*args, db=db)
        self.assertEqual(
            proc.returncode, 2,
            msg=f"args={args!r}: 预期退出码 2，stdout={proc.stdout!r} stderr={proc.stderr!r}",
        )
        self.assertEqual(
            proc.stderr, "",
            msg=f"args={args!r}: 预期 stderr 为空（不得输出异常堆栈），实际 stderr={proc.stderr!r}",
        )
        # 标准输出只有一行、即这一个 JSON 对象。
        self.assertEqual(
            proc.stdout.strip(),
            json.dumps({"error": "invalid_input"}),
            msg=f"args={args!r}: 预期标准输出只有 invalid_input 对象，实际 stdout={proc.stdout!r}",
        )
        self.assertEqual(
            json.loads(proc.stdout), {"error": "invalid_input"},
            msg=f"args={args!r}: 预期解析结果为 invalid_input，实际 stdout={proc.stdout!r}",
        )
        return proc

    def add_resource(self, name, db=None):
        """登记资源并断言成功输出仅含 resource_id 与 name，返回 resource_id。"""
        payload = self.run_ok("resource-add", "--name", name, db=db)
        self.assertEqual(
            set(payload.keys()), {"resource_id", "name"},
            msg=f"name={name!r}: 成功输出应仅含 resource_id 与 name，实际 {payload!r}",
        )
        self.assertIsInstance(payload["resource_id"], int)
        self.assertGreater(payload["resource_id"], 0)
        return payload

    def list_resources(self):
        """运行 resource-list，返回 resources 数组。"""
        payload = self.run_ok("resource-list")
        self.assertEqual(set(payload.keys()), {"resources"})
        return payload["resources"]

    def day_query(self, resource_id, date=BOOKING_DATE):
        """运行 day-query，返回解析后的完整结果对象。"""
        return self.run_ok(
            "day-query", "--resource", str(resource_id), "--date", date
        )

    # ---- 成功路径：首尾空白去除、内部空白保留 ----

    def test_surrounding_whitespace_stripped_and_inner_spaces_kept(self):
        """名称前后的空格、制表符、换行被去除；名称内部连续空格原样保留。"""
        payload = self.add_resource(" \t\n\r北区  会议室\r\n\t ")

        self.assertEqual(
            payload["name"], "北区  会议室",
            msg=f"预期去除首尾空白并保留内部连续空格，实际 {payload!r}",
        )
        resource_id = payload["resource_id"]

        # 另一次独立调用中通过 resource-list 读到同一名称与标识。
        resources = self.list_resources()
        self.assertEqual(
            resources,
            [{"resource_id": resource_id, "name": "北区  会议室"}],
            msg=f"resource-list 应读到同一名称与标识，实际 {resources!r}",
        )

    # ---- 失败路径：非法名称的拒绝协议 ----

    def test_blank_names_rejected(self):
        """空字符串与仅由空格/制表符/CR/LF 组成的名称一律 invalid_input。"""
        for name in BLANK_NAMES:
            with self.subTest(name=name):
                self.run_invalid_input("resource-add", "--name", name)

    def test_missing_name_argument_rejected(self):
        """省略 --name：invalid_input、退出码 2、stderr 为空（无异常堆栈）。"""
        self.run_invalid_input("resource-add")

    def test_name_argument_without_value_rejected(self):
        """提供 --name 而没有值：invalid_input、退出码 2、stderr 为空。"""
        self.run_invalid_input("resource-add", "--name")

    # ---- 失败路径：不创建数据库文件 ----

    def test_invalid_name_does_not_create_database_file(self):
        """对尚不存在的数据库路径，每个非法登记请求结束后文件仍不存在；
        随后的有效登记才创建数据库并获得 resource_id 为 1。"""
        self.assertFalse(self.db_path.exists())

        for name in BLANK_NAMES:
            with self.subTest(name=name):
                self.run_invalid_input("resource-add", "--name", name)
                self.assertFalse(
                    self.db_path.exists(),
                    msg=f"name={name!r}: 非法登记不得创建数据库文件 {self.db_path}",
                )
        # 缺参与缺值形式同样不得创建文件。
        self.run_invalid_input("resource-add")
        self.assertFalse(self.db_path.exists())
        self.run_invalid_input("resource-add", "--name")
        self.assertFalse(self.db_path.exists())

        # 首个有效登记创建数据库，resource_id 从 1 开始（失败未消耗标识）。
        payload = self.add_resource("北区  会议室")
        self.assertEqual(
            payload["resource_id"], 1,
            msg=f"失败请求不得消耗标识，首个有效登记预期 resource_id=1，实际 {payload!r}",
        )
        self.assertTrue(self.db_path.exists())

    # ---- 失败路径：不改变已有数据、不消耗标识 ----

    def test_invalid_name_does_not_change_existing_database(self):
        """已有数据库上非法名称失败前后，resource-list 与 day-query 解析结果
        完全相同；之后有效登记获得紧接原最大标识的新标识。"""
        first = self.add_resource("一号会议室")
        resource_id = first["resource_id"]
        self.run_ok(
            "reserve", "--resource", str(resource_id),
            "--start", f"{BOOKING_DATE}T09:00", "--end", f"{BOOKING_DATE}T10:00",
        )
        list_before = self.list_resources()
        day_before = self.day_query(resource_id)

        for name in BLANK_NAMES:
            with self.subTest(name=name):
                self.run_invalid_input("resource-add", "--name", name)
                self.assertEqual(
                    self.list_resources(), list_before,
                    msg=f"name={name!r}: 失败前后 resource-list 应完全相同",
                )
                self.assertEqual(
                    self.day_query(resource_id), day_before,
                    msg=f"name={name!r}: 失败前后 day-query 应完全相同",
                )
        # 缺参与缺值形式同样不得改变已有数据。
        self.run_invalid_input("resource-add")
        self.run_invalid_input("resource-add", "--name")
        self.assertEqual(self.list_resources(), list_before)
        self.assertEqual(self.day_query(resource_id), day_before)

        # 失败没有消耗资源标识：有效登记获得紧接原最大标识的新标识。
        second = self.add_resource("二号会议室")
        self.assertEqual(
            second["resource_id"], resource_id + 1,
            msg=f"失败请求不得消耗标识，预期新标识 {resource_id + 1}，实际 {second!r}",
        )


if __name__ == "__main__":
    unittest.main()
