"""标识文本夹带换行/空白字符的回归测试（仅标准库，python -m unittest 可发现）。

修复前的两个缺陷：
- Python 正则中 ``$`` 允许在末尾换行符之前匹配，``"1\\n"`` 因此被当成标识 1，
  带换行的资源/预约标识可能实际创建或取消预约；
- ``"0\\n"``、``"000\\n"`` 绕过正则后在逐字符 ``int()`` 处抛出未捕获的
  ValueError，进程以异常堆栈、退出码 1 结束。

修复后这类输入一律返回已有的 ``{"error": "invalid_input"}``（退出码 2，
标准错误为空），不自动去除任何空白。通过公开命令行入口
``python -m booking --db <文件> <命令>`` 准备数据并断言，
每个用例使用独立的临时 SQLite 数据库，结束后自动清理；
不依赖已有数据库、当前日期、机器时区或任何第三方库。
断言均基于解析后的 JSON 内容，不依赖输出对象的键顺序。

覆盖的公开行为：
- reserve/day-query 的 --resource、cancel 的 --booking 在标识末尾夹带 LF 时
  一律 invalid_input；``"0\\n"``、``"000\\n"`` 同样 invalid_input 而非异常；
- 首尾空格、制表符、CR、CRLF 或夹在数字中间的换行均返回同一拒绝结果；
- 校验先于资源存在与冲突判断：资源/预约不存在、旧库未迁移时仍返回 invalid_input；
- 失败请求不新建数据库文件、不在已有库新增/取消/改动记录、不消耗标识，
  失败前后 day-query 结果逐字节一致，原预约仍可用正常标识取消；
- 正常标识数值语义不变：``0001`` 与 ``1`` 指向同一记录，Unicode 十进制数字
  按数值解释，无换行的全零文本仍为 invalid_input，任意长度合法正整数继续支持，
  大于 9223372036854775807 仍按 not_found 处理。
"""

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# 项目根目录（booking 包所在目录），子进程以此作为工作目录，
# 保证 `python -m booking` 与测试运行时的当前目录无关。
PROJECT_ROOT = Path(__file__).resolve().parent

# 固定的样例日期与起止时间（固定 UTC+08:00 的本地时间，与运行环境无关）。
DAY = "2026-10-05"
START = f"{DAY}T09:00"
END = f"{DAY}T10:00"
LATER_START = f"{DAY}T11:00"
LATER_END = f"{DAY}T12:00"

# SQLite INTEGER 主键边界：2^63 起越界。
OVER_MAX_ID = "9223372036854775808"

# 各类夹带空白/换行的非法标识文本（作为同一个参数值传入的真实字符）。
LF = "\n"
CR = "\r"
WHITESPACE_ID_TEXTS = [
    "1" + LF,          # 末尾 LF：修复前被当成标识 1
    "0" + LF,          # 全零加 LF：修复前抛出未捕获 ValueError
    "000" + LF,        # 同上，多个前导零
    LF + "1",          # 开头 LF
    " 1",              # 开头空格
    "1 ",              # 末尾空格
    "1\t",             # 末尾制表符
    "1" + CR,          # 末尾 CR
    "1" + CR + LF,     # 末尾 CRLF
    "1" + LF + "2",    # 换行夹在数字中间
    " 0001 ",          # 首尾空格包裹前导零
    OVER_MAX_ID + LF,  # 越界值加 LF：输入合法性先于越界/不存在判断
]


