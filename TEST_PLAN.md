# Codex Desktop Bridge 测试计划

## 自动化测试

```bash
UV_PYTHON=3.13 uv run --with pytest --with pytest-asyncio \
  python3 -m pytest -q \
  tests/test_codex_app_server.py \
  tests/test_desktop_notifications.py \
  tests/test_desktop_ipc.py \
  tests/test_desktop_card.py \
  tests/test_desktop_bridge.py \
  tests/test_desktop_lark_integration.py \
  tests/test_option_select.py \
  tests/test_card_service_images.py \
  tests/test_desktop_subagents.py \
  tests/test_card_theme.py \
  tests/test_card_time.py \
  tests/test_card_builder_palette.py \
  tests/test_stream_poller.py
```

覆盖范围：

- 完成/失败提醒及普通/归档列表的北京时间显示；UTC、显式正负偏移、跨日/跨年、
  已为 UTC+8 时不重复转换、主机时区无关、毫秒省略及非法/无时区值安全省略；
  展示格式化不得修改原始时间字段或通知去重、排序逻辑；
- IPC 小端长度帧、initialize、request correlation、超时和重连；
- owner discovery、follow、start/steer/interrupt、审批和用户输入；
- `/desktop` 列表的项目映射、完整 Session ID 和每页 5 条分页；
- `/desktop` 列表的运行中、失败和空闲状态文本；
- Session 完成通知的持久去重、失败重试和一键连接按钮；
- 仅主线程自身完成产生通知；覆盖嵌套/JSON 来源、父线程标志、错误 rollout 映射、
  子任务复制的根历史、旧子任务 outbox 重试、元数据不完整时暂缓与恢复；
- 子 Agent 状态的 snapshot、canonical 历史、patch 与 rollout 基线投影；每轮去重、
  插入/删除后的 patch 索引更新、历史隔离、白名单字段与未知活动安全降级；
- `completed -> interacted` 不猜测运行状态，依据子线程自己的 ordinal 边界后的
  启动/完成/失败/中断记录确认；半行、超长记录、身份错配和扫描超限显示待同步；
- 子状态定时刷新不改父状态、不发送通知、继续复用原卡；慢文件读取不能丢失并发
  IPC 增量，重复启动不会创建多个轮询任务，关闭时必须回收；
- 成功/失败完成提醒仅由 `NEXT / 重新连接` 整块区域承接 `desktop_attach`，不再出现
  下方的“连接此 Session”按钮；缺失有效 ID 时无连接回调，列表高亮区域保持非交互；
- 长连接卡片按 turn 展示 Query/进度及历史翻页，翻页状态按 chat 隔离，历史轮隐藏 live pending/停止操作，成功发送后立即回到最新轮；
- Desktop 实时卡、任务列表、归档列表和完成通知的 Card JSON 2.0 GPT-style
  Workspace 基础契约：`compact_width=false`、公开摘要、等比分栏、标准折叠箭头，
  以及集中式低饱和色板：雾蓝灰操作色、灰蓝运行色、灰绿完成色、米灰等待色、
  灰红失败色与中性状态色；状态徽标与主操作色必须分离，旧亮蓝不可回退；
- 已确认色板的全部 light/dark 值、返回值修改隔离和文字对比度；成功/失败 RESULT
  与固定操作色 NEXT 不串色，列表/归档统计保持中性，子任务状态不影响父徽标；
- 用户选定的 B 冷雾灰 `#F1F3F5` 与无自绘边框/圆角的外壳；仅由飞书气泡裁切四角，内部
  panel/button 边界不受影响。冷灰底衬左右各 2px、底部独立 3px 列 padding，侧衬在
  正文、分隔区和 Composer 间连续且不叠加，不携带 callback/confirm/disabled、非法
  height 或伪 CSS/shadow；最大五层容器（`column` 槽位不重复计层）。深色及已确认
  状态/操作配色保持不变；分隔区用平直纸面垫平原生分栏背景圆角，侧边无弧形缺口；
- 输入表单仍为 body 根元素，内部 Composer 同色纸面不改变 padding/spacing、字段名、
  提交字段或审批/停止回调；覆盖空卡、全部状态、历史、列表/归档与完成/失败提醒；
- CLI 重新连接、Enter 表单、首选项、进入会话、Claude 群聊、查看会话和 Desktop
  菜单主按钮的柔化覆盖；完整保留 callback/表单/confirm/disabled 等字段，不能
  因颜色重构改变提交路径；正文软标题保留状态与版本，避免原生 header 强制白字；
- 实时卡采用用户问题 → 全宽 Codex 回复 → 折叠历史 → 轮次导航 → Composer →
  次级会话控制的单列顺序；审批/输入中断区位于对话之前，历史轮不出现实时操作；
