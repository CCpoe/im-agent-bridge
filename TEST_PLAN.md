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
  tests/test_option_select.py
```

覆盖范围：

- IPC 小端长度帧、initialize、request correlation、超时和重连；
- owner discovery、follow、start/steer/interrupt、审批和用户输入；
- `/desktop` 列表的项目映射、完整 Session ID 和每页 5 条分页；
- `/desktop` 列表的运行中、失败和空闲状态文本；
- Session 完成通知的持久去重、失败重试和一键连接按钮；
- 长连接卡片按 turn 展示 Query/进度及历史翻页，翻页状态按 chat 隔离，历史轮隐藏 live pending/停止操作，成功发送后立即回到最新轮；
- Desktop 实时卡、任务列表、归档列表和完成通知的 Card JSON 2.0 GPT-style
  Workspace 基础契约：`compact_width=false`、公开摘要、等比分栏、标准折叠箭头，
  以及白色、`#CBC5FF`、`#C6D6FF`、`#3941FF` 四个最终主题色；
- 实时卡采用用户问题 → 全宽 Codex 回复 → 折叠历史 → 轮次导航 → Composer →
  次级会话控制的单列顺序；审批/输入中断区位于对话之前，历史轮不出现实时操作；
- Desktop 输入表单保持 `desktop_input` / `desktop_command__{thread_id}` /
  `desktop_send` 协议，提交按钮补齐 form 语义，普通 callback 位于 form 外；
- 从 `/desktop` 列表或完成提醒点击“连接 Session”时，原卡片必须被原地更新并成为
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
4. 完成一个成功 turn 并构造一个失败 turn，验证绿/红私聊提醒及“连接此 Session”。
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