class NewlineIdTestCase(unittest.TestCase):
    """每个用例一个独立的临时目录和数据库路径，tearDown 自动清理。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="booking-test-newline-")
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
        """运行命令并断言退出码 0、标准输出为单个 JSON 对象且 stderr 为空。"""
        proc = self.run_cli(*args, db=db)
        self.assertEqual(
            proc.returncode, 0,
            msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}",
        )
        return self.assert_single_json_object(proc)

    def run_invalid(self, *args, db=None):
        """断言退出码 2、stdout 恰为 invalid_input、stderr 完全为空（无异常堆栈）。"""
        proc = self.run_cli(*args, db=db)
        self.assertEqual(
            proc.returncode, 2,
            msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}",
        )
        self.assertEqual(proc.stderr, "", msg=f"stderr 不应有输出：{proc.stderr!r}")
        payload = self.assert_single_json_object(proc)
        self.assertEqual(payload, {"error": "invalid_input"})
        return proc

    def assert_single_json_object(self, proc):
        """stdout 恰好是一个 JSON 对象加换行，stderr 完全为空（无异常堆栈）。"""
        self.assertEqual(proc.stderr, "", msg=f"stderr 不应有输出：{proc.stderr!r}")
        lines = proc.stdout.splitlines()
        self.assertEqual(len(lines), 1, msg=f"stdout 应只有一个 JSON 对象：{proc.stdout!r}")
        payload = json.loads(lines[0])
        self.assertIsInstance(payload, dict)
        self.assertEqual(proc.stdout, json.dumps(payload, ensure_ascii=False) + "\n")
        return payload

    def add_resource(self, name="测试资源"):
        payload = self.run_ok("resource-add", "--name", name)
        self.assertIsInstance(payload["resource_id"], int)
        self.assertGreater(payload["resource_id"], 0)
        return payload["resource_id"]

    def reserve_ok(self, resource_id, start=START, end=END):
        payload = self.run_ok(
            "reserve", "--resource", str(resource_id), "--start", start, "--end", end
        )
        self.assertEqual(
            payload,
            {
                "booking_id": payload["booking_id"],
                "resource_id": int(resource_id),
                "start": start,
                "end": end,
            },
        )
        return payload["booking_id"]

    def day_query(self, resource_text, date=DAY):
        """以原始文本形式的资源标识做按日查询，返回解析结果。"""
        return self.run_ok("day-query", "--resource", resource_text, "--date", date)

    # ---- 2026-10-05 有效预约上的端到端回归 ----

    def test_resource_id_with_lf_is_rejected_and_state_unchanged(self):
        """在有效预约上以带 LF 的资源标识做 reserve/day-query：全部失败且结果前后一致。"""
        resource_id = self.add_resource()
        booking_id = self.reserve_ok(resource_id)
        before = self.day_query(str(resource_id))

        # 末尾 LF 的正整数标识曾被当成 1，必须拒绝且绝不创建预约。
        self.run_invalid(
            "reserve", "--resource", "1" + LF,
            "--start", LATER_START, "--end", LATER_END,
        )
        self.run_invalid("day-query", "--resource", "1" + LF, "--date", DAY)
        # 全零加 LF 曾触发 ValueError：同样返回 invalid_input。
        self.run_invalid(
            "reserve", "--resource", "0" + LF,
            "--start", LATER_START, "--end", LATER_END,
        )
        self.run_invalid("day-query", "--resource", "000" + LF, "--date", DAY)

        # 失败前后 day-query 逐字节一致，没有新增预约。
        after = self.day_query(str(resource_id))
        self.assertEqual(after, before)
        self.assertEqual(
            after["bookings"],
            [{"booking_id": booking_id, "start": START, "end": END}],
        )

    def test_booking_id_with_lf_is_rejected_and_booking_still_cancellable(self):
        """在有效预约上以带 LF 的预约标识取消：失败；原预约仍可用正常标识取消。"""
        resource_id = self.add_resource()
        booking_id = self.reserve_ok(resource_id)
        before = self.day_query(str(resource_id))

        # "1\n" 曾被当成标识 1 而误取消；"0\n" 曾抛出 ValueError。
        self.run_invalid("cancel", "--booking", "1" + LF)
        self.run_invalid("cancel", "--booking", "0" + LF)
        self.run_invalid("cancel", "--booking", "000" + LF)

        # 预约未被误伤：查询不变、同时段仍冲突。
        self.assertEqual(self.day_query(str(resource_id)), before)
        proc = self.run_cli(
            "reserve", "--resource", str(resource_id),
            "--start", START, "--end", END,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stdout), {"error": "booking_conflict"})

        # 原预约仍可用正常标识取消，取消后时段释放。
        payload = self.run_ok("cancel", "--booking", str(booking_id))
        self.assertEqual(payload, {"booking_id": booking_id, "cancelled": True})
        self.assertEqual(self.day_query(str(resource_id))["bookings"], [])

    # ---- 各类首尾/夹空白字符：三个命令一律拒绝 ----

    def test_all_whitespace_variants_are_rejected_by_every_command(self):
        """首尾空格、制表符、CR、CRLF 与数字中间换行均返回 invalid_input。"""
        resource_id = self.add_resource()
        self.reserve_ok(resource_id)

        for text in WHITESPACE_ID_TEXTS:
            with self.subTest(id_text=repr(text)):
                self.run_invalid(
                    "reserve", "--resource", text,
                    "--start", LATER_START, "--end", LATER_END,
                )
                self.run_invalid("day-query", "--resource", text, "--date", DAY)
                self.run_invalid("cancel", "--booking", text)

    def test_validation_precedes_existence_and_conflict(self):
        """标识不存在或资源/预约缺失时，带空白的标识仍先返回 invalid_input。"""
        # 空库：标识 2/999 本就不存在，但合法性校验应先于存在性判断。
        for text in ["2" + LF, "999" + LF, "1 "]:
            with self.subTest(id_text=repr(text)):
                self.run_invalid(
                    "reserve", "--resource", text,
                    "--start", LATER_START, "--end", LATER_END,
                )
                self.run_invalid("day-query", "--resource", text, "--date", DAY)
                self.run_invalid("cancel", "--booking", text)

    # ---- 无副作用：不建文件、不改记录、不消耗标识、不迁移旧库 ----

    def test_invalid_whitespace_id_does_not_create_database(self):
        """不存在的数据库路径上，0 加 LF 等非法标识不创建文件、无异常堆栈。"""
        cases = [
            ("reserve", ["reserve", "--resource", "0" + LF,
                         "--start", START, "--end", END]),
            ("day-query", ["day-query", "--resource", "0" + LF, "--date", DAY]),
            ("cancel", ["cancel", "--booking", "0" + LF]),
            ("reserve-1-lf", ["reserve", "--resource", "1" + LF,
                              "--start", START, "--end", END]),
            ("cancel-000-lf", ["cancel", "--booking", "000" + LF]),
        ]
        for index, (label, args) in enumerate(cases):
            with self.subTest(case=label):
                missing_db = Path(self._tmpdir.name) / f"should-not-exist-{index}.sqlite"
                self.run_invalid(*args, db=missing_db)
                self.assertFalse(missing_db.exists())

    def test_failed_requests_change_nor_consume_ids(self):
        """已有库上各类失败不新增/取消/改动记录，也不消耗预约标识。"""
        resource_id = self.add_resource()
        booking_id = self.reserve_ok(resource_id)
        before = self.day_query(str(resource_id))

        for text in WHITESPACE_ID_TEXTS:
            self.run_invalid("cancel", "--booking", text)

        # 相邻时段成功时获得紧随其后的标识，说明失败没有消耗标识。
        adjacent_id = self.reserve_ok(resource_id, LATER_START, LATER_END)
        self.assertEqual(adjacent_id, booking_id + 1)

        after = self.day_query(str(resource_id))
        self.assertEqual(
            after["bookings"],
            [
                {"booking_id": booking_id, "start": START, "end": END},
                {"booking_id": adjacent_id, "start": LATER_START, "end": LATER_END},
            ],
        )
        self.assertEqual(before["bookings"], after["bookings"][:1])

    def test_legacy_database_is_not_migrated_or_modified(self):
        """未迁移旧库遇到带空白标识请求：返回 invalid_input，且不补齐列、不改数据。"""
        legacy_db = Path(self._tmpdir.name) / "legacy.sqlite"
        conn = sqlite3.connect(str(legacy_db))
        try:
            conn.executescript(
                """
                CREATE TABLE resources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL
                );
                CREATE TABLE bookings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    resource_id INTEGER NOT NULL,
                    start TEXT NOT NULL,
                    end TEXT NOT NULL,
                    FOREIGN KEY (resource_id) REFERENCES resources(id)
                );
                """
            )
            conn.execute(
                "INSERT INTO resources (id, name) VALUES (?, ?)", (1, "旧库资源")
            )
            conn.execute(
                "INSERT INTO bookings (id, resource_id, start, end) "
                "VALUES (?, ?, ?, ?)",
                (1, 1, START, END),
            )
            conn.commit()
        finally:
            conn.close()

        for text in ["1" + LF, "0" + LF, "000" + LF]:
            with self.subTest(id_text=repr(text)):
                self.run_invalid("cancel", "--booking", text, db=legacy_db)
                self.run_invalid(
                    "reserve", "--resource", text,
                    "--start", LATER_START, "--end", LATER_END,
                    db=legacy_db,
                )
                self.run_invalid(
                    "day-query", "--resource", text, "--date", DAY, db=legacy_db
                )

        # 旧库既未迁移（仍无 cancelled 列），记录也原封不动。
        conn = sqlite3.connect(str(legacy_db))
        try:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(bookings)")]
            rows = conn.execute(
                "SELECT id, resource_id, start, end FROM bookings"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(columns, ["id", "resource_id", "start", "end"])
        self.assertEqual(rows, [(1, 1, START, END)])

    # ---- 正常标识的数值语义保持不变 ----

    def test_normal_numeric_ids_keep_existing_semantics(self):
        """0001 与 1 等价；Unicode 十进制数字按数值解释；全零文本仍非法。"""
        resource_id = self.add_resource()
        booking_id = self.reserve_ok(resource_id)
        self.assertEqual((resource_id, booking_id), (1, 1))

        # 前导零按数值解释：0001 查到资源 1 与预约 1。
        payload = self.day_query("0001")
        self.assertEqual(payload["resource_id"], 1)
        self.assertEqual(
            payload["bookings"],
            [{"booking_id": 1, "start": START, "end": END}],
        )

        # 已有的 Unicode 十进制数字（阿拉伯-印度数字 ١ 表示 1）继续按数值解释。
        payload = self.day_query("٠٠٠١")  # 0001
        self.assertEqual(payload["resource_id"], 1)

        # 无换行的全零文本仍为 invalid_input。
        self.run_invalid(
            "reserve", "--resource", "000",
            "--start", LATER_START, "--end", LATER_END,
        )
        self.run_invalid("day-query", "--resource", "0", "--date", DAY)
        self.run_invalid("cancel", "--booking", "000")

        # 大于 2^63-1 的合法正整数仍按“不存在”处理（而非 invalid_input）。
        self.run_error_code(
            "resource_not_found",
            "day-query", "--resource", OVER_MAX_ID, "--date", DAY,
        )
        self.run_error_code(
            "resource_not_found",
            "reserve", "--resource", OVER_MAX_ID,
            "--start", LATER_START, "--end", LATER_END,
        )
        self.run_error_code("booking_not_found", "cancel", "--booking", OVER_MAX_ID)

        # 任意长度合法正整数继续支持：连续两千个 9 仍只按不存在处理。
        self.run_error_code(
            "resource_not_found",
            "day-query", "--resource", "9" * 2000, "--date", DAY,
        )

    def run_error_code(self, expected_error, *args):
        """断言退出码 2 且 stdout 恰为指定 not_found 错误对象（合法的不存在标识）。"""
        proc = self.run_cli(*args)
        self.assertEqual(
            proc.returncode, 2,
            msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}",
        )
        self.assertEqual(proc.stderr, "")
        self.assertEqual(json.loads(proc.stdout), {"error": expected_error})


if __name__ == "__main__":
    unittest.main()
