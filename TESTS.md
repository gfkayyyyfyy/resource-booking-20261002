# 回归测试执行说明

`test_day_query.py` 固定 `day-query` 按日查询的公开行为；`test_free_query.py` 固定 `free-query` 空闲时段查询的公开行为（最大连续空闲区间、左闭右开、越界截断、只读语义与各类失败分支）；`test_free_min_minutes.py` 固定 `free-query --min-minutes` 最小时长筛选的公开行为（按窗口内连续分钟数过滤、合格区间保留完整端点、边界值与各类非法值拒绝）；`test_resource_list.py` 固定 `resource-list` 资源目录查询的公开行为（输出结构、按标识数值升序、同名不合并、只读语义与非法参数拒绝）；`test_cancel_rebook.py` 固定 `cancel` 取消后再次预约这条流程的公开行为（时段释放、新预约不复用旧标识、旧标识与后来预约相互隔离，以及各类失败分支）；`test_reserve_conflict.py` 固定 `reserve` 的左闭右开时段冲突规则（各类相交拒绝、端点相接与不同资源放行、冲突失败不改记录、跨午夜一致性及重开持久化）；`test_legacy_db_compat.py` 固定取消功能上线前旧 SQLite 库的兼容承诺（首次打开自动补齐 cancelled 列、旧预约一律视为有效、取消旧预约后以大于原最大标识的新标识重新预约、重开持久化，以及未打开旧库上非法输入不迁移不改数据）；`test_oversized_id.py` 固定超过 SQLite INTEGER 范围（>2^63-1）的超大正整数标识行为（reserve/day-query 返回 resource_not_found、cancel 返回 booking_not_found，五千个 9 与任意前导零按同一数值规则处理，0001 指向标识 1，invalid_input 校验优先级，失败不新增记录/不消耗标识/原预约仍冲突且可取消，非法输入不建文件而越界合法输入与普通未知标识一样初始化/迁移旧库，边界值 2^63-1 走普通查询）；`test_repeat_weeks.py` 固定 `reserve --repeat-weeks` 每周重复预约的公开行为（含首次的总次数 2 至 8、端点逐周推进七个日历日、整批全部成功或全部失败、生成区间彼此重叠或与未取消预约重叠均报 booking_conflict 且不保存不消耗标识、成功输出只含 resource_id 与按发生时间升序的 bookings、各项独立标识持久化为普通预约可分别查询与取消、非法次数文本与生成端点越界统一 invalid_input 且先于资源检查、不建库不迁移旧库）；`test_newline_id.py` 固定标识文本混入空白字符的拒绝行为（reserve/day-query 的 --resource、cancel 的 --booking 若在首尾或数字中间含 LF/CR/空格/制表符，包括历史上的 "1\n" 被当成标识 1 与 "0\n"/"000\n" 触发 ValueError 堆栈，一律返回 invalid_input、退出码 2 且 stderr 为空，不自动去除空白；校验先于资源/预约存在与冲突判断，失败不建文件、不新增或取消记录、不消耗标识、不迁移旧库，失败前后 day-query 一致且原预约仍可正常取消；0001 与 1 等价、Unicode 十进制数字按数值解释、全零仍非法、大于 2^63-1 仍按不存在处理等正常数值语义不变）；`test_unicode_digit_time.py` 固定日期/时间参数只接受 ASCII 数字的行为（reserve/free-query 的起止与 day-query 的日期中，全角、阿拉伯文数字及其与 ASCII 混写的年份/月/日/时/分一律 invalid_input、退出码 2 且 stderr 为空，不自动转换；起止端点分别检查，同一规则适用于 --repeat-weeks 与 --min-minutes；校验先于资源存在性（含超大标识）与冲突判断，失败不建文件、不新增记录、不消耗标识，正常查询下原预约标识与完整起止不变；ASCII 时间的冲突、闰日、过去日期、跨午夜与左闭右开规则不变，资源/预约标识继续允许 Unicode 十进制数字）。均仅使用 Python 标准库，无需安装任何依赖。

## 运行

