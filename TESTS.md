# 回归测试执行说明

- `test_day_query.py` 固定 `day-query` 按日查询的公开行为。
- `test_cancel_rebook.py` 固定 `cancel` 取消后再次预约的公开行为：取消释放时段、新预约不复用旧标识、旧标识隔离（再次取消报 `booking_not_found` 且不影响新预约）、重开数据库后状态保持一致，以及非法/不存在预约标识的失败分支（`invalid_input` / `booking_not_found`，退出码 2，不改动数据、不创建数据库文件）。

仅使用 Python 标准库，无需安装任何依赖。

## 运行

在项目根目录（`booking` 包所在目录）执行：

```sh
python3 -m unittest                # 自动发现并运行全部测试
python3 -m unittest test_day_query -v    # 只运行按日查询用例
python3 -m unittest test_cancel_rebook -v  # 只运行取消后再预约用例
```

全部通过时退出码为 0，末行输出 `OK`。

## 说明

- 测试通过公开入口 `python -m booking --db <文件> <命令>` 准备数据并断言，不触碰 `booking` 包内部实现。
- 每个用例使用独立的临时 SQLite 数据库，结束后自动清理；不依赖已有数据库、当前日期、机器时区或第三方库。
- 断言基于解析后的 JSON 内容，不依赖输出对象的键顺序。
