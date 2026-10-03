# 回归测试执行说明

`test_day_query.py` 固定 `day-query` 按日查询的公开行为；`test_cancel_rebook.py` 固定 `cancel` 取消后再次预约这条流程的公开行为（时段释放、新预约不复用旧标识、旧标识与后来预约相互隔离，以及各类失败分支）；`test_reserve_conflict.py` 固定 `reserve` 的左闭右开时段冲突规则（各类相交拒绝、端点相接与不同资源放行、冲突失败不改记录、跨午夜一致性及重开持久化）；`test_legacy_db_compat.py` 固定取消功能上线前旧 SQLite 库的兼容承诺（首次打开自动补齐 cancelled 列、旧预约一律视为有效、取消旧预约后以大于原最大标识的新标识重新预约、重开持久化，以及未打开旧库上非法输入不迁移不改数据）；`test_overflow_id.py` 固定超大正整数标识的公开行为（越界标识按不存在处理、前导零不改变数值含义、非法输入优先于“不存在”、越界请求不影响已有数据与标识分配）。均仅使用 Python 标准库，无需安装任何依赖。

## 运行

在项目根目录（`booking` 包所在目录）执行：

```sh
python3 -m unittest                # 自动发现并运行全部测试
python3 -m unittest test_day_query -v     # 只运行按日查询用例
python3 -m unittest test_cancel_rebook -v     # 只运行取消后再次预约用例
python3 -m unittest test_reserve_conflict -v # 只运行时段冲突规则用例
python3 -m unittest test_legacy_db_compat -v # 只运行旧库兼容承诺用例
python3 -m unittest test_overflow_id -v      # 只运行超大标识用例
```

全部通过时退出码为 0，末行输出 `OK`。

## 说明

- 测试通过公开入口 `python -m booking --db <文件> <命令>` 准备数据并断言，不触碰 `booking` 包内部实现。
- 每个用例使用独立的临时 SQLite 数据库，结束后自动清理；不依赖已有数据库、当前日期、机器时区或第三方库。
- 断言基于解析后的 JSON 内容，不依赖输出对象的键顺序。
- `test_cancel_rebook.py` 额外覆盖：取消成功返回原标识且时段立即释放；同资源同时段再次预约获得新的正整数 `booking_id`；旧标识再次取消返回 `booking_not_found` 且不影响后来创建的预约；重新预约后同时段请求返回 `booking_conflict`；重开数据库后取消状态与隔离效果保持一致；缺失参数、零/负数/小数/非整数标识返回 `invalid_input`，失败前后按日查询结果一致，且非法输入不会新建数据库文件。
- `test_legacy_db_compat.py` 额外覆盖：旧库样本（无 `cancelled` 列，资源 7/12 与预约 21/35）首次打开即自动迁移，旧预约仍被视为有效（按日查询准确返回且跨资源不混入，同时段预约返回 `booking_conflict`，失败后查询不变），资源名称、资源标识与预约原始时间不被改变；取消旧预约 21 后资源 7 按日结果为空，同时段以严格大于 35 的新标识重新预约成功，再次取消 21 返回 `booking_not_found` 且新预约仍可查询，取消状态与新预约在独立进程重开同一文件后保持一致，资源 12 的预约 35 始终保留；在另一份从未打开的旧库上以标识 0 调用 cancel 返回 `invalid_input`，表结构（仍无 `cancelled` 列）与已有数据均不改变。
- `test_overflow_id.py` 额外覆盖：`9223372036854775808` 与连续五千个 `9` 作为标识时，reserve / day-query 返回 `resource_not_found`、cancel 返回 `booking_not_found`，退出码 2 且标准错误无异常堆栈；`0001` 仍指向标识 1（查询一致、参与冲突判断、可取消），`0009223372036854775808` 仍按不存在处理；超大标识与非法日期/时间或起止顺序错误同时出现时优先返回 `invalid_input`；边界值 `9223372036854775807` 本身按普通不存在标识处理；越界请求前后按日查询结果（标识、起止文本、数量、顺序）一致，不消耗预约标识，原预约仍参与冲突判断且可正常取消。