- Desktop 输入表单保持 `desktop_input` / `desktop_command__{thread_id}` /
  `desktop_send` 协议，提交按钮补齐 form 语义，普通 callback 位于 form 外；
- 从 `/desktop` 列表点击连接或从完成提醒点击 `NEXT / 重新连接` 时，原卡片必须被原地更新并成为
  后续实时更新目标；只有消息映射恢复或 CardKit 更新失败时才允许降级新建一张卡；
- 从旧版历史卡点击前/后轮或提交表单时，回调 `message_id` 必须使被点击卡先原地升级
  到新版主题并成为活动卡，后续更新不得落到另一张消息；
- `idle/running/waiting_approval/waiting_input/completed/failed/interrupted/unknown`
  状态矩阵中的 pill、提示面与停止按钮可见性；
- `/archived` 的独立分页、移出归档和恢复后连接；
- snapshot 与 Immer patches；
- 超过 256 MiB 无 snapshot 时的 rollout turn 基线 + patch-only 回退，包括有界反向 Query 恢复、active turn 安全归属和 history/current 同 ID 补丁去重；
- reasoning、命令输出和未知 schema 不进入飞书卡片；
- 本地 Markdown 图片在格式与 10 MiB 大小门禁后上传为真实 `img_key`，同一文件更新
  卡片时复用缓存；缺失、失败、不支持格式和远程 URL 均降级为 alt 文本，本机路径
  不进入卡片 JSON 或日志；
- 单图/多图使用 `img_combination` 缩略布局，长截图不再按正文全宽展开；验证单图
  `double`、双图 `double`、三图 `triple`、四图 `bisect`，并确认点击可查看原图；
- 所有主题 token 同时验证 light/dark 值，暗色下正文、次要文字、强调面和按钮均保持
  可读对比度，图片位图本身不做颜色反转；
- 飞书 chat 与 Desktop thread 的绑定、消息路由和卡片原地更新。

## 本机只读联调

1. 启动 ChatGPT Desktop，并确认 `~/.codex/ipc/ipc.sock` 存在。
2. 使用真实 thread id 执行 DesktopBridgeManager attach。
3. 验证 owner discovery 成功、rollout 基线可读、状态卡片可生成。
4. 不发送消息，detach 并确认 follower 连接正常关闭。

## 飞书端到端

1. 启动 `remote-claude lark start`。
2. 飞书发送 `/desktop`，确认 GPT-style Workspace 任务列表、项目名、完整 Session ID、
   状态文本、每页 5 条和翻页按钮正确，
   再选择一个非关键测试任务。
3. 验证同一张卡片持续更新，用户 Query、全宽 Codex 回复、折叠进度、轮次导航、
   Composer 和次级控制按单列顺序排列，PC/移动端和浅色/深色模式均可读。
4. 完成一个成功 turn 并构造一个失败 turn，验证完成/失败提醒的 `NEXT / 重新连接`
   整块区域可点击，且 Session 详情下方不再出现重复连接按钮。
5. 在长连接卡片中前后翻轮次，确认每页只有当轮 Query 和当轮 Agent 进度。
6. 验证 `/desktop` 不含归档任务；在 `/archived` 中分别测试“移出归档”和“恢复并进入”。
7. 分别验证空闲任务 start、运行中任务 steer、停止、命令审批、文件审批和用户输入；
   确认菜单/停止/断开按钮未被表单提交分支吞掉。
8. 查看历史轮时确认 live 审批、输入和停止操作不可见，但“继续当前任务”和断开仍可用。
9. 让 Codex 返回本地 PNG/JPEG/GIF/WebP 图片，确认卡片显示可点击预览的图片、
   采用紧凑缩略布局、连续更新不重复上传、本地路径不可见；再验证缺失图片和远程
   URL 安全降级，并分别检查浅色/深色与 PC/移动端。
10. 分别从 `/desktop` 列表和完成提醒点击连接，确认当前卡片原地变成实时会话卡、
    后续更新仍落在同一消息；重启客户端后再次点击旧完成提醒验证消息反查恢复。
11. Desktop 升级后先重复只读联调；协议不兼容时禁止写操作。
12. 主任务运行时启动多个子 Agent，确认详情状态区可展开且按本轮展示；子任务逐个
    完成时仅状态计数变化，不弹主任务完成提醒。主任务最后完成时只收到一次提醒。
13. 再次使用同一子 Agent，确认不会因 `interacted` 直接沿用旧“已完成”；检查失败、
    中断与状态暂不可读的文案。切换历史轮后，不应混入该子 Agent 后续新轮的状态。
14. 检查浅色 PC/移动端的冷雾灰纸面、左右 2px 与底部 3px 淡灰底衬：经过分隔线、
    原生输入表单、次级控制时侧边连续，四角无双线；深色不增加亮边。用原生预览转换
    检查实时卡、列表及完成提醒的嵌套合法性，并保留表单提交和审批/停止回归验证。
