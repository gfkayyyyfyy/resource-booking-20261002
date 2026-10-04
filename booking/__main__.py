"""命令行入口：python -m booking --db <sqlite 文件> <命令> ...

每次业务命令在标准输出打印一个 JSON 对象：
成功退出码 0，业务失败退出码 2（错误对象见各命令说明）。
"""

import argparse
import datetime
import json
import re
import sys

from . import store

# 固定时区：所有输入时间统一解释为 UTC+08:00 本地时间。
TIMEZONE_OFFSET = datetime.timezone(datetime.timedelta(hours=8))

# 严格的 YYYY-MM-DDTHH:mm 定宽格式（拒绝秒、时区后缀、非零填充等）。
TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
# 严格的 YYYY-MM-DD 定宽日期格式。
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# 只接受纯数字文本（允许前导零）；数值上限另行判断。
# 必须用 \Z 锚定字符串绝对结尾，不能用 $：$ 允许在末尾 LF 之前匹配，
# 会让 "1\n" 被当成 1、"0\n" 进入纯零分支后触发未捕获的 ValueError。
# \Z 不接受任何尾随字符，首尾空白、制表符、CR 或夹在数字中间的 LF 一律拒绝。
POSITIVE_INT_RE = re.compile(r"^\d+\Z")
# --min-minutes 与 --repeat-weeks 共同的数值文本规则只维护这一份：
# 只接受非空 ASCII 十进制数字（\d 会匹配各语种十进制数字，必须用 [0-9]），
# 允许任意数量前导零、按实际数值解释；各自的取值范围在解析函数处判断。
# 必须用 \Z 锚定字符串绝对结尾，不能用 $：$ 允许在末尾 LF 之前匹配，
# 会让 "1\n" 被当成 1、"0\n" 进入纯零分支后触发未捕获的 ValueError。
# \Z 不接受任何尾随字符，首尾空白、制表符、CR 或夹在数字中间的 LF 一律拒绝。
BOUNDED_ASCII_INT_RE = re.compile(r"^[0-9]+\Z")
# --min-minutes 的取值上限：一天 24 小时的分钟数（范围 1 至 1440）。
MAX_MIN_MINUTES = 1440
# --repeat-weeks 的取值范围：包含首次在内的总次数 2 至 8。
MIN_REPEAT_WEEKS = 2
MAX_REPEAT_WEEKS = 8
# SQLite INTEGER 主键可表示的最大有符号整数；任何真实存在的资源/预约
# 标识都不可能超过它，因此更大的正整数一律等价于“标识不存在”。
SQLITE_MAX_ID = 2 ** 63 - 1
_SQLITE_MAX_ID_DIGITS = str(SQLITE_MAX_ID)

# 标识为超过 SQLite INTEGER 上限的正整数：输入合法，但该标识必然不存在。
# 由调用方在全部校验完成后按 not_found 处理，绝不绑定给 SQLite。
OVERSIZED_ID = object()


class UsageError(Exception):
    """参数或输入值不合法。"""


class _Parser(argparse.ArgumentParser):
    """把 argparse 的默认报错（退出码 2 + stderr 文本）转为 UsageError。"""

    def error(self, message):
        raise UsageError(message)


def _emit(payload, exit_code):
    print(json.dumps(payload, ensure_ascii=False))
    return exit_code


def invalid_input():
    return _emit({"error": "invalid_input"}, 2)


def parse_positive_int(text):
    """解析正整数文本，允许任意长度与前导零（按数值解释）。

    范围：返回 [1, 2^63-1] 内的 int；超过 SQLite INTEGER 上限时返回
    OVERSIZED_ID（输入合法，但该标识必然不存在）。不把超长数字串直接
    交给 int()：Python 对超长数字串的转换有位数限制，且标识是否越界
    只取决于十进制数值，与位数或前导零无关。
    """
    if text is None or not POSITIVE_INT_RE.match(text):
        raise UsageError("expected positive integer")
    # 按数值跳过前导零（\d 可匹配各语种十进制数字，不能只剥 ASCII '0'）。
    index = 0
    while index < len(text) and int(text[index]) == 0:
        index += 1
    if index == len(text):  # 纯零（含 "0"、"000"）不是正整数。
        raise UsageError("expected positive integer")
    if len(text) - index > len(_SQLITE_MAX_ID_DIGITS):
        return OVERSIZED_ID
    # 剩余至多 19 位，int() 转换不受超长数字串限制。
    value = int(text[index:])
    if value > SQLITE_MAX_ID:
        return OVERSIZED_ID
    return value


