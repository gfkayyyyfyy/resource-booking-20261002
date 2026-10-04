"""reserve 时间输入校验的回归测试（仅标准库，python -m unittest 可发现、可单独执行）。

通过公开命令行入口 `python -m booking --db <文件> <命令>` 准备资源、提交预约并
读取结果，每个用例使用独立的临时 SQLite 数据库，结束后自动清理；不依赖已有数据库、
当前日期、机器时区或任何第三方库。断言均基于解析后的 JSON 内容，不依赖输出对象的
键顺序，也不预先猜测 resource_id / booking_id 的具体数值。

覆盖的公开行为（固定 UTC+08:00，时间文本一律为零填充 YYYY-MM-DDTHH:mm）：
- 成功样例 2026-10-05T09:00–10:00 与闰年 2028-02-29T23:30–2028-03-01T00:30
  各自返回正整数 booking_id、正确的 resource_id 与原始起止文本，退出码 0，
  按日查询（跨午夜样例查询两个日期）能读回完整时段；
- 未零填充、日期与时间之间用空格、带秒、带 Z 或 +08:00 后缀、首尾空白、
  2026-02-29 这样的无效日期、小时 24、分钟 60 一律拒绝；开始与结束参数
  分别受到同样检查；开始等于结束或晚于结束同样拒绝；
- 所有拒绝请求只在标准输出返回 {"error": "invalid_input"}，退出码 2，
  标准错误为空；
- 非法时间搭配不存在的正整数资源标识仍返回 invalid_input（校验先于资源存在判断）；
  在已有预约上提交带秒的相同时段也返回 invalid_input（校验先于冲突判断）；
- 拒绝请求不留下任何数据变化：指向尚不存在的数据库路径时文件不被创建；
  在已有资源和预约的库上连续提交拒绝样例后，资源目录与按日查询的解析结果
  与提交前完全一致；随后提交合法的相邻时段成功，booking_id 紧接原有最大
  预约标识，证明失败没有消耗 AUTOINCREMENT 标识。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# 项目根目录（booking 包所在目录），子进程以此作为工作目录，
# 保证 `python -m booking` 与测试运行时的当前目录无关。
PROJECT_ROOT = Path(__file__).resolve().parent

# 一组合法的对照起止文本。
VALID_START = "2026-10-05T09:00"
VALID_END = "2026-10-05T10:00"
DAY = "2026-10-05"

# 成功样例：(标签, start, end, 应能读回该时段的查询日期列表, 子进程 TZ)。
# 两个样例分别在不同的机器时区下运行，固定 UTC+08:00 的解释不应随 TZ 漂移。
VALID_SAMPLES = [
    (
        "普通时段",
        "2026-10-05T09:00",
        "2026-10-05T10:00",
        ["2026-10-05"],
        "America/Los_Angeles",
    ),
    (
        "闰年 2028-02-29 跨午夜到 03-01",
        "2028-02-29T23:30",
        "2028-03-01T00:30",
        ["2028-02-29", "2028-03-01"],
        "UTC",
    ),
]

# 非法时间文本：(说明, 文本)。
MALFORMED_TIMES = [
    ("日期未零填充", "2026-10-5T09:00"),
    ("小时未零填充", "2026-10-05T9:00"),
    ("分钟未零填充", "2026-10-05T09:0"),
    ("日期与时间之间使用空格", "2026-10-05 09:00"),
    ("带秒", "2026-10-05T09:00:00"),
    ("带 Z 后缀", "2026-10-05T09:00Z"),
    ("带 +08:00 时区后缀", "2026-10-05T09:00+08:00"),
    ("前导空格", " 2026-10-05T09:00"),
    ("尾随空格", "2026-10-05T09:00 "),
    ("前导制表符", "\t2026-10-05T09:00"),
    ("2026 年不是闰年，02-29 不存在", "2026-02-29T09:00"),
    ("小时为 24", "2026-10-05T24:00"),
    ("分钟为 60", "2026-10-05T09:60"),
]


class ReserveTimeValidationTestCase(unittest.TestCase):
    """每个用例一个独立的临时目录和数据库路径，tearDown 自动清理。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="booking-test-")
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "test.sqlite"

    def db_path_for(self, name):
        """同一临时目录内再取一个独立数据库路径（供需要彼此隔离的子用例）。"""
        return Path(self._tmpdir.name) / name

    # ---- 公开命令的调用辅助 ----

    def run_cli(self, *args, db=None, env=None):
        """运行一条 booking 命令，返回 CompletedProcess；env 可覆盖子进程环境变量。"""
        proc_env = None if env is None else {**os.environ, **env}
        return subprocess.run(
            [sys.executable, "-m", "booking", "--db", str(db or self.db_path), *args],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            env=proc_env,
        )

    def run_ok(self, *args, db=None, env=None):
        """运行命令并断言退出码 0、标准输出为单个 JSON 对象，返回解析结果。"""
        proc = self.run_cli(*args, db=db, env=env)
        self.assertEqual(
            proc.returncode, 0,
            msg=f"命令 {args!r} 预期退出码 0，实际 {proc.returncode}；"
                f"stdout={proc.stdout!r} stderr={proc.stderr!r}",
        )
        payload = json.loads(proc.stdout)  # 若输出不是单个 JSON 文档会抛错
        self.assertIsInstance(payload, dict)
        return payload

    def add_resource(self, name="测试资源", db=None, env=None):
        payload = self.run_ok("resource-add", "--name", name, db=db, env=env)
        self.assertIsInstance(payload["resource_id"], int)
        self.assertGreater(payload["resource_id"], 0)
        self.assertEqual(payload["name"], name)
        return payload["resource_id"]

    def reserve_ok(self, resource_id, start, end, db=None, env=None):
        """提交预约并断言成功：退出码 0、stderr 为空，返回 booking_id。

        成功对象必须恰好包含 booking_id（正整数）、正确的 resource_id 与
        原始起止文本；字典相等性不依赖键序。
        """
        proc = self.run_cli(
            "reserve", "--resource", str(resource_id),
            "--start", start, "--end", end, db=db, env=env,
        )
        slot = f"start={start!r}, end={end!r}"
        self.assertEqual(
            proc.returncode, 0,
            msg=f"输入 {slot} 预期成功（退出码 0），实际 rc={proc.returncode}；"
                f"stdout={proc.stdout!r} stderr={proc.stderr!r}",
        )
        self.assertEqual(
            proc.stderr, "",
            msg=f"输入 {slot} 成功时 stderr 应为空；实际 {proc.stderr!r}",
        )
        payload = json.loads(proc.stdout)
        self.assertIsInstance(payload, dict)
        self.assertIsInstance(
            payload.get("booking_id"), int,
            msg=f"输入 {slot} 预期 booking_id 为整数；实际返回 {payload!r}",
        )
        self.assertGreater(
            payload["booking_id"], 0,
            msg=f"输入 {slot} 预期 booking_id 为正整数；实际 {payload!r}",
        )
        self.assertEqual(
            payload,
            {
                "booking_id": payload["booking_id"],
                "resource_id": resource_id,
                "start": start,
                "end": end,
            },
            msg=f"输入 {slot} 的成功返回内容与公开约定不符："
                f"预期包含 resource_id={resource_id!r} 与原始起止文本，实际 {payload!r}",
        )
        return payload["booking_id"]

    def assert_reserve_invalid_input(self, resource, start, end, db=None):
        """断言 reserve 请求只返回 {"error": "invalid_input"}：rc=2、stderr 为空。

        失败信息包含输入值、实际结果与预期结果。
        """
        proc = self.run_cli(
            "reserve", "--resource", str(resource),
            "--start", start, "--end", end, db=db,
        )
        inputs = f"--resource={resource!r} --start={start!r} --end={end!r}"
        self.assertEqual(
            proc.returncode, 2,
            msg=f"输入 {inputs} 预期退出码 2，实际 {proc.returncode}；"
                f"stdout={proc.stdout!r} stderr={proc.stderr!r}",
        )
        self.assertEqual(
            proc.stderr, "",
            msg=f"输入 {inputs} 被拒时 stderr 预期为空字符串，实际 {proc.stderr!r}",
        )
        try:
            payload = json.loads(proc.stdout)
        except ValueError:
            self.fail(
                f"输入 {inputs} 预期 stdout 为单个 JSON 对象 "
                f"{{'error': 'invalid_input'}}，实际 stdout={proc.stdout!r}"
            )
        self.assertEqual(
            payload, {"error": "invalid_input"},
            msg=f"输入 {inputs} 预期返回 {{'error': 'invalid_input'}}，"
                f"实际返回 {payload!r}",
        )

    def day_query(self, resource_id, date, db=None, env=None):
        return self.run_ok(
            "day-query", "--resource", str(resource_id), "--date", date,
            db=db, env=env,
        )

    # ---- 成功样例：接受范围 ----

    def test_valid_samples_succeed_and_read_back_by_day_query(self):
        """两个成功样例返回正整数标识与原始起止文本，按日查询读回完整时段。"""
        for index, (label, start, end, dates, tz) in enumerate(VALID_SAMPLES):
            with self.subTest(sample=label, start=start, end=end):
                # 每个样例使用彼此隔离的数据库文件，并在不同的 TZ 下运行。
                db = self.db_path_for(f"valid-{index}.sqlite")
                env = {"TZ": tz}
                resource_id = self.add_resource(name=f"资源-{label}", db=db, env=env)
                booking_id = self.reserve_ok(
                    resource_id, start, end, db=db, env=env
                )

                for date in dates:
                    payload = self.day_query(
                        resource_id, date, db=db, env=env
                    )
                    # 跨午夜时段在涉及的每个日期都完整、不截断地出现一次。
                    self.assertEqual(
                        payload,
                        {
                            "resource_id": resource_id,
                            "date": date,
                            "bookings": [
                                {"booking_id": booking_id, "start": start, "end": end}
                            ],
                        },
                        msg=f"样例 {label}（{start}~{end}）按日查询 {date} 的结果不符；"
                            f"实际 {payload!r}",
                    )

    # ---- 拒绝：格式与取值非法，开始/结束分别检查 ----

    def test_malformed_time_texts_rejected_for_start_and_end(self):
        """每类非法时间文本分别作为开始、作为结束提交，均为 invalid_input。"""
        resource_id = self.add_resource()
        for label, bad in MALFORMED_TIMES:
            for position in ("start", "end"):
                with self.subTest(label=label, position=position, value=bad):
                    if position == "start":
                        self.assert_reserve_invalid_input(
                            resource_id, bad, VALID_END
                        )
                    else:
                        self.assert_reserve_invalid_input(
                            resource_id, VALID_START, bad
                        )

    def test_start_equal_or_after_end_is_rejected(self):
        """开始等于结束、开始晚于结束（含跨日逆序）均为 invalid_input。"""
        resource_id = self.add_resource()
        cases = [
            ("开始等于结束", "2026-10-05T09:00", "2026-10-05T09:00"),
            ("开始晚于结束（同一天）", "2026-10-05T10:00", "2026-10-05T09:00"),
            (
                "开始晚于结束（跨午夜逆序）",
                "2028-03-01T00:30",
                "2028-02-29T23:30",
            ),
        ]
        for label, start, end in cases:
            with self.subTest(label=label, start=start, end=end):
                self.assert_reserve_invalid_input(resource_id, start, end)

    # ---- 拒绝优先级：先于资源存在与冲突判断 ----

    def test_invalid_time_takes_priority_over_nonexistent_resource(self):
        """非法时间搭配不存在的正整数资源标识，仍只返回 invalid_input。"""
        cases = [
            ("开始结束都带秒", "2026-10-05T09:00:00", "2026-10-05T10:00:00"),
            ("带 Z 后缀", "2026-10-05T09:00Z", "2026-10-05T10:00Z"),
            ("无效日期 2026-02-29", "2026-02-29T09:00", "2026-02-29T10:00"),
            ("小时为 24", "2026-10-05T24:00", "2026-10-05T25:00"),
        ]
        for label, start, end in cases:
            with self.subTest(label=label):
                # 资源 999 为正整数但在该库中不存在；不应返回 resource_not_found。
                self.assert_reserve_invalid_input(999, start, end)

    def test_invalid_time_takes_priority_over_booking_conflict(self):
        """已有预约上用带秒的相同时段请求，返回 invalid_input 而非冲突。"""
        resource_id = self.add_resource()
        booking_id = self.reserve_ok(
            resource_id, "2026-10-05T09:00", "2026-10-05T10:00"
        )

        self.assert_reserve_invalid_input(
            resource_id, "2026-10-05T09:00:00", "2026-10-05T10:00:00"
        )

        # 未进入冲突分支，已有预约原样保留。
        payload = self.day_query(resource_id, DAY)
        self.assertEqual(
            payload["bookings"],
            [{"booking_id": booking_id,
              "start": "2026-10-05T09:00", "end": "2026-10-05T10:00"}],
            msg=f"非法的相同时段请求后按日查询结果发生变化：{payload!r}",
        )

    # ---- 拒绝不留下数据变化：不创建文件 ----

    def test_invalid_requests_never_create_database_file(self):
        """对尚不存在的数据库路径提交非法时间，文件（及任何旁路文件）都不出现。"""
        missing_db = self.db_path_for("should-not-exist.sqlite")
        cases = [
            ("开始未零填充", "2026-10-5T09:00", VALID_END),
            ("结束带秒", VALID_START, "2026-10-05T10:00:00"),
            ("开始等于结束", VALID_START, VALID_START),
            ("开始晚于结束", VALID_END, VALID_START),
            ("无效日期 2026-02-29", "2026-02-29T09:00", VALID_END),
            ("小时 24", "2026-10-05T24:00", VALID_END),
        ]
        for label, start, end in cases:
            with self.subTest(label=label, start=start, end=end):
                self.assert_reserve_invalid_input(1, start, end, db=missing_db)
                self.assertFalse(
                    missing_db.exists(),
                    msg=f"非法请求（{label}）不应创建数据库文件 {missing_db}，"
                        f"但文件已存在",
                )
                leftovers = list(Path(self._tmpdir.name).iterdir())
                self.assertEqual(
                    leftovers, [],
                    msg=f"非法请求（{label}）在临时目录留下了文件："
                        f"{[str(p) for p in leftovers]}",
                )

    # ---- 拒绝不留下数据变化：已有库内容与标识不变 ----

    def test_rejections_leave_state_unchanged_and_do_not_consume_booking_id(self):
        """连续拒绝后资源目录与按日查询不变；随后合法相邻时段获得紧接的标识。"""
        resource_a = self.add_resource("一号资源")
        resource_b = self.add_resource("二号资源")
        seed_id = self.reserve_ok(
            resource_a, "2026-10-05T09:00", "2026-10-05T10:00"
        )

        # 提交拒绝样例前的公开状态快照（解析后的 JSON 内容）。
        catalog_before = self.run_ok("resource-list")
        day_a_before = self.day_query(resource_a, DAY)
        day_b_before = self.day_query(resource_b, DAY)
        self.assertEqual(day_a_before["bookings"], [
            {"booking_id": seed_id,
             "start": "2026-10-05T09:00", "end": "2026-10-05T10:00"},
        ])
        self.assertEqual(day_b_before["bookings"], [])

        # 连续提交各类拒绝样例：格式非法（开始/结束两个位置）、顺序非法、
        # 对已有预约的带秒相同时段（本会冲突）、不存在资源搭配非法时间。
        for label, bad in MALFORMED_TIMES:
            self.assert_reserve_invalid_input(resource_a, bad, VALID_END)
        self.assert_reserve_invalid_input(
            resource_a, VALID_START, "2026-10-05T10:00:00"
        )  # 结束带秒
        self.assert_reserve_invalid_input(
            resource_a, "2026-10-05T09:00", "2026-10-05T09:00"
        )  # 开始等于结束
        self.assert_reserve_invalid_input(
            resource_a, "2026-10-05T10:00", "2026-10-05T09:00"
        )  # 开始晚于结束
        self.assert_reserve_invalid_input(
            resource_a, "2026-10-05T09:00:00", "2026-10-05T10:00:00"
        )  # 已有预约上带秒的相同时段
        self.assert_reserve_invalid_input(
            999, "2026-10-05T09:00Z", "2026-10-05T10:00Z"
        )  # 不存在的正整数资源

        # 资源目录与两个资源的按日查询解析结果与提交前完全一致。
        self.assertEqual(
            self.run_ok("resource-list"), catalog_before,
            msg="连续拒绝后资源目录发生变化",
        )
        self.assertEqual(
            self.day_query(resource_a, DAY), day_a_before,
            msg="连续拒绝后资源 A 的按日查询发生变化",
        )
        self.assertEqual(
            self.day_query(resource_b, DAY), day_b_before,
            msg="连续拒绝后资源 B 的按日查询发生变化",
        )

        # 原有最大预约标识来自快照，不预先猜测其数值；新标识必须紧接其后，
        # 证明所有失败请求都没有消耗 AUTOINCREMENT 标识。
        previous_max_id = max(
            booking["booking_id"] for booking in day_a_before["bookings"]
        )
        adjacent_id = self.reserve_ok(
            resource_a, "2026-10-05T10:00", "2026-10-05T11:00"
        )
        self.assertEqual(
            adjacent_id, previous_max_id + 1,
            msg=f"拒绝样例后合法预约的 booking_id 预期紧接原有最大标识 "
                f"{previous_max_id}（即 {previous_max_id + 1}），"
                f"实际为 {adjacent_id}，说明失败请求消耗了标识",
        )

        # 新预约与原预约都能按日读回，按 start 升序排列。
        payload = self.day_query(resource_a, DAY)
        self.assertEqual(
            payload["bookings"],
            [
                {"booking_id": seed_id,
                 "start": "2026-10-05T09:00", "end": "2026-10-05T10:00"},
                {"booking_id": adjacent_id,
                 "start": "2026-10-05T10:00", "end": "2026-10-05T11:00"},
            ],
            msg=f"拒绝后再提交合法相邻时段的读回结果不符：{payload!r}",
        )
        # 新建预约不改变资源目录。
        self.assertEqual(self.run_ok("resource-list"), catalog_before)


if __name__ == "__main__":
    unittest.main()
