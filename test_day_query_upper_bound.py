"""day-query 日期上界（9999-12-31）的回归测试（仅标准库，python -m unittest 可发现）。

固定已有 day-query 流程在四位年份规则最后一天的公开行为：跨日预约只出现
一次并保留完整端点、当天预约正常返回、按 start 升序；结束于当天 00:00
的预约不属于该日；其他资源与已取消预约不出现；资源存在但无匹配返回空
bookings；未登记正整数标识返回 resource_not_found；10000-01-01 超出四位
年份规则返回 invalid_input（优先于资源存在性检查且不建库）。

通过公开命令行入口 `python -m booking --db <文件> <命令>` 准备数据并断言，
每个用例使用独立的临时 SQLite 数据库，结束后自动清理；不依赖已有数据库、
当前日期、机器时区或任何第三方库。资源与预约标识均取自登记/预约命令的
实际返回；预期端点与预期结果在用例中逐字写明，不借用包内查询函数生成。
断言均基于解析后的 JSON 内容，不依赖输出对象的键顺序；失败信息包含对应
输入、预期结果与实际输出。
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

# 四位年份规则下的最后一天及其前一天（固定 UTC+08:00 的本地日期，与运行环境无关）。
LAST_DAY = "9999-12-31"
PREV_DAY = "9999-12-30"
# 超出四位年份规则的日期：正则只接受 \d{4}，应判 invalid_input。
BEYOND_DATE = "10000-01-01"

# 主样例中逐字固定的端点文本。
CROSS_START = "9999-12-30T23:30"
CROSS_END = "9999-12-31T00:30"
SAME_DAY_START = "9999-12-31T09:00"
SAME_DAY_END = "9999-12-31T10:00"
CANCELLED_START = "9999-12-31T14:00"
CANCELLED_END = "9999-12-31T15:00"
# 恰好结束于最后一天 00:00 的预约端点（左闭右开，不属于最后一天）。
BOUNDARY_START = "9999-12-30T22:00"
BOUNDARY_END = "9999-12-31T00:00"


class DayQueryUpperBoundTestCase(unittest.TestCase):
    """每个用例一个独立的临时目录和数据库路径，tearDown 自动清理。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="booking-test-")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "test.sqlite"

    # ---- 公开命令的调用辅助 ----

    def run_cli(self, *args, db=None):
        """运行一条 booking 命令（独立进程），返回 CompletedProcess。"""
        return subprocess.run(
            [sys.executable, "-m", "booking", "--db", str(db or self.db_path), *args],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def run_prepare_ok(self, *args):
        """运行 resource-add/reserve/cancel 准备命令并断言成功，返回解析后的 JSON。"""
        proc = self.run_cli(*args)
        self.assertEqual(
            proc.returncode,
            0,
            msg=(
                f"准备输入={args}\n预期退出码=0\n实际退出码={proc.returncode}\n"
                f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
            ),
        )
        self.assertEqual(proc.stderr, "", msg=f"准备输入={args}\nstderr={proc.stderr!r}")
        return self._parse_single_json_object(args, proc)

    def _parse_single_json_object(self, args, proc):
        """断言 stdout 恰好包含一个 JSON 对象（允许尾随空白），返回解析结果。"""
        decoder = json.JSONDecoder()
        try:
            payload, end_index = decoder.raw_decode(proc.stdout)
        except json.JSONDecodeError:
            self.fail(
                f"输入={args}\nstdout 不是合法 JSON：{proc.stdout!r}\nstderr={proc.stderr!r}"
            )
        trailing = proc.stdout[end_index:]
        # 允许 print 产生的尾随换行等空白，但不得再有第二个 JSON 或其他内容。
        self.assertEqual(
            trailing.strip(),
            "",
            msg=f"输入={args}\nstdout 在单个 JSON 对象后还有内容：{trailing!r}",
        )
        self.assertIsInstance(payload, dict, msg=f"输入={args}\n实际输出={payload!r}")
        return payload

    def assert_day_query(self, resource_id, date, expected_code, expected_payload, db=None):
        """执行 day-query 并固定退出码、stderr、stdout 单对象与完整 JSON 结果。"""
        args = ("day-query", "--resource", str(resource_id), "--date", date)
        proc = self.run_cli(*args, db=db)

        self.assertEqual(
            proc.returncode,
            expected_code,
            msg=(
                f"输入={args}\n预期退出码={expected_code}\n"
                f"实际退出码={proc.returncode}\nstdout={proc.stdout!r}\nstderr={proc.stderr!r}"
            ),
        )
        # 成功与失败路径都要求标准错误为空（无异常堆栈或其他文本）。
        self.assertEqual(
            proc.stderr,
            "",
            msg=f"输入={args}\n预期 stderr 为空\n实际 stderr={proc.stderr!r}",
        )
        payload = self._parse_single_json_object(args, proc)
        self.assertEqual(
            payload,
            expected_payload,
            msg=(
                f"输入={args}\n预期结果={json.dumps(expected_payload, ensure_ascii=False)}\n"
                f"实际输出={json.dumps(payload, ensure_ascii=False)}"
            ),
        )
        return payload

    def add_resource(self, name):
        payload = self.run_prepare_ok("resource-add", "--name", name)
        self.assertIsInstance(payload.get("resource_id"), int)
        self.assertGreater(payload["resource_id"], 0)
        self.assertEqual(payload.get("name"), name)
        return payload["resource_id"]

    def reserve(self, resource_id, start, end):
        payload = self.run_prepare_ok(
            "reserve",
            "--resource",
            str(resource_id),
            "--start",
            start,
            "--end",
            end,
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
        payload = self.run_prepare_ok("cancel", "--booking", str(booking_id))
        self.assertEqual(payload, {"booking_id": booking_id, "cancelled": True})

    # ---- 成功路径 ----

    def test_last_day_returns_cross_day_once_and_same_day_sorted(self):
        """最后一天：跨日预约只出现一次且端点完整，当天预约正常返回，按 start 升序。

        其他资源同日预约与目标资源已取消的预约均不出现；创建顺序刻意与
        开始时间顺序不同（跨日预约后创建但开始时间更早），防止按标识排序蒙混。
        """
        target_id = self.add_resource("目标资源")
        other_id = self.add_resource("其他资源")

        same_day_id = self.reserve(target_id, SAME_DAY_START, SAME_DAY_END)
        cross_id = self.reserve(target_id, CROSS_START, CROSS_END)
        cancelled_id = self.reserve(target_id, CANCELLED_START, CANCELLED_END)
        # 另一个资源同日 09:00–10:00 的预约不得混入目标资源结果。
        other_booking_id = self.reserve(other_id, SAME_DAY_START, SAME_DAY_END)
        # 目标资源当天一条预约被取消，不得出现。
        self.cancel(cancelled_id)

        expected = {
            "resource_id": target_id,
            "date": LAST_DAY,
            "bookings": [
                {"booking_id": cross_id, "start": CROSS_START, "end": CROSS_END},
                {"booking_id": same_day_id, "start": SAME_DAY_START, "end": SAME_DAY_END},
            ],
        }
        payload = self.assert_day_query(target_id, LAST_DAY, 0, expected)

        # 顶层结构与每项字段固定为既有契约。
        self.assertEqual(set(payload.keys()), {"resource_id", "date", "bookings"})
        self.assertEqual(payload["resource_id"], target_id)
        self.assertEqual(payload["date"], LAST_DAY)
        for booking in payload["bookings"]:
            self.assertEqual(set(booking.keys()), {"booking_id", "start", "end"})

        # 跨日预约只出现一次（标识不重复），并保留完整原始端点，不截断到日界线。
        booking_ids = [booking["booking_id"] for booking in payload["bookings"]]
        self.assertEqual(len(booking_ids), len(set(booking_ids)))
        cross_entries = [b for b in payload["bookings"] if b["booking_id"] == cross_id]
        self.assertEqual(len(cross_entries), 1)
        self.assertEqual(cross_entries[0]["start"], CROSS_START)
        self.assertEqual(cross_entries[0]["end"], CROSS_END)

        # 结果按 start 升序：跨日预约（前一天 23:30）排在当天 09:00 之前。
        starts = [booking["start"] for booking in payload["bookings"]]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(
            starts,
            [CROSS_START, SAME_DAY_START],
        )
        # 已取消预约与其他资源预约标识均不在结果中。
        self.assertNotIn(cancelled_id, booking_ids)
        self.assertNotIn(other_booking_id, booking_ids)

    def test_booking_ending_exactly_at_last_midnight_is_excluded(self):
        """独立样例：结束于 9999-12-31T00:00 的预约与最后一天只有端点接触，不属于该日。"""
        resource_id = self.add_resource("边界资源")
        self.reserve(resource_id, BOUNDARY_START, BOUNDARY_END)

        self.assert_day_query(
            resource_id,
            LAST_DAY,
            0,
            {"resource_id": resource_id, "date": LAST_DAY, "bookings": []},
        )

    def test_existing_resource_without_matching_bookings_returns_empty_list(self):
        """资源存在但最后一天没有任何匹配预约：返回空 bookings，退出码 0。"""
        resource_id = self.add_resource("无匹配资源")
        # 只有前一天白天的预约，与最后一天无交集。
        self.reserve(resource_id, f"{PREV_DAY}T09:00", f"{PREV_DAY}T10:00")

        self.assert_day_query(
            resource_id,
            LAST_DAY,
            0,
            {"resource_id": resource_id, "date": LAST_DAY, "bookings": []},
        )

    def test_repeated_queries_stable_across_processes_and_read_only(self):
        """重复查询与独立进程重开同一数据库结果一致，查询不改变已有预约、不消耗标识。"""
        resource_id = self.add_resource("只读验证资源")
        cross_id = self.reserve(resource_id, CROSS_START, CROSS_END)
        same_day_id = self.reserve(resource_id, SAME_DAY_START, SAME_DAY_END)
        cancelled_id = self.reserve(resource_id, CANCELLED_START, CANCELLED_END)
        self.cancel(cancelled_id)

        expected = {
            "resource_id": resource_id,
            "date": LAST_DAY,
            "bookings": [
                {"booking_id": cross_id, "start": CROSS_START, "end": CROSS_END},
                {"booking_id": same_day_id, "start": SAME_DAY_START, "end": SAME_DAY_END},
            ],
        }

        # 每条命令都是独立 Python 进程：三次调用即同进程重复 + 重开读取。
        first = self.assert_day_query(resource_id, LAST_DAY, 0, expected)
        second = self.assert_day_query(resource_id, LAST_DAY, 0, expected)
        reopened = self.assert_day_query(resource_id, LAST_DAY, 0, expected)
        self.assertEqual(first, second)
        self.assertEqual(second, reopened)

        # 查询为只读：之后端点相接的新预约获得紧接原有最大标识的标识，
        # 说明查询没有消耗任何预约标识；原有两条预约的端点也原样保留。
        adjacent_id = self.reserve(resource_id, SAME_DAY_END, "9999-12-31T11:00")
        self.assertEqual(adjacent_id, cancelled_id + 1)
        self.assert_day_query(
            resource_id,
            LAST_DAY,
            0,
            {
                "resource_id": resource_id,
                "date": LAST_DAY,
                "bookings": [
                    {"booking_id": cross_id, "start": CROSS_START, "end": CROSS_END},
                    {"booking_id": same_day_id, "start": SAME_DAY_START, "end": SAME_DAY_END},
                    {"booking_id": adjacent_id, "start": SAME_DAY_END, "end": "9999-12-31T11:00"},
                ],
            },
        )

    # ---- 失败路径 ----

    def test_unregistered_positive_id_returns_resource_not_found(self):
        """未登记的正整数资源标识查询有效日期：resource_not_found，退出码 2、stderr 为空。"""
        registered_id = self.add_resource("已登记资源")
        unregistered_id = registered_id + 100
        self.assert_day_query(
            unregistered_id,
            LAST_DAY,
            2,
            {"error": "resource_not_found"},
        )

    def test_five_digit_year_returns_invalid_input_even_when_resource_missing(self):
        """10000-01-01 超出四位年份规则：invalid_input，优先于资源不存在的判定。"""
        registered_id = self.add_resource("已登记资源")
        # 资源 999 明确不存在，但日期非法时必须先报 invalid_input。
        self.assertNotEqual(registered_id, 999)
        self.assert_day_query(
            999,
            BEYOND_DATE,
            2,
            {"error": "invalid_input"},
        )

    def test_invalid_year_does_not_create_database_file(self):
        """非法日期指向尚不存在的数据库时，不得创建任何文件。"""
        missing_db = Path(self._tmpdir.name) / "should-not-exist.sqlite"
        self.assertFalse(missing_db.exists())

        self.assert_day_query(
            1,
            BEYOND_DATE,
            2,
            {"error": "invalid_input"},
            db=missing_db,
        )
        self.assertFalse(
            missing_db.exists(),
            msg=f"非法日期查询不应创建数据库文件，但发现 {missing_db}",
        )
        # SQLite 可能使用的 WAL/sidecar 文件同样不应出现。
        self.assertEqual(
            list(Path(self._tmpdir.name).iterdir()),
            [],
            msg="非法日期查询在临时目录中留下了文件",
        )


if __name__ == "__main__":
    unittest.main()