def parse_bounded_ascii_int(text, lower, upper, message):
    """解析有界 ASCII 十进制整数文本，允许任意数量前导零（按数值解释）。

    --min-minutes 与 --repeat-weeks 共同的输入校验规则只在此维护：缺值、
    空文本、纯零、负数、小数、含空格/制表符/换行或非 ASCII 数字（全角、
    阿拉伯文等）的文本，以及数值不在 [lower, upper] 内，一律抛 UsageError。
    先剥前导零再按位数判断，不把超长数字串直接交给 int()（Python 对超长
    数字串的转换有位数限制），故五千个 9 之类的输入也走同一非法路径。
    """
    if text is None or not BOUNDED_ASCII_INT_RE.match(text):
        raise UsageError(message)
    stripped = text.lstrip("0")
    if not stripped or len(stripped) > len(str(upper)):
        # 纯零（含 "0"、"000"）或剥零后位数已多于上限：都不在范围内。
        raise UsageError(message)
    # 剩余位数与上限同级，int() 转换不受超长数字串限制。
    value = int(stripped)
    if not lower <= value <= upper:
        raise UsageError(message)
    return value


def parse_min_minutes(text):
    """解析 --min-minutes：1 至 1440 的十进制整数文本，允许前导零。

    文本规则见 parse_bounded_ascii_int；范围含义为本参数独有。
    """
    return parse_bounded_ascii_int(
        text, 1, MAX_MIN_MINUTES, "expected integer minutes in [1, 1440]"
    )


def parse_repeat_weeks(text):
    """解析 --repeat-weeks：2 至 8 的 ASCII 十进制整数文本，允许前导零。

    文本规则见 parse_bounded_ascii_int；[2, 8] 表示包含首次在内的总次数。
    """
    return parse_bounded_ascii_int(
        text,
        MIN_REPEAT_WEEKS,
        MAX_REPEAT_WEEKS,
        "expected integer repeat count in [2, 8]",
    )


def parse_time(text):
    """严格解析 YYYY-MM-DDTHH:mm，返回带 UTC+08:00 时区的 datetime。"""
    if text is None or not TIME_RE.match(text):
        raise UsageError("expected YYYY-MM-DDTHH:mm")
    try:
        parsed = datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M")
    except ValueError:
        raise UsageError("invalid date or time")
    return parsed.replace(tzinfo=TIMEZONE_OFFSET)


def parse_time_window(start_text, end_text):
    """校验 reserve/free-query 共用的时间窗口规则，返回原始 (start, end) 文本。

    两个入口共同的时间窗口规则只在此维护：先后严格解析开始与结束文本
    （固定 UTC+08:00、零填充 YYYY-MM-DDTHH:mm，拒绝秒、时区后缀与首尾
    空白，非法日期由 parse_time 拒绝），并要求开始严格早于结束；起止
    相等或颠倒都抛 UsageError。过去日期与跨午夜窗口不在此限制，继续
    合法。成功时返回入参原文：成功结果顶层原样回显，定宽文本也直接
    交给存储层（字典序即时间先后）。
    """
    start_dt = parse_time(start_text)
    end_dt = parse_time(end_text)
    if not start_dt < end_dt:
        raise UsageError("start must be strictly before end")
    return start_text, end_text


def parse_date(text):
    """严格解析零填充 YYYY-MM-DD，返回带 UTC+08:00 时区的 date。"""
    if text is None or not DATE_RE.match(text):
        raise UsageError("expected YYYY-MM-DD")
    try:
        return datetime.datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        raise UsageError("invalid date")


