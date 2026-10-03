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


def parse_time(text):
    """严格解析 YYYY-MM-DDTHH:mm，返回带 UTC+08:00 时区的 datetime。"""
    if text is None or not TIME_RE.match(text):
        raise UsageError("expected YYYY-MM-DDTHH:mm")
    try:
        parsed = datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M")
    except ValueError:
        raise UsageError("invalid date or time")
    return parsed.replace(tzinfo=TIMEZONE_OFFSET)


def parse_date(text):
    """严格解析零填充 YYYY-MM-DD，返回带 UTC+08:00 时区的 date。"""
    if text is None or not DATE_RE.match(text):
        raise UsageError("expected YYYY-MM-DD")
    try:
        return datetime.datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        raise UsageError("invalid date")


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

    p_cancel = subparsers.add_parser("cancel")
    p_cancel.add_argument("--booking", required=True)

    subparsers.add_parser("resource-list")

    p_day_query = subparsers.add_parser("day-query")
    p_day_query.add_argument("--resource", required=True)
    p_day_query.add_argument("--date", required=True)

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
            start_dt = parse_time(args.start)
            end_dt = parse_time(args.end)
            if not start_dt < end_dt:
                raise UsageError("start must be strictly before end")
            start, end = args.start, args.end
        elif args.command == "cancel":
            booking_id = parse_positive_int(args.booking)
        elif args.command == "resource-list":
            pass  # 无额外参数，无需校验
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
        if args.command in ("reserve", "day-query"):
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
            return _emit({"resources": store.list_resources(conn)}, 0)

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
