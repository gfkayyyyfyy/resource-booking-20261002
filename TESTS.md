# 回归测试执行说明

`test_day_query.py` 固定 `day-query` 按日查询的公开行为；`test_cancel_rebook.py` 固定 `cancel` 取消后再次预约这条流程的公开行为（时段释放、新预约不复用旧标识、旧标识与后来预约相互隔离，以及各类失败分支）；`test_reserve_conflict.py` 固定 `reserve` 的左闭右开时段冲突规则（各类相交拒绝、端点相接与不同资源放行、冲突失败不改记录、跨午夜一致性及重开持久化）；`test_legacy_db_compat.py` 固定取消功能上线前创建的旧版数据库的兼容承诺（首次打开自动补齐取消标记列、既有预约一律视为未取消）。均仅使用 Python 标准库，无需安装任何依赖。

## 运行

在项目根目录（`booking` 包所在目录）执行：

```sh
python3 -m unittest                # 自动发现并运行全部测试
python3 -m unittest test_day_query -v     # 只运行按日查询用例
python3 -m unittest test_cancel_rebook -v     # 只运行取消后再次预约用例
python3 -m unittest test_reserve_conflict -v # 只运行时段冲突规则用例
python3 -m unittest test_legacy_db_compat -v # 只运行旧版数据库兼容性用例
```

全部通过时退出码为 0，末行输出 `OK`。

## 说明

- 测试通过公开入口 `python -m booking --db <文件> <命令>` 准备数据并断言，不触碰 `booking` 包内部实现。
- 每个用例使用独立的临时 SQLite 数据库，结束后自动清理；不依赖已有数据库、当前日期、机器时区或第三方库。
- 断言基于解析后的 JSON 内容，不依赖输出对象的键顺序。
- `test_cancel_rebook.py` 额外覆盖：取消成功返回原标识且时段立即释放；同资源同时段再次预约获得新的正整数 `booking_id`；旧标识再次取消返回 `booking_not_found` 且不影响后来创建的预约；重新预约后同时段请求返回 `booking_conflict`；重开数据库后取消状态与隔离效果保持一致；缺失参数、零/负数/小数/非整数标识返回 `invalid_input`，失败前后按日查询结果一致，且非法输入不会新建数据库文件。
- `test_legacy_db_compat.py` 的旧库样本由标准库 `sqlite3` 直接构造：表结构与自增标识规则与当前版本一致，仅 `bookings` 表缺少 `cancelled` 列；样本含资源 7 与 12，各有预约 21 与 35（同一时段）。覆盖：首次 `day-query` 打开后既有预约按未取消处理（可查询、参与冲突判断），资源名称、资源标识与预约原始时间不因首次打开而改变；取消旧预约后时段释放、再次预约获得大于样本最大标识 35 的新标识，旧标识再次取消返回 `booking_not_found` 且不影响新预约与其他资源的预约；在从未被打开的旧库上以非法标识调用 `cancel` 返回 `invalid_input`，表结构与已有数据完全不变。