def _format_time(value):
    """把 datetime 格式化为定宽 YYYY-MM-DDTHH:mm。

    年份手工零填充：%Y 在部分平台上对 1000 年以前的年份不补零。
    """
    return (
        f"{value.year:04d}-{value.month:02d}-{value.day:02d}"
        f"T{value.hour:02d}:{value.minute:02d}"
    )


def repeat_occurrences(start, end, count):
    """把 [start, end) 扩展为每周同一时段的 count 次预约（含首次）。

    首次区间使用原始起止文本，之后每次将两个端点分别推进七个本地日历
    日（固定 UTC+08:00，分钟精度）；结果按发生时间升序。任一生成端点
    超出可表示的日期范围时抛 UsageError（按 invalid_input 处理）。
    """
    start_dt = parse_time(start)
    end_dt = parse_time(end)
    occurrences = []
    for index in range(count):
        try:
            occurrence_start = start_dt + datetime.timedelta(weeks=index)
            occurrence_end = end_dt + datetime.timedelta(weeks=index)
        except OverflowError:
            raise UsageError("repeat endpoint out of representable date range")
        occurrences.append(
            (_format_time(occurrence_start), _format_time(occurrence_end))
        )
    return occurrences


def build_parser():
    parser = _Parser(prog="booking", add_help=True)
    parser.add_argument("--db", required=True, help="SQLite 数据库文件路径")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_add = subparsers.add_parser("resource-add")
    p_add.add_argument("--name", required=True)

    p_reserve = subparsers.add_parser("reserve")
    p_reserve.add_argument("--resource", required=True)
    p_reserve.add_argument("--start", required=True)
    p_reserve.add_argument("--end", required=True)
    # 可选：每周同一时段的重复预约总次数（含首次）；缺省为单次预约。
    p_reserve.add_argument("--repeat-weeks", default=None)

    p_cancel = subparsers.add_parser("cancel")
    p_cancel.add_argument("--booking", required=True)

    p_list = subparsers.add_parser("resource-list")
    # 可选：只返回保存后的名称包含该片段的资源；缺省返回完整目录。
    p_list.add_argument("--contains", default=None)

    p_day_query = subparsers.add_parser("day-query")
    p_day_query.add_argument("--resource", required=True)
    p_day_query.add_argument("--date", required=True)

    p_free_query = subparsers.add_parser("free-query")
    p_free_query.add_argument("--resource", required=True)
    p_free_query.add_argument("--start", required=True)
    p_free_query.add_argument("--end", required=True)
    # 可选：只保留窗口内连续分钟数不低于该值的空闲区间；缺省不过滤。
    p_free_query.add_argument("--min-minutes", default=None)

    return parser


