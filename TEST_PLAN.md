# Codex Desktop Bridge 测试计划

## 自动化测试

```bash
UV_PYTHON=3.13 uv run --with pytest --with pytest-asyncio \
  python3 -m pytest -q \
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
- `/desktop` 列表的运行中、失败和空闲状态图标；
- snapshot 与 Immer patches；
- 超过 256 MiB 无 snapshot 时的 rollout + patch-only 回退；
- reasoning、命令输出和未知 schema 不进入飞书卡片；
- 飞书 chat 与 Desktop thread 的绑定、消息路由和卡片原地更新。

## 本机只读联调

1. 启动 ChatGPT Desktop，并确认 `~/.codex/ipc/ipc.sock` 存在。
2. 使用真实 thread id 执行 DesktopBridgeManager attach。
3. 验证 owner discovery 成功、rollout 基线可读、状态卡片可生成。
4. 不发送消息，detach 并确认 follower 连接正常关闭。

## 飞书端到端

1. 启动 `remote-claude lark start`。
2. 飞书发送 `/desktop`，确认项目名、完整 Session ID、每页 5 条和翻页按钮正确，
   再选择一个非关键测试任务。
3. 验证同一张卡片持续更新。
4. 分别验证空闲任务 start、运行中任务 steer、停止、命令审批、文件审批和用户输入。
5. Desktop 升级后先重复只读联调；协议不兼容时禁止写操作。
