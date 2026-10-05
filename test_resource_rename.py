"""resource-rename 单个资源改名的回归测试（仅标准库，python -m unittest 可发现）。

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


class ResourceRenameTestCase(unittest.TestCase):
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

    def rename(self, resource, name):
        """运行 resource-rename 并断言成功结构，返回 payload。"""
        payload = self.run_ok(
            "resource-rename", "--resource", resource, "--name", name
        )
        # 顶层只有 resource_id 与 name 两个键。
        self.assertEqual(set(payload.keys()), {"resource_id", "name"})
        self.assertIsInstance(payload["resource_id"], int)
        self.assertIsInstance(payload["name"], str)
        return payload

    def list_resources(self, *extra_args):
        payload = self.run_ok("resource-list", *extra_args)
        return payload["resources"]

    # ---- 成功路径 ----

    def test_acceptance_rename_and_filter_by_new_name(self):
        """验收场景：资源 1 改名后目录筛选立即使用新名称，预约归属不变。"""
        first_id = self.add_resource("一号会议室")
        second_id = self.add_resource("二号会议室")
        self.assertEqual([first_id, second_id], [1, 2])
        booking = self.run_ok(
            "reserve", "--resource", "1",
            "--start", "2026-10-05T09:00", "--end", "2026-10-05T10:00",
        )
        self.assertEqual(booking["booking_id"], 1)

        payload = self.rename("0001", "  二号会议室  ")
        self.assertEqual(payload, {"resource_id": 1, "name": "二号会议室"})

        # 目录筛选立即使用新名称，按原标识顺序返回两个资源。
        self.assertEqual(
            self.list_resources("--contains", "二号会议室"),
            [
                {"resource_id": 1, "name": "二号会议室"},
                {"resource_id": 2, "name": "二号会议室"},
            ],
        )
        # 旧名称不再命中资源 1。
        self.assertEqual(self.list_resources("--contains", "一号会议室"), [])
        # 每条命令都是独立进程，本次查询即“重新打开同一数据库”后的读取。
        self.assertEqual(
            self.list_resources(),
            [
                {"resource_id": 1, "name": "二号会议室"},
                {"resource_id": 2, "name": "二号会议室"},
            ],
        )
        # 整段空闲筛选仍由原预约决定：当天只有资源 2 整段空闲。
        self.assertEqual(
            self.list_resources(
                "--start", "2026-10-05T00:00", "--end", "2026-10-05T23:59"
            ),
            [{"resource_id": 2, "name": "二号会议室"}],
        )
        # 预约仍归属资源 1，起止不变。
        day = self.run_ok("day-query", "--resource", "1", "--date", "2026-10-05")
        self.assertEqual(
            day["bookings"],
            [{"booking_id": 1, "start": "2026-10-05T09:00",
              "end": "2026-10-05T10:00"}],
        )

    def test_rename_to_same_name_succeeds(self):
        """新旧名称相同也成功，只处理指定标识。"""
        self.add_resource("一号会议室")

        payload = self.rename("1", "一号会议室")

        self.assertEqual(payload, {"resource_id": 1, "name": "一号会议室"})
        self.assertEqual(
            self.list_resources(),
            [{"resource_id": 1, "name": "一号会议室"}],
        )

    def test_rename_strips_blank_keeps_inner_space_case_and_cjk(self):
        """新名称去除首尾空白，内部空白、大小写及中文保持原样。"""
        self.add_resource("旧名称")

        payload = self.rename("1", "  Meeting Room 甲  ")

        self.assertEqual(payload, {"resource_id": 1, "name": "Meeting Room 甲"})
        self.assertEqual(
            self.list_resources(),
            [{"resource_id": 1, "name": "Meeting Room 甲"}],
        )

    def test_rename_only_touches_target_resource(self):
        """只改指定标识：其他资源名称与全部预约保持不变。"""
        self.add_resource("一号会议室")
        self.add_resource("二号会议室")
        self.run_ok(
            "reserve", "--resource", "2",
            "--start", "2026-10-05T09:00", "--end", "2026-10-05T10:00",
        )

        self.rename("1", "三号会议室")

        self.assertEqual(
            self.list_resources(),
            [
                {"resource_id": 1, "name": "三号会议室"},
                {"resource_id": 2, "name": "二号会议室"},
            ],
        )
        day = self.run_ok("day-query", "--resource", "2", "--date", "2026-10-05")
        self.assertEqual(
            day["bookings"],
            [{"booking_id": 1, "start": "2026-10-05T09:00",
              "end": "2026-10-05T10:00"}],
        )

    def test_rename_does_not_consume_ids(self):
        """改名不新增资源、不消耗资源或预约标识。"""
        self.add_resource("一号会议室")
        self.run_ok(
            "reserve", "--resource", "1",
            "--start", "2026-10-05T09:00", "--end", "2026-10-05T10:00",
        )

        self.rename("1", "二号会议室")

        # 下一个资源与下一条预约各自获得紧接原有最大标识的标识。
        self.assertEqual(self.add_resource("三号会议室"), 2)
        booking = self.run_ok(
            "reserve", "--resource", "1",
            "--start", "2026-10-05T10:00", "--end", "2026-10-05T11:00",
        )
        self.assertEqual(booking["booking_id"], 2)

    def test_rename_unicode_digit_and_leading_zero_id(self):
        """前导零与 Unicode 十进制数字按数值解释，指向同一资源。"""
        self.add_resource("一号会议室")
        self.add_resource("二号会议室")

        payload = self.rename("０００２", "投影仪")

        self.assertEqual(payload, {"resource_id": 2, "name": "投影仪"})
        self.assertEqual(
            self.list_resources(),
            [
                {"resource_id": 1, "name": "一号会议室"},
                {"resource_id": 2, "name": "投影仪"},
            ],
        )

    def test_rename_does_not_change_cancelled_state(self):
        """改名不影响预约的取消状态。"""
        self.add_resource("一号会议室")
        self.run_ok(
            "reserve", "--resource", "1",
            "--start", "2026-10-05T09:00", "--end", "2026-10-05T10:00",
        )
        self.run_ok("cancel", "--booking", "1")

        self.rename("1", "二号会议室")

        day = self.run_ok(
            "day-query", "--resource", "1",
            "--date", "2026-10-05", "--include-cancelled",
        )
        self.assertEqual(
            day["bookings"],
            [{"booking_id": 1, "start": "2026-10-05T09:00",
              "end": "2026-10-05T10:00", "cancelled": True}],
        )

    # ---- 失败路径：resource_not_found ----

    def test_unknown_resource_returns_not_found(self):
        """资源不存在：resource_not_found、退出码 2、stderr 为空。"""
        self.add_resource("一号会议室")

        self.run_error(
            "resource_not_found",
            "resource-rename", "--resource", "2", "--name", "新名称",
        )
        # 失败不改动已有资源。
        self.assertEqual(
            self.list_resources(),
            [{"resource_id": 1, "name": "一号会议室"}],
        )

    def test_oversized_id_returns_not_found(self):
        """超过 2^63-1 的正整数标识：resource_not_found。"""
        self.add_resource("一号会议室")

        self.run_error(
            "resource_not_found",
            "resource-rename",
            "--resource", "9223372036854775808", "--name", "新名称",
        )
        self.assertEqual(
            self.list_resources(),
            [{"resource_id": 1, "name": "一号会议室"}],
        )

    def test_valid_request_on_missing_db_initializes_then_not_found(self):
        """合法请求访问未建库路径：沿用初始化行为，随后 resource_not_found。"""
        self.assertFalse(self.db_path.exists())

        self.run_error(
            "resource_not_found",
            "resource-rename", "--resource", "1", "--name", "新名称",
        )

        # 合法请求沿用了初始化行为，数据库文件已被创建。
        self.assertTrue(self.db_path.exists())
        # 未消耗标识：首次登记的资源标识仍为 1。
        self.assertEqual(self.add_resource("一号会议室"), 1)

    # ---- 失败路径：invalid_input ----

    def test_missing_arguments_return_invalid_input(self):
        """缺少 --resource 或 --name：invalid_input。"""
        self.run_error("invalid_input", "resource-rename", "--name", "新名称")
        self.run_error("invalid_input", "resource-rename", "--resource", "1")
        self.run_error("invalid_input", "resource-rename")

    def test_unknown_argument_returns_invalid_input(self):
        """出现不支持的参数：invalid_input。"""
        self.run_error(
            "invalid_input",
            "resource-rename", "--resource", "1", "--name", "x", "--bogus",
        )

    def test_empty_or_blank_name_returns_invalid_input(self):
        """名称为空或仅为空白：invalid_input。"""
        self.run_error(
            "invalid_input", "resource-rename", "--resource", "1", "--name", ""
        )
        self.run_error(
            "invalid_input", "resource-rename", "--resource", "1", "--name", "   "
        )
        self.run_error(
            "invalid_input", "resource-rename", "--resource", "1",
            "--name", " \t ",
        )

    def test_invalid_resource_id_returns_invalid_input(self):
        """零、负数、小数和含非数字字符的标识：invalid_input。"""
        for bad in ("0", "000", "-1", "1.5", "1a", " 1", "1 ", "1\n"):
            self.run_error(
                "invalid_input",
                "resource-rename", "--resource", bad, "--name", "新名称",
            )

    def test_invalid_input_takes_priority_over_not_found(self):
        """输入合法性优先：不存在的标识搭配空名称仍返回 invalid_input。"""
        self.run_error(
            "invalid_input", "resource-rename", "--resource", "99", "--name", ""
        )
        # 超大标识搭配纯空白名称同样只报 invalid_input。
        self.run_error(
            "invalid_input",
            "resource-rename",
            "--resource", "99999999999999999999999999", "--name", "  ",
        )

    def test_invalid_input_does_not_create_database_file(self):
        """非法输入指向尚不存在的数据库路径时，不得创建文件。"""
        missing_db = Path(self._tmpdir.name) / "should-not-exist.sqlite"
        self.run_error(
            "invalid_input",
            "resource-rename", "--resource", "1", "--name", "  ",
            db=missing_db,
        )
        self.assertFalse(missing_db.exists())

    def test_invalid_input_does_not_change_existing_database(self):
        """已有数据库上非法输入失败后，资源与预约均不变。"""
        self.add_resource("一号会议室")
        self.run_ok(
            "reserve", "--resource", "1",
            "--start", "2026-10-05T09:00", "--end", "2026-10-05T10:00",
        )

        self.run_error(
            "invalid_input", "resource-rename", "--resource", "1", "--name", ""
        )
        self.run_error(
            "invalid_input", "resource-rename", "--resource", "0", "--name", "x"
        )

        self.assertEqual(
            self.list_resources(),
            [{"resource_id": 1, "name": "一号会议室"}],
        )
        day = self.run_ok("day-query", "--resource", "1", "--date", "2026-10-05")
        self.assertEqual(
            day["bookings"],
            [{"booking_id": 1, "start": "2026-10-05T09:00",
              "end": "2026-10-05T10:00"}],
        )
        # 失败不消耗标识：下一个资源与预约标识紧接原有最大值。
        self.assertEqual(self.add_resource("二号会议室"), 2)
        booking = self.run_ok(
            "reserve", "--resource", "1",
            "--start", "2026-10-05T10:00", "--end", "2026-10-05T11:00",
        )
        self.assertEqual(booking["booking_id"], 2)


if __name__ == "__main__":
    unittest.main()