def main(argv):
    parser = build_parser()
    # 先解析参数并完成全部输入校验，之后才连接/初始化数据库，
    # 保证非法输入不会新建文件或改动任何记录。
    try:
        args = parser.parse_args(argv)
        if args.command == "resource-add":
            name = args.name.strip()
            if not name:
                raise UsageError("name must not be empty")
        elif args.command == "reserve":
            resource_id = parse_positive_int(args.resource)
            start, end = parse_time_window(args.start, args.end)
            # 省略 --repeat-weeks 时保持单次预约语义；给出时先校验次数文本，
            # 再生成全部区间（任一端点越界同样视为非法输入），与标识、时间
            # 校验一起先于资源存在性检查。
            occurrences = None
            if args.repeat_weeks is not None:
                occurrences = repeat_occurrences(
                    start, end, parse_repeat_weeks(args.repeat_weeks)
                )
        elif args.command == "cancel":
            booking_id = parse_positive_int(args.booking)
        elif args.command == "resource-list":
            # --contains 去除首尾空白后参与匹配（中间空格保留）；
            # 缺省为 None 表示不筛选，空文本或纯空白一律非法。
            contains = None
            if args.contains is not None:
                contains = args.contains.strip()
                if not contains:
                    raise UsageError("contains must not be empty")
        elif args.command == "day-query":
            resource_id = parse_positive_int(args.resource)
            day = parse_date(args.date)
            day_start = f"{args.date}T00:00"
            try:
                next_day = day + datetime.timedelta(days=1)
                day_end = f"{next_day.isoformat()}T00:00"
            except OverflowError:
                # 9999-12-31 之后没有可表示的次日，查询上界不设限。
                day_end = None
        elif args.command == "free-query":
            resource_id = parse_positive_int(args.resource)
            start, end = parse_time_window(args.start, args.end)
            min_minutes = None
            if args.min_minutes is not None:
                min_minutes = parse_min_minutes(args.min_minutes)
        else:  # pragma: no cover - argparse 已保证
            raise UsageError("unknown command")
    except UsageError:
        return invalid_input()

    # 输入合法后再打开数据库（不存在则初始化），后续进程沿用同一文件。
    conn = store.connect(args.db)
    try:
        # 超出 SQLite INTEGER 范围的正整数是合法输入，但真实标识不可能超过
        # 2^63-1，故在发起任何查询前按“不存在”短路，绝不把超大整数绑定给
        # SQLite（否则会抛 OverflowError）。此时连接已正常打开，因此合法输入
        # 原有的建库初始化与旧库兼容行为保持不变；invalid_input 已在连接前
        # 返回，优先级不受影响。
        if args.command in ("reserve", "day-query", "free-query"):
            if resource_id is OVERSIZED_ID:
                return _emit({"error": "resource_not_found"}, 2)
        elif args.command == "cancel":
            if booking_id is OVERSIZED_ID:
                return _emit({"error": "booking_not_found"}, 2)

        if args.command == "resource-add":
            resource_id = store.insert_resource(conn, name)
            return _emit(
                {"resource_id": resource_id, "name": name}, 0
            )

        if args.command == "resource-list":
            return _emit({"resources": store.list_resources(conn, contains)}, 0)

        if args.command == "cancel":
            if not store.cancel_booking(conn, booking_id):
                return _emit({"error": "booking_not_found"}, 2)
            return _emit({"booking_id": booking_id, "cancelled": True}, 0)

        if args.command == "day-query":
            if not store.resource_exists(conn, resource_id):
                return _emit({"error": "resource_not_found"}, 2)
            bookings = store.query_day_bookings(
                conn, resource_id, day_start, day_end
            )
            return _emit(
                {
                    "resource_id": resource_id,
                    "date": args.date,
                    "bookings": bookings,
                },
                0,
            )

        if args.command == "free-query":
            if not store.resource_exists(conn, resource_id):
                return _emit({"error": "resource_not_found"}, 2)
            free_slots = store.query_free_slots(
                conn, resource_id, start, end, min_minutes
            )
            return _emit(
                {
                    "resource_id": resource_id,
                    "start": start,
                    "end": end,
                    "free_slots": free_slots,
                },
                0,
            )

        if args.command == "reserve" and occurrences is not None:
            booking_ids, error = store.insert_bookings(
                conn, resource_id, occurrences
            )
            if error == "resource_not_found":
                return _emit({"error": "resource_not_found"}, 2)
            if error == "booking_conflict":
                return _emit({"error": "booking_conflict"}, 2)
            return _emit(
                {
                    "resource_id": resource_id,
                    "bookings": [
                        {"booking_id": booking_id, "start": s, "end": e}
                        for booking_id, (s, e) in zip(booking_ids, occurrences)
                    ],
                },
                0,
            )

        booking_id, error = store.insert_booking(
            conn, resource_id, start, end
        )
        if error == "resource_not_found":
            return _emit({"error": "resource_not_found"}, 2)
        if error == "booking_conflict":
            return _emit({"error": "booking_conflict"}, 2)
        return _emit(
            {
                "booking_id": booking_id,
                "resource_id": resource_id,
                "start": start,
                "end": end,
            },
            0,
        )
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
