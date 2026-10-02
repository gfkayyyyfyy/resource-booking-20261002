"""命令行入口：参数解析、输入校验与 JSON 响应。

校验顺序固定为：输入合法性 -> 资源是否存在 -> 预约冲突，
任一步失败均输出 JSON 错误对象并以退出码 2 结束，且不写入任何数据。
"""

import argparse
import json
import re
import sys
from datetime import datetime

from .storage import Storage

TIME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$")
POSITIVE_INT_RE = re.compile(r"^\d+$")


class BookingArgumentParser(argparse.ArgumentParser):
    """把 argparse 的用法错误统一转换为 {"error": "invalid_input"}。"""

    def error(self, message):
        emit_error("invalid_input")


def emit_error(code):
    print(json.dumps({"error": code}, ensure_ascii=False))
    sys.exit(2)


def parse_positive_int(value):
    if not isinstance(value, str) or not POSITIVE_INT_RE.match(value):
        return None
    number = int(value)
    return number if number > 0 else None


def parse_time(value):
    """只接受 YYYY-MM-DDTHH:MM，且必须是真实存在的日期与时间。

    时间统一解释为固定 UTC+08:00 的本地时间；不接受秒或时区后缀。
    返回清洗后的时间字符串（格式本身已固定为等宽规范形式），非法返回 None。
    """
    if not isinstance(value, str):
        return None
    match = TIME_RE.match(value)
    if not match:
        return None
    try:
        datetime(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
            int(match.group(4)),
            int(match.group(5)),
        )
    except ValueError:
        return None
    return value


def build_parser():
    parser = BookingArgumentParser(prog="booking", allow_abbrev=False)
    parser.add_argument("--db", required=True)
    # 子解析器同样使用自定义类，保证子命令的参数错误也输出 JSON。
    subparsers = parser.add_subparsers(dest="command", parser_class=BookingArgumentParser)

    resource_add = subparsers.add_parser("resource-add")
    resource_add.add_argument("--name", required=True)

    reserve = subparsers.add_parser("reserve")
    reserve.add_argument("--resource", required=True)
    reserve.add_argument("--start", required=True)
    reserve.add_argument("--end", required=True)
    return parser


def cmd_resource_add(args, storage):
    name = args.name.strip() if args.name is not None else ""
    if not name:
        emit_error("invalid_input")
    resource_id = storage.add_resource(name)
    print(json.dumps(
        {"resource_id": resource_id, "name": name}, ensure_ascii=False
    ))


def cmd_reserve(args, storage):
    # 第一步：输入合法性
    resource_id = parse_positive_int(args.resource)
    start = parse_time(args.start)
    end = parse_time(args.end)
    if resource_id is None or start is None or end is None:
        emit_error("invalid_input")
    # 开始严格早于结束（等宽字符串可直接比较，跨日同样成立）
    if not start < end:
        emit_error("invalid_input")

    # 第二步：资源是否存在
    if not storage.resource_exists(resource_id):
        emit_error("resource_not_found")

    # 第三步：冲突判断（存储层在写事务内原子完成）
    booking_id = storage.add_booking(resource_id, start, end)
    if booking_id is None:
        emit_error("booking_conflict")

    print(json.dumps(
        {
            "booking_id": booking_id,
            "resource_id": resource_id,
            "start": start,
            "end": end,
        },
        ensure_ascii=False,
    ))


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        emit_error("invalid_input")

    storage = Storage(args.db)
    try:
        if args.command == "resource-add":
            cmd_resource_add(args, storage)
        elif args.command == "reserve":
            cmd_reserve(args, storage)
        else:
            emit_error("invalid_input")
    finally:
        storage.close()


if __name__ == "__main__":
    main()
