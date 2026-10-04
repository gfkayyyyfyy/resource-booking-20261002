"""resource-add 资源登记的回归测试（仅标准库，python -m unittest 可发现）。

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
BOOKING_DATE = "2026-10-05"

# 去除首尾空白后为空的全部非法名称：空字符串、只含空格/制表符/CR/LF
# 以及这些字符的混合。每种输入都应得到完全相同的失败结果。
INVALID_NAMES = [
    "",
    " ",
    "   ",
    "\t",
    "\t\t",
    "\r",
    "\n",
    "\r\n",
    " \t\r\n ",
    "\n\t \r",
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

    def run_error(self, expected_error, *args, db=None):
        """运行命令并断言退出码 2、stderr 为空、输出为指定的错误对象。"""
        proc = self.run_cli(*args, db=db)
        self.assertEqual(proc.returncode, 2, msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        self.assertEqual(proc.stderr, "", msg=f"stderr={proc.stderr!r}")
        self.assertEqual(json.loads(proc.stdout), {"error": expected_error})
        return proc

    def add_resource(self, name, db=None):
        """登记资源并断言成功输出仅含 resource_id 与 name，返回 resource_id。"""
        payload = self.run_ok("resource-add", "--name", name, db=db)
        self.assertEqual(
            set(payload.keys()),
            {"resource_id", "name"},
            msg=f"成功输出应仅含 resource_id 与 name，实际为 {payload!r}",
        )
        self.assertIsInstance(payload["resource_id"], int)
        self.assertGreater(payload["resource_id"], 0)
        return payload

    def list_resources(self, db=None):
        """运行 resource-list，返回 resources 数组。"""
        payload = self.run_ok("resource-list", db=db)
        return payload["resources"]

    def day_query(self, resource_id, date=BOOKING_DATE, db=None):
        """运行 day-query，返回解析后的完整结果对象。"""
        return self.run_ok(
            "day-query", "--resource", str(resource_id), "--date", date, db=db
        )

    def assert_invalid_name(self, name, db=None):
        """断言指定名称被拒绝：invalid_input、退出码 2、stderr 为空。"""
        proc = self.run_error("invalid_input", "resource-add", "--name", name, db=db)
        # 标准输出只有这一份 JSON 对象（json.loads 已拒绝任何额外内容），
        # 且 stderr 为空意味着没有异常堆栈。
        self.assertEqual(
            proc.stdout.strip(),
            '{"error": "invalid_input"}',
            msg=f"输入 {name!r} 应只输出一份 invalid_input 对象，实际为 {proc.stdout!r}",
        )

    # ---- 成功路径（对照组） ----

    def test_valid_name_strips_surrounding_whitespace_keeps_inner_spaces(self):
        """首尾空格/制表符/换行被去除，名称内部连续空格原样保留。"""
        payload = self.add_resource("\t 北区  会议室 \n ")

        self.assertEqual(payload["name"], "北区  会议室")
        resource_id = payload["resource_id"]

        # 另一次独立调用中通过 resource-list 读到同一名称与标识。
        resources = self.list_resources()
        self.assertEqual(
            resources,
            [{"resource_id": resource_id, "name": "北区  会议室"}],
        )

    def test_same_name_resources_are_registered_separately(self):
        """同名资源允许分别登记，各自获得独立标识。"""
        first = self.add_resource("一号会议室")
        second = self.add_resource("一号会议室")

        self.assertNotEqual(first["resource_id"], second["resource_id"])
        self.assertEqual(
            self.list_resources(),
            [
                {"resource_id": first["resource_id"], "name": "一号会议室"},
                {"resource_id": second["resource_id"], "name": "一号会议室"},
            ],
        )

    # ---- 失败路径：输出协议 ----

    def test_whitespace_only_names_are_rejected(self):
        """空字符串与只含空白字符的名称一律 invalid_input、退出码 2、stderr 为空。"""
        for name in INVALID_NAMES:
            with self.subTest(name=name):
                self.assert_invalid_name(name)

    def test_missing_name_argument_is_rejected(self):
        """省略 --name：invalid_input、退出码 2、stderr 为空（无异常堆栈）。"""
        proc = self.run_error("invalid_input", "resource-add")
        self.assertEqual(
            proc.stdout.strip(),
            '{"error": "invalid_input"}',
            msg=f"省略 --name 应只输出一份 invalid_input 对象，实际为 {proc.stdout!r}",
        )

    def test_name_argument_without_value_is_rejected(self):
        """提供 --name 而没有值：invalid_input、退出码 2、stderr 为空（无异常堆栈）。"""
        proc = self.run_error("invalid_input", "resource-add", "--name")
        self.assertEqual(
            proc.stdout.strip(),
            '{"error": "invalid_input"}',
            msg=f"--name 缺值应只输出一份 invalid_input 对象，实际为 {proc.stdout!r}",
        )

    # ---- 失败路径：不创建数据库 ----

    def test_invalid_names_do_not_create_database_file(self):
        """每个非法登记请求结束后数据库文件仍不存在；随后有效登记建库并得标识 1。"""
        self.assertFalse(self.db_path.exists())

        for name in INVALID_NAMES:
            with self.subTest(name=name):
                self.assert_invalid_name(name)
                self.assertFalse(
                    self.db_path.exists(),
                    msg=f"输入 {name!r} 被拒绝后不应创建数据库文件 {self.db_path}",
                )
        # 缺参与缺值同样不得创建文件。
        self.run_error("invalid_input", "resource-add")
        self.assertFalse(self.db_path.exists())
        self.run_error("invalid_input", "resource-add", "--name")
        self.assertFalse(self.db_path.exists())

        # 随后首个有效登记创建数据库并获得 resource_id 为 1。
        payload = self.add_resource("一号会议室")
        self.assertTrue(self.db_path.exists())
        self.assertEqual(payload["resource_id"], 1)

    # ---- 失败路径：不改变已有数据 ----

    def test_invalid_names_do_not_change_existing_database(self):
        """已有库上非法登记失败前后目录与按日查询一致，且不消耗资源标识。"""
        first = self.add_resource("一号会议室")
        resource_id = first["resource_id"]
        self.run_ok(
            "reserve",
            "--resource", str(resource_id),
            "--start", f"{BOOKING_DATE}T09:00",
            "--end", f"{BOOKING_DATE}T10:00",
        )
        list_before = self.list_resources()
        day_before = self.day_query(resource_id)

        for name in INVALID_NAMES:
            with self.subTest(name=name):
                self.assert_invalid_name(name)

        # 失败前后 resource-list 与该资源的 day-query 解析结果完全相同。
        self.assertEqual(self.list_resources(), list_before)
        self.assertEqual(self.day_query(resource_id), day_before)

        # 失败没有消耗资源标识：有效登记获得紧接原最大标识的新标识。
        second = self.add_resource("二号会议室")
        self.assertEqual(second["resource_id"], resource_id + 1)


if __name__ == "__main__":
    unittest.main()
