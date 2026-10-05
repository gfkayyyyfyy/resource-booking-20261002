"""resource-rename 资源改名的回归测试（仅标准库，python -m unittest 可发现）。

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

# 不是正整数的标识文本：零、负数、小数、含非数字字符（含首尾空白）。
INVALID_IDS = [
    "0",
    "000",
    "-1",
    "+1",
    "1.0",
    "0.5",
    "1e3",
    "abc",
    "1a",
    " 1",
    "1 ",
    "\t1",
    "1\n",
    "",
]


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

    def run_error(self, expected, *args, db=None):
        """运行命令并断言业务错误协议：退出码 2、stderr 为空（无异常堆栈）、
        标准输出只有指定的错误对象这一个 JSON 对象。"""
        proc = self.run_cli(*args, db=db)
        self.assertEqual(
            proc.returncode, 2,
            msg=f"args={args!r}: 预期退出码 2，stdout={proc.stdout!r} stderr={proc.stderr!r}",
        )
        self.assertEqual(
            proc.stderr, "",
            msg=f"args={args!r}: 预期 stderr 为空（不得输出异常堆栈），实际 stderr={proc.stderr!r}",
        )
        self.assertEqual(
            proc.stdout.strip(),
            json.dumps(expected, ensure_ascii=False),
            msg=f"args={args!r}: 预期标准输出只有 {expected!r}，实际 stdout={proc.stdout!r}",
        )
        self.assertEqual(json.loads(proc.stdout), expected)
        return proc

    def run_invalid_input(self, *args, db=None):
        """运行命令并断言返回 {"error": "invalid_input"}。"""
        return self.run_error({"error": "invalid_input"}, *args, db=db)

    def run_not_found(self, *args, db=None):
        """运行命令并断言返回 {"error": "resource_not_found"}。"""
        return self.run_error({"error": "resource_not_found"}, *args, db=db)

    def add_resource(self, name):
        """登记资源并断言成功输出仅含 resource_id 与 name，返回该对象。"""
        payload = self.run_ok("resource-add", "--name", name)
        self.assertEqual(
            set(payload.keys()), {"resource_id", "name"},
            msg=f"name={name!r}: 成功输出应仅含 resource_id 与 name，实际 {payload!r}",
        )
        return payload

    def rename(self, resource, name):
        """改名并断言成功输出仅含 resource_id 与 name，返回该对象。"""
        payload = self.run_ok(
            "resource-rename", "--resource", str(resource), "--name", name
        )
        self.assertEqual(
            set(payload.keys()), {"resource_id", "name"},
            msg=f"resource={resource!r} name={name!r}: 成功输出应仅含 "
            f"resource_id 与 name，实际 {payload!r}",
        )
        return payload

    def list_resources(self, *extra):
        """运行 resource-list（可带 --contains 等参数），返回 resources 数组。"""
        payload = self.run_ok("resource-list", *extra)
        self.assertEqual(set(payload.keys()), {"resources"})
        return payload["resources"]

    def day_query(self, resource_id, date=BOOKING_DATE):
        """运行 day-query，返回解析后的完整结果对象。"""
        return self.run_ok(
            "day-query", "--resource", str(resource_id), "--date", date
        )

    def free_query(self, resource_id, start, end):
        """运行 free-query，返回解析后的完整结果对象。"""
        return self.run_ok(
            "free-query", "--resource", str(resource_id),
            "--start", start, "--end", end,
        )

    # ---- 验收场景：改名后目录筛选与整段空闲筛选 ----

    def test_acceptance_rename_and_directory_filters(self):
        """资源 1“一号会议室”（有 09:00–10:00 预约）、资源 2“二号会议室”；
        以 0001 把资源 1 改名为带首尾空白的“  二号会议室  ”，返回
        {"resource_id":1,"name":"二号会议室"}；随后 --contains 二号会议室
        按原标识顺序返回两个资源，重开数据库后一致，整段空闲筛选仍由原
        预约决定。"""
        first = self.add_resource("一号会议室")
        second = self.add_resource("二号会议室")
        self.assertEqual((first["resource_id"], second["resource_id"]), (1, 2))
        booking = self.run_ok(
            "reserve", "--resource", "1",
            "--start", f"{BOOKING_DATE}T09:00", "--end", f"{BOOKING_DATE}T10:00",
        )
        self.assertEqual(booking["booking_id"], 1)

        payload = self.rename("0001", "  二号会议室  ")
        self.assertEqual(
            payload, {"resource_id": 1, "name": "二号会议室"},
            msg=f"改名应返回原数值标识与保存后的名称，实际 {payload!r}",
        )

        # 目录筛选立即使用新名称，按原标识数值升序返回两个资源。
        expected = [
            {"resource_id": 1, "name": "二号会议室"},
            {"resource_id": 2, "name": "二号会议室"},
        ]
        self.assertEqual(
            self.list_resources("--contains", "二号会议室"), expected,
            msg="--contains 应按新名称命中并按原标识顺序返回",
        )
        self.assertEqual(self.list_resources(), expected)
        # 旧名称不再命中任何资源。
        self.assertEqual(self.list_resources("--contains", "一号会议室"), [])

        # 整段空闲筛选仍由原预约决定：资源 1 在 09:00–10:00 被占用。
        window = ["--start", f"{BOOKING_DATE}T09:00", "--end", f"{BOOKING_DATE}T10:00"]
        self.assertEqual(
            self.list_resources(*window),
            [{"resource_id": 2, "name": "二号会议室"}],
            msg="改名不得改变预约占用，资源 1 在预约时段内不应入选",
        )
        free = self.free_query(1, f"{BOOKING_DATE}T08:00", f"{BOOKING_DATE}T12:00")
        self.assertEqual(
            free["free_slots"],
            [
                {"start": f"{BOOKING_DATE}T08:00", "end": f"{BOOKING_DATE}T09:00"},
                {"start": f"{BOOKING_DATE}T10:00", "end": f"{BOOKING_DATE}T12:00"},
            ],
            msg=f"改名不得改变预约时段，实际 {free!r}",
        )

        # 改名不新增资源、不消耗资源或预约标识。
        third = self.add_resource("三号会议室")
        self.assertEqual(third["resource_id"], 3)
        rebooked = self.run_ok(
            "reserve", "--resource", "2",
            "--start", f"{BOOKING_DATE}T09:00", "--end", f"{BOOKING_DATE}T10:00",
        )
        self.assertEqual(rebooked["booking_id"], 2)

        # 重新打开同一数据库（每条命令本就是独立进程）后结果仍一致。
        self.assertEqual(
            self.list_resources("--contains", "二号会议室"), expected,
        )
        # 整段空闲筛选仍由原预约决定：资源 1 与 2 在 09:00–10:00 均被占用。
        self.assertEqual(
            self.list_resources(*window),
            [{"resource_id": 3, "name": "三号会议室"}],
        )
        # 资源 1 的预约保持原归属、起止与取消状态。
        self.assertEqual(
            self.day_query(1)["bookings"],
            [{"booking_id": 1, "start": f"{BOOKING_DATE}T09:00",
              "end": f"{BOOKING_DATE}T10:00"}],
        )

    # ---- 成功路径：名称规则与边界 ----

    def test_surrounding_whitespace_stripped_inner_and_case_kept(self):
        """首尾空白去除；内部连续空白、大小写与中文原样保存。"""
        self.add_resource("北区会议室")
        payload = self.rename(1, " \t\nRoom  A  会议室\r\n ")
        self.assertEqual(payload["name"], "Room  A  会议室")
        self.assertEqual(
            self.list_resources(),
            [{"resource_id": 1, "name": "Room  A  会议室"}],
        )

    def test_rename_to_same_name_succeeds(self):
        """名称不变也成功，只处理指定标识。"""
        self.add_resource("一号会议室")
        self.add_resource("二号会议室")
        payload = self.rename(1, "一号会议室")
        self.assertEqual(payload, {"resource_id": 1, "name": "一号会议室"})
        self.assertEqual(
            self.list_resources(),
            [
                {"resource_id": 1, "name": "一号会议室"},
                {"resource_id": 2, "name": "二号会议室"},
            ],
        )

    def test_rename_to_other_resources_name_allowed(self):
        """允许改成其他资源的名称；其他资源名称不变。"""
        self.add_resource("一号会议室")
        self.add_resource("二号会议室")
        payload = self.rename(2, "一号会议室")
        self.assertEqual(payload, {"resource_id": 2, "name": "一号会议室"})
        self.assertEqual(
            self.list_resources(),
            [
                {"resource_id": 1, "name": "一号会议室"},
                {"resource_id": 2, "name": "一号会议室"},
            ],
        )

    def test_leading_zero_and_unicode_digit_ids(self):
        """前导零与 Unicode 十进制数字按数值解释，指向同一标识。"""
        self.add_resource("一号会议室")
        payload = self.rename("0001", "二号会议室")
        self.assertEqual(payload, {"resource_id": 1, "name": "二号会议室"})
        # 阿拉伯文数字 U+0661 按数值 1 解释。
        payload = self.rename("١", "三号会议室")
        self.assertEqual(payload, {"resource_id": 1, "name": "三号会议室"})
        self.assertEqual(
            self.list_resources(),
            [{"resource_id": 1, "name": "三号会议室"}],
        )

    def test_rename_does_not_touch_bookings(self):
        """改名不改变任何预约的资源归属、起止时间与取消状态。"""
        self.add_resource("一号会议室")
        self.add_resource("二号会议室")
        self.run_ok(
            "reserve", "--resource", "1",
            "--start", f"{BOOKING_DATE}T09:00", "--end", f"{BOOKING_DATE}T10:00",
        )
        self.run_ok(
            "reserve", "--resource", "2",
            "--start", f"{BOOKING_DATE}T11:00", "--end", f"{BOOKING_DATE}T12:00",
        )
        self.run_ok("cancel", "--booking", "2")
        day1_before = self.day_query(1)
        day2_before = self.run_ok(
            "day-query", "--resource", "2", "--date", BOOKING_DATE,
            "--include-cancelled",
        )

        self.rename(1, "改名后")
        self.rename(2, "改名后")

        self.assertEqual(self.day_query(1), day1_before)
        self.assertEqual(
            self.run_ok(
                "day-query", "--resource", "2", "--date", BOOKING_DATE,
                "--include-cancelled",
            ),
            day2_before,
        )
        # 取消状态不变：已取消的预约 2 再次取消仍返回 booking_not_found。
        self.run_error({"error": "booking_not_found"}, "cancel", "--booking", "2")

    # ---- 失败路径：非法输入 ----

    def test_missing_arguments_rejected(self):
        """缺少 --resource 或 --name、参数缺值、未知参数一律 invalid_input。"""
        self.add_resource("一号会议室")
        self.run_invalid_input("resource-rename")
        self.run_invalid_input("resource-rename", "--resource", "1")
        self.run_invalid_input("resource-rename", "--name", "新名称")
        self.run_invalid_input("resource-rename", "--resource", "--name", "新名称")
        self.run_invalid_input("resource-rename", "--resource", "1", "--name")
        self.run_invalid_input(
            "resource-rename", "--resource", "1", "--name", "新名称", "--bogus"
        )
        # 已有数据不变。
        self.assertEqual(
            self.list_resources(),
            [{"resource_id": 1, "name": "一号会议室"}],
        )

    def test_blank_names_rejected(self):
        """空文本与仅由空白组成的名称一律 invalid_input。"""
        self.add_resource("一号会议室")
        for name in BLANK_NAMES:
            with self.subTest(name=name):
                self.run_invalid_input(
                    "resource-rename", "--resource", "1", "--name", name
                )
        self.assertEqual(
            self.list_resources(),
            [{"resource_id": 1, "name": "一号会议室"}],
        )

    def test_invalid_ids_rejected(self):
        """零、负数、小数与含非数字字符（含首尾空白）的标识一律 invalid_input。"""
        self.add_resource("一号会议室")
        for resource in INVALID_IDS:
            with self.subTest(resource=resource):
                self.run_invalid_input(
                    "resource-rename", "--resource", resource, "--name", "新名称"
                )
        self.assertEqual(
            self.list_resources(),
            [{"resource_id": 1, "name": "一号会议室"}],
        )

    def test_invalid_input_takes_priority_over_not_found(self):
        """不存在的标识搭配空名称仍返回 invalid_input。"""
        self.add_resource("一号会议室")
        self.run_invalid_input(
            "resource-rename", "--resource", "999", "--name", ""
        )
        self.run_invalid_input(
            "resource-rename", "--resource", "999", "--name", "   "
        )
        self.run_invalid_input(
            "resource-rename", "--resource", "0", "--name", ""
        )

    # ---- 失败路径：资源不存在 ----

    def test_unknown_and_oversized_ids_return_not_found(self):
        """未知标识与超过 2^63-1 的正整数标识返回 resource_not_found。"""
        self.add_resource("一号会议室")
        for resource in (
            "2",
            "999",
            "9223372036854775807",  # 2^63-1：合法范围内的未知标识
            "9223372036854775808",  # 2^63：越界，按不存在处理
            "009223372036854775808",
            "9" * 5000,
        ):
            with self.subTest(resource=resource):
                self.run_not_found(
                    "resource-rename", "--resource", resource, "--name", "新名称"
                )
        self.assertEqual(
            self.list_resources(),
            [{"resource_id": 1, "name": "一号会议室"}],
        )

    def test_valid_request_on_missing_path_initializes_database(self):
        """合法请求访问未建库路径：沿用现有初始化行为，随后返回
        resource_not_found；随后首个登记的资源标识仍为 1。"""
        self.assertFalse(self.db_path.exists())
        self.run_not_found(
            "resource-rename", "--resource", "1", "--name", "新名称"
        )
        self.assertTrue(
            self.db_path.exists(),
            msg="合法请求应沿用现有初始化行为创建数据库文件",
        )
        payload = self.add_resource("一号会议室")
        self.assertEqual(payload["resource_id"], 1)

    def test_invalid_input_does_not_create_database_file(self):
        """非法输入不新建数据库文件。"""
        self.assertFalse(self.db_path.exists())
        self.run_invalid_input(
            "resource-rename", "--resource", "1", "--name", ""
        )
        self.assertFalse(self.db_path.exists())
        self.run_invalid_input(
            "resource-rename", "--resource", "0", "--name", "新名称"
        )
        self.assertFalse(self.db_path.exists())
        self.run_invalid_input("resource-rename", "--resource", "1")
        self.assertFalse(self.db_path.exists())

    def test_failures_do_not_change_existing_data(self):
        """各类失败前后，目录与预约查询结果完全相同，且不消耗标识。"""
        self.add_resource("一号会议室")
        self.run_ok(
            "reserve", "--resource", "1",
            "--start", f"{BOOKING_DATE}T09:00", "--end", f"{BOOKING_DATE}T10:00",
        )
        list_before = self.list_resources()
        day_before = self.day_query(1)

        self.run_invalid_input(
            "resource-rename", "--resource", "1", "--name", ""
        )
        self.run_invalid_input(
            "resource-rename", "--resource", "-1", "--name", "新名称"
        )
        self.run_not_found(
            "resource-rename", "--resource", "999", "--name", "新名称"
        )
        self.run_not_found(
            "resource-rename", "--resource", "9223372036854775808",
            "--name", "新名称",
        )

        self.assertEqual(self.list_resources(), list_before)
        self.assertEqual(self.day_query(1), day_before)

        # 失败不消耗资源或预约标识。
        self.assertEqual(self.add_resource("二号会议室")["resource_id"], 2)
        booking = self.run_ok(
            "reserve", "--resource", "2",
            "--start", f"{BOOKING_DATE}T09:00", "--end", f"{BOOKING_DATE}T10:00",
        )
        self.assertEqual(booking["booking_id"], 2)


if __name__ == "__main__":
    unittest.main()
