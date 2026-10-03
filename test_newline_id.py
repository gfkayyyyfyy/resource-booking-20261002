"""标识文本末尾换行（以及其他位置空白字符）拒绝的回归测试。

通过公开命令行入口 `python -m booking --db <文件> <命令>` 准备数据并断言，
每个用例使用独立的临时 SQLite 数据库，结束后自动清理；
不依赖已有数据库、当前日期、机器时区或任何第三方库。
断言均基于解析后的 JSON 内容，不依赖输出对象的键顺序。

历史缺陷：正整数正则用 `$` 收尾，而 `$` 允许在末尾 LF 之前匹配，于是
- reserve/day-query 的 --resource、cancel 的 --booking 传入 "1\n" 时，
  尾随换行被静默吞掉，实际按标识 1 创建/取消了预约；
- 传入 "0\n"、"000\n" 时，前导零跳过逻辑停在 '\n' 上，int('\n')
  抛出未捕获的 ValueError，进程退出码 1 且 stderr 出现异常堆栈。

覆盖的公开行为：
- 标识文本的任意位置出现 LF/CR/空格/制表符（首尾或夹在数字中间）一律
  返回 {"error": "invalid_input"}，退出码 2，stdout 只有这一个 JSON 对象，
  stderr 完全为空（无异常堆栈）；不自动去除任何空白；
- 该校验先于资源存在、预约存在与冲突判断：标识即便指向不存在的记录，
  或时段本会冲突，带空白的标识仍只返回 invalid_input；
- 带空白标识的失败请求不新建文件（指向不存在的数据库时）、不新增或取消
  记录、不消耗 AUTOINCREMENT 标识，失败前后 day-query 结果逐字节一致，
  原预约仍可用正常标识取消；尚未迁移的旧库不会因此被迁移；
- 正常标识的数值语义不变：0001 与 1 指向同一记录，可用的 Unicode 十进制
  数字继续按数值解释，无换行的全零文本仍为 invalid_input，任意长度的合法
  正整数继续支持，大于 9223372036854775807 时仍按不存在的标识处理。
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

# 各类混入空白的标识文本；LF/CR 均作为同一个参数值内的真实字符传入。
LF = "1\n"
ZERO_LF = "0\n"
ZERO_ZERO_ZERO_LF = "000\n"
WHITESPACE_VARIANTS = [
    "1\n",        # 末尾 LF：曾被静默当成标识 1
    "0\n",        # 末尾 LF + 全零：曾触发未捕获 ValueError
    "000\n",      # 多个前导零后接 LF
    "\n1",        # 开头 LF
    "1\n2",       # 换行夹在数字中间
    " 1",         # 开头空格
    "1 ",         # 末尾空格
    "\t1",        # 开头制表符
    "1\t",        # 末尾制表符
    " 1 ",        # 首尾空格
    "1\r",        # 末尾 CR
    "1\r\n",      # 末尾 CRLF
]

# 超过 SQLite INTEGER 主键上限（2^63-1）的正整数：合法但必然不存在。
OVER_MAX_ID = "9223372036854775808"


def create_legacy_db(path):
    """创建取消功能上线前的旧库（无 cancelled 列），含一条资源与预约。"""
    conn = sqlite3.connect(str(path))
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
        conn.execute("INSERT INTO resources (id, name) VALUES (?, ?)", (1, "旧库资源"))
        conn.execute(
            "INSERT INTO bookings (id, resource_id, start, end) "
            "VALUES (?, ?, ?, ?)",
            (1, 1, START, END),
        )
        conn.commit()
    finally:
        conn.close()


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

    def assert_invalid_input(self, *args, db=None):
        """断言退出码 2、stdout 恰为 invalid_input 对象加换行、stderr 完全为空。"""
        proc = self.run_cli(*args, db=db)
        self.assertEqual(
            proc.returncode, 2,
            msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}",
        )
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
        # 再次确认没有第二个 JSON 文档或其他杂散输出。
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
        return self.run_ok(
            "day-query", "--resource", resource_text, "--date", date
        )

    # ---- 直接对数据库文件做只读核对（验证无副作用所必需） ----

    def booking_columns(self, db=None):
        conn = sqlite3.connect(str(db or self.db_path))
        try:
            return [row[1] for row in conn.execute("PRAGMA table_info(bookings)")]
        finally:
            conn.close()

    def raw_bookings(self, db=None):
        conn = sqlite3.connect(str(db or self.db_path))
        try:
            return conn.execute(
                "SELECT id, resource_id, start, end, cancelled "
                "FROM bookings ORDER BY id"
            ).fetchall()
        finally:
            conn.close()

    def raw_bookings_without_cancelled(self, db):
        """旧库原样四列（不含 cancelled），用于核对未迁移文件的已有数据。"""
        conn = sqlite3.connect(str(db))
        try:
            return conn.execute(
                "SELECT id, resource_id, start, end FROM bookings ORDER BY id"
            ).fetchall()
        finally:
            conn.close()

    # ---- 指定回归：2026-10-05 有效预约 + 带 LF 的资源/预约标识 ----

    def test_lf_resource_id_rejected_and_day_query_unchanged(self):
        """reserve/day-query 传入带 LF 的资源标识：invalid_input，预约未被创建，查询结果前后一致。"""
        resource_id = self.add_resource()
        booking_id = self.reserve_ok(resource_id)
        expected_bookings = [
            {"booking_id": booking_id, "start": START, "end": END}
        ]
        before = self.day_query(str(resource_id))
        self.assertEqual(before["bookings"], expected_bookings)

        # 其他参数（时间）完全有效，仅资源标识尾部带真实 LF：必须拒绝，
        # 不能把换行吞掉后实际在资源 1 上创建 11:00–12:00 的预约。
        self.assert_invalid_input(
            "reserve", "--resource", LF,
            "--start", f"{DAY}T11:00", "--end", f"{DAY}T12:00",
        )
        # 按日查询同样拒绝带 LF 的资源标识，而不是查到资源 1。
        self.assert_invalid_input(
            "day-query", "--resource", LF, "--date", DAY
        )

        # 失败前后 day-query 结果一致：标识、起止文本、数量与顺序完全相同。
        after = self.day_query(str(resource_id))
        self.assertEqual(after, before)
        self.assertEqual(after["bookings"], expected_bookings)

        # 原预约仍可用正常标识取消。
        payload = self.run_ok("cancel", "--booking", str(booking_id))
        self.assertEqual(payload, {"booking_id": booking_id, "cancelled": True})
        self.assertEqual(self.day_query(str(resource_id))["bookings"], [])

    def test_lf_booking_id_rejected_and_original_booking_still_cancellable(self):
        """cancel 传入带 LF 的预约标识：invalid_input，原预约未被取消且仍可用正常标识取消。"""
        resource_id = self.add_resource()
        booking_id = self.reserve_ok(resource_id)
        expected_bookings = [
            {"booking_id": booking_id, "start": START, "end": END}
        ]

        # "1\n" 不得被当成预约标识 1 取消；"0\n" 曾触发 ValueError 堆栈。
        self.assert_invalid_input("cancel", "--booking", LF)
        self.assert_invalid_input("cancel", "--booking", ZERO_LF)

        # 原预约依旧有效：day-query 仍返回它，且同时段仍冲突。
        self.assertEqual(self.day_query(str(resource_id))["bookings"], expected_bookings)
        proc = self.run_cli(
            "reserve", "--resource", str(resource_id),
            "--start", START, "--end", END,
        )
        self.assertEqual(proc.returncode, 2, msg=proc.stderr)
        self.assertEqual(json.loads(proc.stdout), {"error": "booking_conflict"})

        # 原预约仍可用正常标识取消，取消后时段释放。
        payload = self.run_ok("cancel", "--booking", str(booking_id))
        self.assertEqual(payload, {"booking_id": booking_id, "cancelled": True})
        self.assertEqual(self.day_query(str(resource_id))["bookings"], [])

    # ---- 指向不存在的数据库：0 加 LF 不创建文件、无异常堆栈 ----

    def test_zero_with_lf_on_missing_database_creates_no_file(self):
        """对不存在的数据库使用带 LF 的标识（含 0\\n）：不创建任何文件，stderr 无堆栈。"""
        for label, args in [
            ("reserve 0+LF",
             ["reserve", "--resource", ZERO_LF,
              "--start", f"{DAY}T11:00", "--end", f"{DAY}T12:00"]),
            ("cancel 0+LF", ["cancel", "--booking", ZERO_LF]),
            ("day-query 0+LF",
             ["day-query", "--resource", ZERO_LF, "--date", DAY]),
            ("reserve 1+LF",
             ["reserve", "--resource", LF,
              "--start", f"{DAY}T11:00", "--end", f"{DAY}T12:00"]),
            ("day-query 1+LF",
             ["day-query", "--resource", LF, "--date", DAY]),
        ]:
            with self.subTest(case=label):
                missing_db = Path(self._tmpdir.name) / f"missing-{label.replace(' ', '-')}.sqlite"
                self.assertFalse(missing_db.exists())
                self.assert_invalid_input(*args, db=missing_db)
                # 非法输入先于数据库连接返回：文件（含 SQLite 的 -journal/-wal）均不得出现。
                self.assertFalse(
                    missing_db.exists(),
                    msg=f"{label} 不应创建数据库文件：{missing_db}",
                )
                siblings = list(Path(self._tmpdir.name).iterdir())
                self.assertFalse(
                    any(p.name.startswith(missing_db.name) for p in siblings),
                    msg=f"{label} 不应留下任何数据库相关文件：{siblings}",
                )

    # ---- 首尾/夹心空白、制表符、CR：同一拒绝结果，不去除空白 ----

    def test_all_whitespace_variants_return_invalid_input(self):
        """标识任意位置含空格、制表符、LF、CR（含夹在数字中间）一律 invalid_input。"""
        resource_id = self.add_resource()
        self.reserve_ok(resource_id)

        for text in WHITESPACE_VARIANTS:
            with self.subTest(text=text):
                self.assert_invalid_input(
                    "reserve", "--resource", text,
                    "--start", f"{DAY}T11:00", "--end", f"{DAY}T12:00",
                )
                self.assert_invalid_input(
                    "day-query", "--resource", text, "--date", DAY
                )
                self.assert_invalid_input("cancel", "--booking", text)

        # 全部失败后原预约仍在。
        self.assertEqual(
            self.day_query(str(resource_id))["bookings"],
            [{"booking_id": 1, "start": START, "end": END}],
        )

    # ---- 校验优先级：先于资源/预约存在与冲突判断 ----

    def test_invalid_input_precedes_existence_and_conflict_checks(self):
        """带空白的标识即便指向不存在的记录、或时段本会冲突，仍只返回 invalid_input。"""
        resource_id = self.add_resource()
        self.reserve_ok(resource_id)

        # 标识指向不存在的资源/预约：仍是 invalid_input，而非各自的 not_found。
        self.assert_invalid_input(
            "day-query", "--resource", "999\n", "--date", DAY
        )
        self.assert_invalid_input(
            "reserve", "--resource", "999\n",
            "--start", f"{DAY}T11:00", "--end", f"{DAY}T12:00",
        )
        self.assert_invalid_input("cancel", "--booking", "999\n")

        # 资源 1 存在且时段与原预约重叠：带 LF 的标识仍先报 invalid_input，
        # 而不是 resource_not_found 或 booking_conflict。
        self.assert_invalid_input(
            "reserve", "--resource", LF, "--start", START, "--end", END
        )

    # ---- 已有数据库：不新增、不取消、不消耗标识 ----

    def test_rejected_ids_change_nothing_and_do_not_consume_ids(self):
        """带空白标识失败前后表内记录逐行一致；随后成功的相邻预约获得紧邻标识。"""
        resource_id = self.add_resource()
        booking_id = self.reserve_ok(resource_id)
        rows_before = self.raw_bookings()
        self.assertEqual(rows_before, [(booking_id, resource_id, START, END, 0)])

        self.assert_invalid_input(
            "reserve", "--resource", LF,
            "--start", f"{DAY}T11:00", "--end", f"{DAY}T12:00",
        )
        self.assert_invalid_input(
            "reserve", "--resource", ZERO_ZERO_ZERO_LF,
            "--start", f"{DAY}T11:00", "--end", f"{DAY}T12:00",
        )
        self.assert_invalid_input("cancel", "--booking", LF)
        self.assert_invalid_input("cancel", "--booking", ZERO_LF)
        self.assert_invalid_input(
            "day-query", "--resource", LF, "--date", DAY
        )

        self.assertEqual(self.raw_bookings(), rows_before)

        # 失败请求没有消耗预约标识：相邻时段成功时获得紧随其后的标识。
        adjacent_id = self.reserve_ok(
            resource_id, f"{DAY}T10:00", f"{DAY}T11:00"
        )
        self.assertEqual(adjacent_id, booking_id + 1)

    # ---- 尚未迁移的旧库：这类请求不得触发迁移 ----

    def test_whitespace_id_requests_do_not_migrate_legacy_database(self):
        """未打开过的旧库上以带空白标识访问：invalid_input，不补齐 cancelled 列，数据原样。"""
        legacy_db = Path(self._tmpdir.name) / "legacy.sqlite"
        create_legacy_db(legacy_db)

        # 前置条件：旧库 bookings 表确实没有 cancelled 列。
        self.assertEqual(
            self.booking_columns(legacy_db),
            ["id", "resource_id", "start", "end"],
        )
        rows_before = self.raw_bookings_without_cancelled(legacy_db)

        self.assert_invalid_input(
            "reserve", "--resource", LF,
            "--start", f"{DAY}T11:00", "--end", f"{DAY}T12:00",
            db=legacy_db,
        )
        self.assert_invalid_input(
            "day-query", "--resource", LF, "--date", DAY, db=legacy_db
        )
        self.assert_invalid_input("cancel", "--booking", LF, db=legacy_db)
        self.assert_invalid_input("cancel", "--booking", ZERO_LF, db=legacy_db)

        # 表结构仍是旧结构，已有数据逐行原样保留。
        self.assertEqual(
            self.booking_columns(legacy_db),
            ["id", "resource_id", "start", "end"],
        )
        self.assertEqual(
            self.raw_bookings_without_cancelled(legacy_db), rows_before
        )
        self.assertEqual(
            self.raw_bookings_without_cancelled(legacy_db),
            [(1, 1, START, END)],
        )

    # ---- 正常标识的数值语义保持不变 ----

    def test_normal_numeric_ids_keep_existing_semantics(self):
        """0001 指向 1；Unicode 十进制数字按数值解释；纯零仍非法；超大值仍按不存在处理。"""
        resource_id = self.add_resource()
        self.assertEqual(resource_id, 1)
        booking_id = self.reserve_ok(resource_id)
        self.assertEqual(booking_id, 1)

        # 前导零不改变数值：0001 与 1 指向同一资源与预约。
        payload = self.day_query("0001")
        self.assertEqual(
            payload["bookings"],
            [{"booking_id": 1, "start": START, "end": END}],
        )
        payload = self.run_ok("cancel", "--booking", "0001")
        self.assertEqual(payload, {"booking_id": 1, "cancelled": True})

        # 可用的 Unicode 十进制数字（U+0663 = 3）按数值 3 解释：
        # 资源 3 不存在，因此是 resource_not_found（而不是 invalid_input）。
        proc = self.run_cli("day-query", "--resource", "٣", "--date", DAY)
        self.assertEqual(proc.returncode, 2, msg=proc.stderr)
        self.assertEqual(json.loads(proc.stdout), {"error": "resource_not_found"})
        proc = self.run_cli(
            "reserve", "--resource", "٣",
            "--start", f"{DAY}T11:00", "--end", f"{DAY}T12:00",
        )
        self.assertEqual(proc.returncode, 2, msg=proc.stderr)
        self.assertEqual(json.loads(proc.stdout), {"error": "resource_not_found"})

        # 无换行的全零文本仍为 invalid_input。
        self.assert_invalid_input(
            "day-query", "--resource", "000", "--date", DAY
        )
        self.assert_invalid_input(
            "reserve", "--resource", "0",
            "--start", f"{DAY}T11:00", "--end", f"{DAY}T12:00",
        )
        self.assert_invalid_input("cancel", "--booking", "0")

        # 大于 2^63-1 的合法正整数仍按不存在的标识处理。
        proc = self.run_cli(
            "day-query", "--resource", OVER_MAX_ID, "--date", DAY
        )
        self.assertEqual(proc.returncode, 2, msg=proc.stderr)
        self.assertEqual(json.loads(proc.stdout), {"error": "resource_not_found"})
        proc = self.run_cli("cancel", "--booking", OVER_MAX_ID)
        self.assertEqual(proc.returncode, 2, msg=proc.stderr)
        self.assertEqual(json.loads(proc.stdout), {"error": "booking_not_found"})


if __name__ == "__main__":
    unittest.main()