在项目根目录（`booking` 包所在目录）执行：

```sh
python3 -m unittest                # 自动发现并运行全部测试
python3 -m unittest test_day_query -v     # 只运行按日查询用例
python3 -m unittest test_free_query -v    # 只运行空闲时段查询用例
python3 -m unittest test_free_min_minutes -v # 只运行最小时长筛选用例
python3 -m unittest test_resource_list -v # 只运行资源目录查询用例
python3 -m unittest test_cancel_rebook -v     # 只运行取消后再次预约用例
python3 -m unittest test_reserve_conflict -v # 只运行时段冲突规则用例
python3 -m unittest test_legacy_db_compat -v # 只运行旧库兼容承诺用例
python3 -m unittest test_oversized_id -v     # 只运行超大标识用例
python3 -m unittest test_repeat_weeks -v     # 只运行每周重复预约用例
python3 -m unittest test_newline_id -v      # 只运行标识含空白字符的拒绝用例
python3 -m unittest test_unicode_digit_time -v # 只运行日期/时间非 ASCII 数字的拒绝用例
```

全部通过时退出码为 0，末行输出 `OK`。

## 说明

- 测试通过公开入口 `python -m booking --db <文件> <命令>` 准备数据并断言，不触碰 `booking` 包内部实现。
- 每个用例使用独立的临时 SQLite 数据库，结束后自动清理；不依赖已有数据库、当前日期、机器时区或第三方库。
- 断言基于解析后的 JSON 内容，不依赖输出对象的键顺序。
- `test_resource_list.py` 额外覆盖：成功查询退出码 0、stderr 为空，stdout 为单个 JSON 对象且顶层只有 `resources`，每项仅含 `resource_id` 与 `name`；路径不存在时沿用初始化行为建库并返回 `{"resources": []}`，随后首个资源标识仍为 1；依次登记“  二号会议室  ”“一号 会议室”“二号会议室”后按标识数值升序返回，不按名称排序、不合并同名项，两个“二号会议室”均为去除首尾空白后的名称且标识不同，“一号 会议室”中间的空格原样保留；标识跨过 9 到 10 时按数值而非文本顺序排列；重复查询与独立进程重开同一数据库后解析结果一致；在已有预约的库上连续查询后，新资源与非重叠时段的新预约各自获得紧接原有最大标识的标识，查询前后 day-query 结果相同，取消预约前后资源目录相同；携带不支持的 `--name` 参数时只返回 `{"error": "invalid_input"}`、退出码 2 且 stderr 为空，不存在的数据库不被创建，已有数据库的目录与预约查询结果不变。
- `test_cancel_rebook.py` 额外覆盖：取消成功返回原标识且时段立即释放；同资源同时段再次预约获得新的正整数 `booking_id`；旧标识再次取消返回 `booking_not_found` 且不影响后来创建的预约；重新预约后同时段请求返回 `booking_conflict`；重开数据库后取消状态与隔离效果保持一致；缺失参数、零/负数/小数/非整数标识返回 `invalid_input`，失败前后按日查询结果一致，且非法输入不会新建数据库文件。
- `test_legacy_db_compat.py` 额外覆盖：旧库样本（无 `cancelled` 列，资源 7/12 与预约 21/35）首次打开即自动迁移，旧预约仍被视为有效（按日查询准确返回且跨资源不混入，同时段预约返回 `booking_conflict`，失败后查询不变），资源名称、资源标识与预约原始时间不被改变；取消旧预约 21 后资源 7 按日结果为空，同时段以严格大于 35 的新标识重新预约成功，再次取消 21 返回 `booking_not_found` 且新预约仍可查询，取消状态与新预约在独立进程重开同一文件后保持一致，资源 12 的预约 35 始终保留；在另一份从未打开的旧库上以标识 0 调用 cancel 返回 `invalid_input`，表结构（仍无 `cancelled` 列）与已有数据均不改变。
- `test_newline_id.py` 额外覆盖：在 2026-10-05 的有效预约上分别以带真实 LF 的资源标识（reserve/day-query 的 --resource）与预约标识（cancel 的 --booking）调用，均返回 `invalid_input`、退出码 2、stdout 只有一个 JSON 对象且 stderr 完全为空（无异常堆栈），失败前后 day-query 逐字节一致、原预约仍可用正常标识取消；首尾空格/制表符/CR 及夹在数字中间的换行同样拒绝且不去除空白；对不存在的数据库使用 `0\n`（以及 `1\n`）不创建任何文件；校验先于资源/预约存在与冲突判断（`999\n`、本会冲突的 `1\n` 均只报 invalid_input）；已有库不新增或取消记录、不消耗 AUTOINCREMENT 标识，未迁移旧库不被补齐 `cancelled` 列且数据原样；`0001` 与 `1` 等价、Unicode 十进制数字（如 U+0663）按数值解释、全零文本仍为 invalid_input、大于 2^63-1 仍按 resource_not_found/booking_not_found 处理。
- `test_free_query.py` 额外覆盖：验收场景（09:00–10:00 与 10:30–11:00 两条预约下，09:30–11:30 返回 10:00–10:30 与 11:00–11:30，09:15–09:45 返回空数组）；无占用返回整个窗口且端点保留完整日期时间；端点相接的预约与窗口边界相接的预约均不产生空区间；跨日/越界预约只按相交部分截断；已取消、其他资源与窗口外预约不影响结果；重复查询一致且不消耗预约标识；未知资源与超大正整数标识返回 `resource_not_found`；缺参、多余参数、非法标识、无效日期时间、起止相等或颠倒均返回 `invalid_input`（优先于资源存在性检查），且非法输入不会新建数据库文件。
- `test_free_min_minutes.py` 额外覆盖：验收场景（同一资源 09:00–10:00 与 10:30–11:00 两条预约下查询 09:30–12:00，`--min-minutes 30` 返回 10:00–10:30 与 11:00–12:00，`--min-minutes 31` 只返回 11:00–12:00）；省略参数时返回与既有查询完全一致；持续分钟数恰好等于下限时保留（大于或等于）；合格区间保留完整起止端点，不截成指定长度、不拆分；无达标区间时 `free_slots` 为空数组并成功退出；无占用时整个窗口仅在时长达标时返回；跨午夜区间按完整日期计分钟（90 分钟边界）；跨出窗口的占用只按相交部分截断后再计时长；前导零按数值解释、1 与 1440 为合法边界；缺值、空文本、零、负数、小数、超范围（含超长数字串）、含空白或其他字符（含全角/阿拉伯文数字）均返回 `invalid_input`、退出码 2 且 stderr 为空，校验优先于资源存在性检查，不建数据库文件、不迁移旧库；合法参数下资源不存在（含超大正整数标识）返回 `resource_not_found`；重复查询与重开后结果一致且不消耗标识。
- `test_repeat_weeks.py` 额外覆盖：验收场景（资源已约 2026-10-12 09:00–10:00 时，以 2026-10-05 09:00–10:00 请求两次重复整批冲突且首周无新增；取消原预约后重试得到两个独立预约，重开同一 SQLite 文件仍可分别查询）；八次重复逐周推进且标识各自独立、按发生时间升序；前导零 `002` 按数值解释；跨日区间整体推进七天；过去日期允许；时长恰好七天端点相接不自冲突、大于七天彼此重叠报 `booking_conflict`；与既有预约端点相接、已取消及其他资源预约均不阻挡；冲突整批失败不新增记录、不消耗预约标识、原记录不变且重开后一致；重复预约作为普通预约可分别查询，`cancel` 只取消指定一项且新预约不复用旧标识；省略参数时单次预约输出结构不变；缺值、空文本、零、1、9、负数、小数、超长数字串、含空白或其他字符（含全角/阿拉伯文数字）均返回 `invalid_input`、退出码 2 且 stderr 为空，生成端点超出可表示日期范围同样 `invalid_input`，校验优先于资源存在性与时间窗口检查，不建数据库文件、不迁移旧库；合法参数下资源不存在（含超大正整数标识）返回 `resource_not_found` 且不新增记录。
