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
POSITIVE_INT_RE = re.compile(r"^\d+$")

# SQLite INTEGER 上限的十进制文本，用于不做大整数转换的越界比较。
_INT64_MAX_TEXT = str(store.SQLITE_INT64_MAX)


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
    if text is None or not POSITIVE_INT_RE.match(text):
        raise UsageError("expected positive integer")
    # 去掉前导零后按十进制文本比较，避免 int() 的位数上限
    # （Python 3.11+ 默认 4300 位）在超大标识上抛出 ValueError。
    digits = text.lstrip("0") or "0"
    if len(digits) > len(_INT64_MAX_TEXT) or (
        len(digits) == len(_INT64_MAX_TEXT) and digits > _INT64_MAX_TEXT
    ):
        # 超出 SQLite INTEGER 范围的正整数不可能存在于库中，
        # 归一化为越界哨兵，由持久层统一按“不存在”处理。
        return store.SQLITE_INT64_MAX + 1
    value = int(digits)
    if value <= 0:
        raise UsageError("expected positive integer")
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
        if args.command == "resource-add":
            resource_id = store.insert_resource(conn, name)
            return _emit(
                {"resource_id": resource_id, "name": name}, 0
            )

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
