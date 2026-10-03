"""超出 SQLite INTEGER 范围的超大正整数标识的回归测试。

通过公开命令行入口 `python -m booking --db <文件> <命令>` 准备数据并断言，
每个用例使用独立的临时 SQLite 数据库，结束后自动清理；
不依赖已有数据库、当前日期、机器时区或任何第三方库。
断言均基于解析后的 JSON 内容，不依赖输出对象的键顺序。

覆盖的公开行为：
- reserve/day-query 的 --resource、cancel 的 --booking 只要按现有规则
  表示正整数，即使数值大于 9223372036854775807（2^63-1），也视为不存在的
  标识：reserve/day-query 返回 {"error": "resource_not_found"}，
  cancel 返回 {"error": "booking_not_found"}，退出码均为 2；
- 结果只取决于十进制数值，与位数无关：连续五千个数字 9 同样按不存在处理；
- 前导零不改变数值含义：0001 指向标识 1，0009223372036854775808 仍不存在；
- 校验优先级不变：超大标识与非法日期/时间、起止顺序错误同时出现时，
  一律先返回 {"error": "invalid_input"}；
- 缺失参数、零、负数、小数、非整数文本继续返回 invalid_input；
- 失败请求不新增记录、不消耗资源或预约标识，原预约继续参与冲突判断且可取消，
  失败前后 day-query 的标识、起止文本、数量与顺序完全一致；
- 标准输出只含单个 JSON 对象，标准错误不出现异常堆栈；
- 非法输入指向尚不存在的数据库路径时不创建文件；越界标识本身是合法输入，
  与普通未知标识一致，仍会正常打开（必要时初始化）数据库；
- 边界值 2^63-1 仍按普通正整数交给数据库查询，行为与普通未知标识一致。
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

# SQLite INTEGER 主键边界：2^63-1 是仍可存储的最大有符号整数，2^63 起越界。
MAX_ID = "9223372036854775807"
OVER_MAX_ID = "9223372036854775808"
# 极端长度：连续五千个数字 9，远超过 Python 对整数字符串转换的位数限制。
FIVE_THOUSAND_NINES = "9" * 5000


class OversizedIdTestCase(unittest.TestCase):
    """每个用例一个独立的临时目录和数据库路径，tearDown 自动清理。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="booking-test-oversized-")
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

    def run_error(self, expected_error, *args, db=None):
        """断言退出码 2、stdout 恰为指定错误对象、stderr 无任何异常输出。"""
        proc = self.run_cli(*args, db=db)
        self.assertEqual(
            proc.returncode, 2,
            msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}",
        )
        payload = self.assert_single_json_object(proc)
        self.assertEqual(payload, {"error": expected_error})
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
        return self.run_ok("day-query", "--resource", resource_text, "--date", date)

    # ---- 超大正整数：一律视为不存在的标识 ----

    def test_reserve_and_day_query_oversized_resource_id_return_resource_not_found(self):
        """2^63 作为资源标识：reserve/day-query 返回 resource_not_found，退出码 2。"""
        resource_id = self.add_resource()
        booking_id = self.reserve_ok(resource_id)

        self.run_error(
            "resource_not_found",
            "day-query", "--resource", OVER_MAX_ID, "--date", DAY,
        )
        self.run_error(
            "resource_not_found",
            "reserve", "--resource", OVER_MAX_ID,
            "--start", f"{DAY}T11:00", "--end", f"{DAY}T12:00",
        )

        # 已有资源与预约不受影响。
        self.assertEqual(
            self.day_query(str(resource_id))["bookings"],
            [{"booking_id": booking_id, "start": START, "end": END}],
        )

    def test_cancel_oversized_booking_id_returns_booking_not_found(self):
        """2^63 作为预约标识：cancel 返回 booking_not_found，退出码 2。"""
        resource_id = self.add_resource()
        booking_id = self.reserve_ok(resource_id)

        self.run_error("booking_not_found", "cancel", "--booking", OVER_MAX_ID)

        # 已有预约未被误伤，仍可正常取消。
        self.assertEqual(
            self.day_query(str(resource_id))["bookings"],
            [{"booking_id": booking_id, "start": START, "end": END}],
        )
        payload = self.run_ok("cancel", "--booking", str(booking_id))
        self.assertEqual(payload, {"booking_id": booking_id, "cancelled": True})

    def test_five_thousand_nines_are_independent_of_digit_count(self):
        """连续五千个数字 9 仍按不存在的标识处理，不依赖十进制位数。"""
        resource_id = self.add_resource()
        booking_id = self.reserve_ok(resource_id)

        self.run_error(
            "resource_not_found",
            "day-query", "--resource", FIVE_THOUSAND_NINES, "--date", DAY,
        )
        self.run_error(
            "resource_not_found",
            "reserve", "--resource", FIVE_THOUSAND_NINES,
            "--start", f"{DAY}T11:00", "--end", f"{DAY}T12:00",
        )
        self.run_error(
            "booking_not_found", "cancel", "--booking", FIVE_THOUSAND_NINES
        )

        self.assertEqual(
            self.day_query(str(resource_id))["bookings"],
            [{"booking_id": booking_id, "start": START, "end": END}],
        )

    # ---- 前导零：按数值解释 ----

    def test_leading_zeros_keep_numeric_value(self):
        """0001 指向标识 1：day-query 查到资源、reserve 落在资源 1、cancel 取消预约 1。"""
        resource_id = self.add_resource()
        self.assertEqual(resource_id, 1)
        booking_id = self.reserve_ok(resource_id)
        self.assertEqual(booking_id, 1)

        payload = self.day_query("0001")
        self.assertEqual(payload["resource_id"], 1)
        self.assertEqual(
            payload["bookings"],
            [{"booking_id": 1, "start": START, "end": END}],
        )

        # 0001 预约资源 1 的另一时段成功，返回的 resource_id 为数值 1。
        payload = self.run_ok(
            "reserve", "--resource", "0001",
            "--start", f"{DAY}T11:00", "--end", f"{DAY}T12:00",
        )
        self.assertEqual(payload["resource_id"], 1)

        # 0001 取消预约 1 成功，返回的 booking_id 为数值 1。
        payload = self.run_ok("cancel", "--booking", "0001")
        self.assertEqual(payload, {"booking_id": 1, "cancelled": True})

    def test_leading_zeros_do_not_save_oversized_id(self):
        """0009223372036854775808 等带前导零的越界值仍视为不存在。"""
        self.add_resource()
        self.reserve_ok(1)

        self.run_error(
            "resource_not_found",
            "day-query", "--resource", "000" + OVER_MAX_ID, "--date", DAY,
        )
        self.run_error(
            "resource_not_found",
            "reserve", "--resource", "000" + OVER_MAX_ID,
            "--start", f"{DAY}T11:00", "--end", f"{DAY}T12:00",
        )
        self.run_error(
            "booking_not_found", "cancel", "--booking", "000" + OVER_MAX_ID
        )
        # 五千个前导零加一个越界值，同样按越界处理。
        self.run_error(
            "resource_not_found",
            "day-query",
            "--resource", ("0" * 5000) + OVER_MAX_ID,
            "--date", DAY,
        )

    # ---- 边界值 2^63-1：普通未知标识，正常走数据库查询 ----

    def test_max_storable_id_is_treated_as_normal_integer(self):
        """2^63-1 可正常绑定给 SQLite：资源/预约不存在时走普通 not_found 分支。"""
        resource_id = self.add_resource()
        booking_id = self.reserve_ok(resource_id)

        self.run_error(
            "resource_not_found",
            "day-query", "--resource", MAX_ID, "--date", DAY,
        )
        self.run_error(
            "resource_not_found",
            "reserve", "--resource", MAX_ID,
            "--start", f"{DAY}T11:00", "--end", f"{DAY}T12:00",
        )
        self.run_error("booking_not_found", "cancel", "--booking", MAX_ID)

        self.assertEqual(
            self.day_query(str(resource_id))["bookings"],
            [{"booking_id": booking_id, "start": START, "end": END}],
        )

    # ---- 校验优先级：invalid_input 先于 not_found ----

    def test_invalid_input_takes_priority_for_day_query(self):
        """超大资源标识与非法日期同时出现：先返回 invalid_input。"""
        bad_dates = ["2026-02-30", "2026-10-5", "2026-13-01", "not-a-date"]
        for bad_date in bad_dates:
            with self.subTest(bad_date=bad_date):
                self.run_error(
                    "invalid_input",
                    "day-query", "--resource", OVER_MAX_ID, "--date", bad_date,
                )
                self.run_error(
                    "invalid_input",
                    "day-query", "--resource", FIVE_THOUSAND_NINES, "--date", bad_date,
                )

    def test_invalid_input_takes_priority_for_reserve(self):
        """超大资源标识与非法时间或起止顺序错误同时出现：先返回 invalid_input。"""
        cases = [
            ("开始日期不存在", f"2026-02-30T11:00", f"{DAY}T12:00"),
            ("结束分钟越界", f"{DAY}T11:00", f"{DAY}T12:60"),
            ("开始时间格式不符", f"{DAY} 11:00", f"{DAY}T12:00"),
            ("开始等于结束", f"{DAY}T11:00", f"{DAY}T11:00"),
            ("开始晚于结束", f"{DAY}T12:00", f"{DAY}T11:00"),
        ]
        for label, start, end in cases:
            with self.subTest(case=label):
                self.run_error(
                    "invalid_input",
                    "reserve", "--resource", OVER_MAX_ID,
                    "--start", start, "--end", end,
                )
                self.run_error(
                    "invalid_input",
                    "reserve", "--resource", FIVE_THOUSAND_NINES,
                    "--start", start, "--end", end,
                )

    def test_missing_and_non_positive_ids_still_return_invalid_input(self):
        """缺失参数、零、负数、小数、非整数文本继续返回 invalid_input。"""
        cases = [
            ("day-query 缺 --resource", ["day-query", "--date", DAY]),
            ("day-query 资源为零", ["day-query", "--resource", "0", "--date", DAY]),
            ("day-query 资源为负数", ["day-query", "--resource", "-1", "--date", DAY]),
            ("day-query 资源为小数", ["day-query", "--resource", "1.5", "--date", DAY]),
            ("day-query 资源为文本", ["day-query", "--resource", "abc", "--date", DAY]),
            ("reserve 资源为零",
             ["reserve", "--resource", "0", "--start", START, "--end", END]),
            ("reserve 资源为负数",
             ["reserve", "--resource", "-9", "--start", START, "--end", END]),
            ("reserve 资源为小数",
             ["reserve", "--resource", "2.0", "--start", START, "--end", END]),
            ("reserve 缺 --end",
             ["reserve", "--resource", OVER_MAX_ID, "--start", START]),
            ("cancel 缺 --booking", ["cancel"]),
            ("cancel 预约为零", ["cancel", "--booking", "0"]),
            ("cancel 预约为负数", ["cancel", "--booking", "-1"]),
            ("cancel 预约为小数", ["cancel", "--booking", "1.0"]),
            ("cancel 预约为文本", ["cancel", "--booking", "xyz"]),
            ("cancel 预约含空白", ["cancel", "--booking", " 1"]),
        ]
        for label, args in cases:
            with self.subTest(case=label):
                self.run_error("invalid_input", *args)

    # ---- 失败无副作用：记录、冲突、标识序列、查询结果均不变 ----

    def test_oversized_failures_leave_state_completely_unchanged(self):
        """越界失败前后 day-query 完全一致；原预约仍冲突且可取消；不消耗预约标识。"""
        resource_id = self.add_resource()
        booking_id = self.reserve_ok(resource_id)
        before = self.day_query(str(resource_id))

        # 各类越界失败均不得写入任何记录。
        self.run_error(
            "resource_not_found",
            "day-query", "--resource", OVER_MAX_ID, "--date", DAY,
        )
        self.run_error(
            "resource_not_found",
            "reserve", "--resource", OVER_MAX_ID,
            "--start", f"{DAY}T11:00", "--end", f"{DAY}T12:00",
        )
        self.run_error(
            "resource_not_found",
            "reserve", "--resource", FIVE_THOUSAND_NINES,
            "--start", f"{DAY}T11:00", "--end", f"{DAY}T12:00",
        )
        self.run_error("booking_not_found", "cancel", "--booking", OVER_MAX_ID)
        self.run_error(
            "booking_not_found", "cancel", "--booking", FIVE_THOUSAND_NINES
        )

        # 查询结果中的标识、起止文本、数量与顺序逐字节保持一致。
        after = self.day_query(str(resource_id))
        self.assertEqual(after, before)
        self.assertEqual(
            after["bookings"],
            [{"booking_id": booking_id, "start": START, "end": END}],
        )

        # 原预约仍参与冲突判断。
        self.run_error(
            "booking_conflict",
            "reserve", "--resource", str(resource_id),
            "--start", START, "--end", END,
        )

        # 越界失败没有消耗预约标识：相邻时段成功时获得紧随其后的标识。
        adjacent_id = self.reserve_ok(
            resource_id, f"{DAY}T10:00", f"{DAY}T11:00"
        )
        self.assertEqual(adjacent_id, booking_id + 1)

        # 原预约仍可正常取消，取消后时段释放。
        payload = self.run_ok("cancel", "--booking", str(booking_id))
        self.assertEqual(payload, {"booking_id": booking_id, "cancelled": True})
        self.assertEqual(
            self.day_query(str(resource_id))["bookings"],
            [{"booking_id": adjacent_id, "start": f"{DAY}T10:00", "end": f"{DAY}T11:00"}],
        )

    # ---- 数据库文件：非法输入不创建；越界合法输入照常初始化/迁移 ----

    def test_invalid_input_with_oversized_id_does_not_create_database(self):
        """非法输入（即使同时带越界标识）指向不存在的路径时，不创建文件。"""
        missing_db = Path(self._tmpdir.name) / "missing-invalid.sqlite"
        self.run_error(
            "invalid_input",
            "day-query", "--resource", OVER_MAX_ID, "--date", "2026-02-30",
            db=missing_db,
        )
        self.assertFalse(missing_db.exists())
        self.run_error(
            "invalid_input",
            "reserve", "--resource", FIVE_THOUSAND_NINES,
            "--start", f"{DAY}T12:00", "--end", f"{DAY}T11:00",
            db=missing_db,
        )
        self.assertFalse(missing_db.exists())

    def test_oversized_id_on_missing_path_initializes_database_like_unknown_id(self):
        """越界标识是合法输入：指向不存在的路径时与普通未知标识一样完成初始化。"""
        missing_db = Path(self._tmpdir.name) / "missing-oversized.sqlite"

        # 普通未知标识会初始化文件；越界标识行为应与之相同。
        self.run_error(
            "resource_not_found",
            "day-query", "--resource", OVER_MAX_ID, "--date", DAY,
            db=missing_db,
        )
        self.assertTrue(missing_db.is_file())

        # 初始化后的库可正常使用：登记资源、预约、查询均工作。
        proc = self.run_cli(
            "resource-add", "--name", "新资源", db=missing_db
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        new_id = json.loads(proc.stdout)["resource_id"]
        self.assertEqual(new_id, 1)

    def test_oversized_id_opens_and_migrates_legacy_database(self):
        """越界标识打开旧库时与普通未知标识一致：首次访问即补齐 cancelled 列。"""
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

        # 以越界标识访问：resource_not_found，但旧库已被正常打开并迁移。
        self.run_error(
            "resource_not_found",
            "day-query", "--resource", OVER_MAX_ID, "--date", DAY,
            db=legacy_db,
        )
        conn = sqlite3.connect(str(legacy_db))
        try:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(bookings)")]
        finally:
            conn.close()
        self.assertEqual(columns, ["id", "resource_id", "start", "end", "cancelled"])

        # 迁移后库可正常使用，原预约仍有效并参与冲突判断。
        self.run_error(
            "booking_conflict",
            "reserve", "--resource", "1", "--start", START, "--end", END,
            db=legacy_db,
        )


if __name__ == "__main__":
    unittest.main()
