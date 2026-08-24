"""
飞书消息处理器 - 基于共享内存的推送架构

架构：
  Server → .mq 共享内存 → SharedMemoryPoller → 飞书卡片
  SessionBridge 只负责：连接管理 + 输入发送

群聊/私聊统一逻辑：以 chat_id 为 key 管理所有 bridge 和会话绑定。
"""

import asyncio
import json
import logging
import os as _os
import subprocess
import sys
import time
from datetime import datetime as _datetime
from pathlib import Path
from typing import Optional, Dict, Any, List

logger = logging.getLogger('LarkHandler')

from .session_bridge import SessionBridge
from .card_service import card_service
from .card_builder import (
    build_stream_card,
    build_status_card,
    build_help_card,
    build_dir_card,
    build_menu_card,
)
from .shared_memory_poller import SharedMemoryPoller, CardSlice
from .desktop_bridge import DesktopBridgeManager
from .desktop_card import build_desktop_list_card

sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.session import list_active_sessions, get_socket_path, get_chat_bindings_file, ensure_user_data_dir, USER_DATA_DIR


def _read_log_since(since: '_datetime', log_path: 'Path') -> str:
    """读取 startup.log 中 since 时间点之后的日志行"""
    if not log_path.exists():
        return ""
    lines = []
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ts = _datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S.%f")
            if ts >= since:
                lines.append(line)
        except ValueError:
            if lines:
                lines.append(line)
    return "\n".join(lines)

try:
    from stats import track as _track_stats
except Exception:
    def _track_stats(*args, **kwargs): pass


class LarkHandler:
    """飞书消息处理器（群聊/私聊统一逻辑）"""

    _CHAT_BINDINGS_FILE = get_chat_bindings_file()
    _OLD_CHAT_BINDINGS_FILE = Path("/tmp/remote-claude/lark_chat_bindings.json")
    _LARK_GROUP_IDS_FILE = Path(get_chat_bindings_file()).parent / "lark_group_ids.json"

    def __init__(self):
        # 兼容迁移：旧绑定文件存在而新路径不存在时，自动迁移
        if not self._CHAT_BINDINGS_FILE.exists() and self._OLD_CHAT_BINDINGS_FILE.exists():
            try:
                import shutil
                ensure_user_data_dir()
                shutil.move(str(self._OLD_CHAT_BINDINGS_FILE), str(self._CHAT_BINDINGS_FILE))
            except Exception as e:
                logger.warning(f"迁移旧绑定文件失败: {e}")
        # chat_id → SessionBridge（活跃连接）
        self._bridges: Dict[str, SessionBridge] = {}
        # chat_id → session_name（当前连接状态）
        self._chat_sessions: Dict[str, str] = {}
        # 共享内存轮询器
        self._poller = SharedMemoryPoller(card_service)
        # chat_id → session_name 持久化绑定（重启后自动恢复）
        self._chat_bindings: Dict[str, str] = self._load_chat_bindings()
        # 专属群聊 chat_id 集合（仅包含通过 /new-group 创建的群）
        self._group_chat_ids: set = self._load_group_chat_ids()
        # chat_id → CardSlice（用户主动断开后保留，供重连时冻结旧卡片）
        self._detached_slices: Dict[str, CardSlice] = {}
        # 正在启动中的会话名集合（防止并发点击触发竞态）
        self._starting_sessions: set = set()
        # Session 健康检查后台任务
        self._health_check_task: Optional[asyncio.Task] = None
        # Codex Desktop owner/follower IPC 桥接（延迟连接）
        self._desktop = DesktopBridgeManager(card_service)
        self._desktop_start_task: Optional[asyncio.Task] = None
        self._processed_message_ids: Dict[str, float] = {}
        logger.info(
            f"LarkHandler 初始化: bindings={len(self._chat_bindings)}, "
            f"groups={len(self._group_chat_ids)}, "
            f"binding_details={{{', '.join(f'{k[:8]}...→{v}' for k, v in self._chat_bindings.items())}}}"
        )

    # ── 持久化绑定 ──────────────────────────────────────────────────────────

    def _load_json_file(self, path: Path):
        """原子读取 JSON 文件，损坏时尝试从 .tmp 备份恢复"""
        try:
            if path.exists():
                content = path.read_text()
                if content.strip():
                    return json.loads(content)
        except json.JSONDecodeError as e:
            logger.warning(f"文件 {path.name} JSON 损坏: {e}，尝试恢复备份")
            tmp_path = path.with_suffix('.json.tmp')
            if tmp_path.exists():
                try:
                    content = tmp_path.read_text()
                    if content.strip():
                        result = json.loads(content)
                        logger.warning(f"从 {tmp_path.name} 恢复成功")
                        return result
                except Exception:
                    pass
        except Exception:
            pass
        return None

    def _atomic_write_json(self, path: Path, data):
        """原子写入 JSON 文件：先写 .tmp 再 rename"""
        try:
            ensure_user_data_dir()
            text = json.dumps(data, ensure_ascii=False)
            tmp_path = path.with_suffix('.json.tmp')
            tmp_path.write_text(text)
            tmp_path.rename(path)
        except Exception as e:
            logger.warning(f"保存 {path.name} 失败: {e}")

    def _load_chat_bindings(self) -> Dict[str, str]:
        result = self._load_json_file(self._CHAT_BINDINGS_FILE)
        return result if result is not None else {}

    def _save_chat_bindings(self):
        self._atomic_write_json(self._CHAT_BINDINGS_FILE, self._chat_bindings)

    def _load_group_chat_ids(self) -> set:
        result = self._load_json_file(self._LARK_GROUP_IDS_FILE)
        return set(result) if result is not None else set()

    def _save_group_chat_ids(self):
        self._atomic_write_json(self._LARK_GROUP_IDS_FILE, list(self._group_chat_ids))

    def _remove_binding_by_chat(self, chat_id: str, force: bool = False):
        """移除 chat_id 的绑定。
        群聊绑定默认不移除（避免断开后无法解散群）；
        force=True 时强制移除（用于会话终止/解散群场景）。
        """
        if not force and chat_id in self._group_chat_ids:
            return
        session = self._chat_bindings.get(chat_id)
        if session is not None:
            logger.info(f"移除绑定: chat_id={chat_id[:8]}..., session={session}, force={force}")
        self._chat_bindings.pop(chat_id, None)
        self._save_chat_bindings()

    # ── 统一 attach / detach / on_disconnect ────────────────────────────────

    async def _attach(self, chat_id: str, session_name: str,
                      user_id: Optional[str] = None) -> bool:
        """统一 attach 逻辑（私聊/群聊共用）"""
        self._ensure_health_check_started()
        # 同一个飞书聊天只能绑定一种后端。
        desktop = getattr(self, "_desktop", None)
        desktop_was_attached = bool(
            desktop is not None and desktop.is_attached(chat_id)
        )
        logger.info(
            f"[ATTACH] 开始: chat_id={chat_id[:8]}..., session={session_name}, "
            f"old_session={self._chat_sessions.get(chat_id)}, "
            f"old_bridge={'有' if chat_id in self._bridges else '无'}"
        )
        # 在断开旧连接之前，先更新旧流式卡片为已断开状态
        old_session = self._chat_sessions.get(chat_id)
        old_slice = self._poller.stop_and_get_active_slice(chat_id)
        if old_slice and old_session:
            await self._update_card_disconnected(chat_id, old_session, old_slice)

        # 断开旧 bridge
        old = self._bridges.pop(chat_id, None)
        if old:
            await old.disconnect()
        # _poller.stop 已通过 stop_and_get_active_slice 完成
        self._chat_sessions.pop(chat_id, None)
        self._detached_slices.pop(chat_id, None)

        def on_disconnect(disconnected_bridge=None):
            asyncio.create_task(self._on_disconnect(chat_id, session_name,
                                                    disconnected_bridge=disconnected_bridge))

        bridge = SessionBridge(session_name, on_disconnect=on_disconnect)
        if await bridge.connect():
            if desktop_was_attached:
                await desktop.detach(chat_id)
            self._bridges[chat_id] = bridge
            self._chat_sessions[chat_id] = session_name
            self._poller.start(chat_id, session_name, is_group=(chat_id in self._group_chat_ids),
                               notify_user_id=user_id)
            _track_stats('lark', 'attach', session_name=session_name,
                         chat_id=chat_id)
            logger.info(f"[ATTACH] 成功: chat_id={chat_id[:8]}..., session={session_name}")
            return True
        logger.warning(f"[ATTACH] 失败: chat_id={chat_id[:8]}..., session={session_name}")
        return False

    async def _detach(self, chat_id: str):
        """统一 detach 逻辑（私聊/群聊共用）"""
        session_name = self._chat_sessions.get(chat_id, "未知")
        logger.info(f"[DETACH] chat_id={chat_id[:8]}..., session={session_name}")
        bridge = self._bridges.pop(chat_id, None)
        if bridge:
            await bridge.disconnect()
        self._chat_sessions.pop(chat_id, None)
        self._poller.stop(chat_id)

    async def _on_disconnect(self, chat_id: str, session_name: str,
                              disconnected_bridge=None):
        """服务端关闭连接时的统一处理"""
        # 防竞态：若当前 bridge 已被 _attach 替换为新实例，跳过清理
        current_bridge = self._bridges.get(chat_id)
        if disconnected_bridge is not None and current_bridge is not disconnected_bridge:
            logger.info(f"会话 '{session_name}' 旧连接断线（已被替换），跳过清理")
            return

        bridge_running = disconnected_bridge.running if disconnected_bridge else "N/A"
        logger.info(f"会话 '{session_name}' 断线, chat_id={chat_id[:8]}..., bridge_running={bridge_running}")
        logger.info(
            f"[DISCONNECT] 状态快照: bridges={[k[:8] for k in self._bridges]}, "
            f"sessions={{{', '.join(f'{k[:8]}→{v}' for k, v in self._chat_sessions.items())}}}"
        )
        _track_stats('lark', 'disconnect', session_name=session_name,
                     chat_id=chat_id)
        active_slice = self._poller.stop_and_get_active_slice(chat_id)
        self._bridges.pop(chat_id, None)
        self._chat_sessions.pop(chat_id, None)
        self._detached_slices.pop(chat_id, None)
        # 注意：不清除持久化绑定，socket 断连是临时状态，下次操作可自动恢复
        # 只有用户主动 /detach 或 /kill 时才清除绑定

        if active_slice:
            updated = await self._update_card_disconnected(chat_id, session_name, active_slice)
            if not updated:
                # 就地更新失败 → 降级发新卡片，确保用户看到断开状态
                logger.warning(f"断线卡片就地更新失败，降级发送新卡: {chat_id[:8]}...")
                card = build_stream_card([], disconnected=True, session_name=session_name)
                await card_service.create_and_send_card(chat_id, card)

    # ── 消息入口 ────────────────────────────────────────────────────────────

    async def handle_message(self, user_id: str, chat_id: str, text: str,
                              chat_type: str = "p2p", message_id: Optional[str] = None):
        """处理用户消息（群聊/私聊统一路由）"""
        if message_id:
            if message_id in self._processed_message_ids:
                logger.info("忽略重复飞书消息: %s", message_id)
                return
            self._processed_message_ids[message_id] = time.time()
            if len(self._processed_message_ids) > 2048:
                oldest = sorted(
                    self._processed_message_ids,
                    key=self._processed_message_ids.get,
                )[:512]
                for old_id in oldest:
                    self._processed_message_ids.pop(old_id, None)
        self._ensure_health_check_started()
        logger.info(f"收到消息: user={user_id[:8]}..., chat={chat_id[:8]}..., type={chat_type}, text={text[:50]}")
        text = text.strip()

        if text.startswith("/"):
            # /cl 前缀：去掉前缀，转发给 Claude
            if text == "/cl" or text.startswith("/cl "):
                claude_text = text[3:].strip()
                if claude_text:
                    await self._forward_to_claude(
                        user_id, chat_id, claude_text, client_message_id=message_id
                    )
                    _track_stats('lark', 'message',
                                 session_name=self._chat_sessions.get(chat_id, ''),
                                 chat_id=chat_id)
            else:
                await self._handle_command(user_id, chat_id, text)
        elif getattr(self, "_desktop", None) is not None and self._desktop.is_attached(chat_id):
            # Desktop 模式下，普通飞书消息即为 Desktop 用户消息。
            await self._forward_to_claude(
                user_id, chat_id, text, client_message_id=message_id
            )
            _track_stats('lark', 'desktop_message',
                         session_name=self._desktop.binding_for(chat_id) or '',
                         chat_id=chat_id)
        # CLI 模式保留原行为：普通消息不转发，使用卡片输入框或 /cl。

    async def forward_to_claude(self, user_id: str, chat_id: str, text: str):
        """卡片输入框直通 Claude（跳过命令路由）"""
        await self._forward_to_claude(user_id, chat_id, text)
        _track_stats('lark', 'message',
                     session_name=self._chat_sessions.get(chat_id, ''),
                     chat_id=chat_id)

    async def forward_to_desktop(
        self, user_id: str, chat_id: str, expected_thread_id: str, text: str
    ):
        """从带 thread id 的 Desktop 卡片发送消息，拒绝过期卡片。"""
        if self._desktop.binding_for(chat_id) != expected_thread_id:
            await card_service.send_text(chat_id, "该卡片已过期，请使用 /desktop 刷新")
            return
        if not await self._desktop.send_message(chat_id, text):
            await card_service.send_text(chat_id, "发送到 Codex Desktop 失败，请刷新状态后重试")

    async def _handle_command(self, user_id: str, chat_id: str, text: str):
        """处理命令（群聊/私聊共用同一逻辑）"""
        parts = text.split(maxsplit=1)
        command = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        _track_stats('lark', 'cmd',
                     session_name=self._chat_sessions.get(chat_id, ''),
                     chat_id=chat_id, detail=command)

        if command == "/attach":
            await self._cmd_attach(user_id, chat_id, args)
        elif command in ("/desktop", "/desktop-list"):
            if command == "/desktop" and args.strip():
                await self._cmd_desktop_attach(user_id, chat_id, args)
            else:
                await self._cmd_desktop_list(user_id, chat_id)
        elif command == "/desktop-detach":
            await self._cmd_desktop_detach(user_id, chat_id)
        elif command in ("/desktop-stop", "/stop"):
            await self._cmd_desktop_stop(user_id, chat_id)
        elif command == "/desktop-status":
            await self._cmd_desktop_status(user_id, chat_id)
        elif command == "/detach":
            await self._cmd_detach(user_id, chat_id)
        elif command == "/list":
            await self._cmd_list(user_id, chat_id)
        elif command == "/status":
            await self._cmd_status(user_id, chat_id)
        elif command == "/start":
            await self._cmd_start(user_id, chat_id, args)
        elif command == "/kill":
            await self._cmd_kill(user_id, chat_id, args)
        elif command in ("/ls", "/tree"):
            await self._cmd_ls(user_id, chat_id, args, tree=(command == "/tree"))
        elif command == "/new-group":
            await self._cmd_new_group(user_id, chat_id, args)
        elif command == "/help":
            await self._cmd_help(user_id, chat_id)
        elif command == "/menu":
            await self._cmd_menu(user_id, chat_id)
        elif command == "/press":
            await self._cmd_press(user_id, chat_id, args)
        else:
            await card_service.send_text(chat_id, f"未知命令: {command}\n使用 /help 查看帮助")

    # ── 命令处理 ─────────────────────────────────────────────────────────────

    async def _cmd_attach(self, user_id: str, chat_id: str, args: str,
                          message_id: Optional[str] = None):
        """连接到会话"""
        session_name = args.strip()

        if not session_name:
            await self._cmd_list(user_id, chat_id, message_id=message_id)
            return

        sessions = list_active_sessions()
        if not any(s["name"] == session_name for s in sessions):
            await card_service.send_text(
                chat_id, f"会话 '{session_name}' 不存在，使用 /list 查看可用会话"
            )
            return

        ok = await self._attach(chat_id, session_name, user_id=user_id)
        if ok:
            logger.info(f"[BINDING] 写入: chat_id={chat_id[:8]}..., session={session_name}, source=cmd_attach")
            self._chat_bindings[chat_id] = session_name
            self._save_chat_bindings()
            if message_id:
                await self._cmd_list(user_id, chat_id, message_id=message_id)
        else:
            await card_service.send_text(chat_id, f"❌ 无法连接到会话 '{session_name}'")

    async def _cmd_detach(self, user_id: str, chat_id: str,
                           message_id: Optional[str] = None):
        """断开会话"""
        if self._desktop.is_attached(chat_id):
            await self._cmd_desktop_detach(user_id, chat_id, message_id=message_id)
            return
        if chat_id not in self._bridges and chat_id not in self._chat_sessions:
            await card_service.send_text(chat_id, "当前未连接到任何会话")
            return
        self._remove_binding_by_chat(chat_id)
        await self._detach(chat_id)
        await self._cmd_menu(user_id, chat_id, message_id=message_id)

    async def _cmd_list(self, user_id: str, chat_id: str,
                         message_id: Optional[str] = None):
        """列出会话（等价于菜单）"""
        await self._cmd_menu(user_id, chat_id, message_id=message_id)

    async def _cmd_status(self, user_id: str, chat_id: str):
        """显示状态"""
        desktop_state = self._desktop.state_for_chat(chat_id)
        if desktop_state is not None:
            await self._desktop.refresh_card(chat_id)
            return
        session_name = self._chat_sessions.get(chat_id)
        bridge = self._bridges.get(chat_id)
        if bridge and bridge.running and session_name:
            card = build_status_card(True, session_name)
        else:
            card = build_status_card(False)
        card_id = await card_service.create_card(card)
        if card_id:
            await card_service.send_card(chat_id, card_id)

    async def _cmd_desktop_list(
        self, user_id: str, chat_id: str, message_id: Optional[str] = None,
        page: int = 0,
    ):
        """列出 Codex Desktop 最近任务并提供进入按钮。"""
        if not await self._desktop.start():
            await card_service.send_text(
                chat_id,
                "无法连接 Codex Desktop。请确认 ChatGPT Desktop 已启动，且 ~/.codex/ipc/ipc.sock 可用。",
            )
            return
        threads = await asyncio.to_thread(self._desktop.list_threads, None)
        card = build_desktop_list_card(
            threads,
            current_thread_id=self._desktop.binding_for(chat_id),
            page=page,
        )
        await self._send_or_update_card(chat_id, card, message_id)

    async def _cmd_desktop_attach(
        self,
        user_id: str,
        chat_id: str,
        args: str,
        message_id: Optional[str] = None,
    ):
        """将飞书聊天绑定到现有 Codex Desktop 任务。"""
        thread_id = args.strip()
        if not thread_id:
            await self._cmd_desktop_list(user_id, chat_id, message_id=message_id)
            return

        had_cli_binding = chat_id in self._bridges or chat_id in self._chat_sessions
        ok = await self._desktop.attach(chat_id, user_id, thread_id)
        if not ok and sys.platform == "darwin":
            clean_id = thread_id.removeprefix("codex://threads/").strip()
            if clean_id and "/" not in clean_id:
                try:
                    subprocess.Popen(
                        ["open", f"codex://threads/{clean_id}"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    await asyncio.sleep(1.5)
                    ok = await self._desktop.attach(chat_id, user_id, clean_id)
                except Exception:
                    logger.debug("唤起 Desktop 任务后重试失败", exc_info=True)
        if ok and had_cli_binding:
            self._remove_binding_by_chat(chat_id, force=True)
            await self._detach(chat_id)
        if not ok:
            await card_service.send_text(
                chat_id,
                "无法连接该 Desktop 任务。请确认任务正在本机 Desktop 中打开，或使用 /desktop 查看可用任务。",
            )

    async def _cmd_desktop_detach(
        self, user_id: str, chat_id: str, message_id: Optional[str] = None,
        expected_thread_id: Optional[str] = None,
    ):
        """停止跟随 Desktop 任务，不停止 Desktop 中的工作。"""
        thread_id = self._desktop.binding_for(chat_id)
        if not thread_id:
            await card_service.send_text(chat_id, "当前未连接 Desktop 任务")
            return
        if expected_thread_id and expected_thread_id != thread_id:
            await card_service.send_text(chat_id, "该卡片已过期，请使用 /desktop 刷新")
            return
        await self._desktop.detach(chat_id)
        await card_service.send_text(chat_id, "已断开 Desktop 任务监控；Desktop 中的任务仍会继续运行。")

    async def _cmd_desktop_stop(
        self, user_id: str, chat_id: str,
        expected_thread_id: Optional[str] = None,
        expected_turn_id: Any = None,
    ):
        """中断当前 Desktop turn。"""
        thread_id = self._desktop.binding_for(chat_id)
        if not thread_id:
            await card_service.send_text(chat_id, "当前未连接 Desktop 任务")
            return
        if expected_thread_id and expected_thread_id != thread_id:
            await card_service.send_text(chat_id, "该卡片已过期，请使用 /desktop 刷新")
            return
        if not await self._desktop.interrupt(
            chat_id, expected_turn_id=expected_turn_id
        ):
            await card_service.send_text(chat_id, "停止 Desktop 任务失败，请在 Desktop 中检查状态")

    async def _cmd_desktop_status(self, user_id: str, chat_id: str):
        """刷新当前 Desktop 状态卡片。"""
        state = self._desktop.state_for_chat(chat_id)
        if state is None:
            await card_service.send_text(chat_id, "当前未连接 Desktop 任务")
            return
        await self._desktop.refresh_card(chat_id)

    async def handle_desktop_approval(
        self, user_id: str, chat_id: str, kind: str, request_id: Any, decision: Any,
        expected_thread_id: Optional[str] = None,
    ):
        """处理 Desktop 审批卡片按钮。"""
        if expected_thread_id and self._desktop.binding_for(chat_id) != expected_thread_id:
            await card_service.send_text(chat_id, "该审批卡片已过期，请刷新 Desktop 状态")
            return
        ok = await self._desktop.handle_approval(chat_id, kind, request_id, decision)
        if not ok:
            await card_service.send_text(chat_id, "审批提交失败或请求已失效，请刷新 Desktop 状态")

    async def handle_desktop_input(
        self,
        user_id: str,
        chat_id: str,
        kind: str,
        request_id: Any,
        answer: str,
        question_id: Optional[str] = None,
        expected_thread_id: Optional[str] = None,
    ):
        """处理 Desktop 用户输入/选项按钮。"""
        if expected_thread_id and self._desktop.binding_for(chat_id) != expected_thread_id:
            await card_service.send_text(chat_id, "该输入卡片已过期，请刷新 Desktop 状态")
            return
        ok = await self._desktop.handle_input(
            chat_id, kind, request_id, answer, question_id=question_id
        )
        if not ok:
            await card_service.send_text(chat_id, "输入提交失败或请求已失效，请刷新 Desktop 状态")

    async def _cmd_start(self, user_id: str, chat_id: str, args: str, cli_type: str = "claude"):
        """启动新会话"""
        parts = args.strip().split(maxsplit=1)
        if not parts:
            await card_service.send_text(
                chat_id,
                "用法: /start <会话名> [工作路径]\n\n"
                "示例:\n"
                "  /start mywork ~/dev/myproject\n"
                "  /start test ~/dev/myproject"
            )
            return

        session_name = parts[0]
        work_dir = parts[1] if len(parts) > 1 else None

        if work_dir:
            work_path = Path(work_dir).expanduser()
            if not work_path.exists():
                await card_service.send_text(chat_id, f"错误: 路径不存在: {work_dir}")
                return
            if not work_path.is_dir():
                await card_service.send_text(chat_id, f"错误: 不是目录: {work_dir}")
                return
            work_dir = str(work_path.absolute())

        sessions = list_active_sessions()
        if any(s["name"] == session_name for s in sessions):
            await card_service.send_text(
                chat_id,
                f"错误: 会话 '{session_name}' 已存在\n使用 /attach {session_name} 连接"
            )
            return

        if session_name in self._starting_sessions:
            await card_service.send_text(chat_id, f"会话 '{session_name}' 正在启动中，请稍候")
            return
        self._starting_sessions.add(session_name)

        script_dir = Path(__file__).parent.parent.absolute()
        server_script = script_dir / "server" / "server.py"
        cmd = ["uv", "run", "--project", str(script_dir), "python3", str(server_script), session_name]
        if cli_type == "codex":
            cmd += ["--cli-type", "codex"]
        if self._poller.get_bypass_enabled():
            if cli_type == "codex":
                cmd += ["--", "--dangerously-bypass-approvals-and-sandbox"]
            else:
                cmd += ["--", "--dangerously-skip-permissions", "--permission-mode=dontAsk"]

        logger.info(f"启动会话: {session_name}, 工作目录: {work_dir}, cli_type: {cli_type}, 命令: {' '.join(cmd)}")
        _track_stats('lark', 'cmd_start', session_name=session_name, chat_id=chat_id)

        try:
            env = _os.environ.copy()
            env.pop("CLAUDECODE", None)

            log_path = USER_DATA_DIR / "startup.log"
            start_time = _datetime.now()

            with open(log_path, 'a') as stderr_fd:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_fd,
                    start_new_session=True,
                    cwd=work_dir,
                    env=env,
                )

            socket_path = get_socket_path(session_name)
            for i in range(120):
                await asyncio.sleep(0.1)
                if socket_path.exists():
                    break
                if (i + 1) % 10 == 0:
                    elapsed = (i + 1) // 10
                    rc = proc.poll()
                    if rc is not None:
                        log_content = _read_log_since(start_time, log_path)
                        logger.warning(f"会话启动失败: server 进程已退出 (exitcode={rc}, elapsed={elapsed}s)\n{log_content}")
                        await card_service.send_text(chat_id, f"错误: Server 进程意外退出 (code={rc})\n\n{log_content}")
                        return
                    logger.info(f"等待 server socket... ({elapsed}s)")
            else:
                log_content = _read_log_since(start_time, log_path)
                logger.error(f"会话启动超时 (12s), session={session_name}\n{log_content}")
                await card_service.send_text(chat_id, f"错误: 会话启动超时 (12s)\n\n{log_content}")
                return

            ok = await self._attach(chat_id, session_name, user_id=user_id)
            if ok:
                logger.info(f"[BINDING] 写入: chat_id={chat_id[:8]}..., session={session_name}, source=cmd_start")
                self._chat_bindings[chat_id] = session_name
                self._save_chat_bindings()
            else:
                await card_service.send_text(
                    chat_id,
                    f"会话已启动但连接失败\n使用 /attach {session_name} 重试"
                )

        except Exception as e:
            logger.error(f"启动会话失败: {e}")
            await card_service.send_text(chat_id, f"错误: 启动失败 - {e}")
        finally:
            self._starting_sessions.discard(session_name)

    async def _cmd_start_and_new_group(self, user_id: str, chat_id: str,
                                       session_name: str, path: str, cli_type: str = "claude"):
        """在指定目录启动会话并创建专属群聊"""
        work_path = Path(path).expanduser()
        if not work_path.is_dir():
            await card_service.send_text(chat_id, f"错误: 路径无效: {path}")
            return

        sessions = list_active_sessions()
        active_names = {s["name"] for s in sessions}
        if session_name in active_names or session_name in self._starting_sessions:
            session_name = f"{session_name}_{_datetime.now().strftime('%m%d_%H%M%S')}"

        self._starting_sessions.add(session_name)

        work_dir = str(work_path.absolute())
        script_dir = Path(__file__).parent.parent.absolute()
        server_script = script_dir / "server" / "server.py"
        cmd = ["uv", "run", "--project", str(script_dir), "python3", str(server_script), session_name]
        if cli_type == "codex":
            cmd += ["--cli-type", "codex"]
        if self._poller.get_bypass_enabled():
            if cli_type == "codex":
                cmd += ["--", "--dangerously-bypass-approvals-and-sandbox"]
            else:
                cmd += ["--", "--dangerously-skip-permissions", "--permission-mode=dontAsk"]

        try:
            env = _os.environ.copy()
            env.pop("CLAUDECODE", None)

            log_path = USER_DATA_DIR / "startup.log"
            start_time = _datetime.now()

            with open(log_path, 'a') as stderr_fd:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=stderr_fd,
                    start_new_session=True, cwd=work_dir, env=env,
                )

            socket_path = get_socket_path(session_name)
            for i in range(120):
                await asyncio.sleep(0.1)
                if socket_path.exists():
                    break
                if (i + 1) % 10 == 0:
                    elapsed = (i + 1) // 10
                    rc = proc.poll()
                    if rc is not None:
                        log_content = _read_log_since(start_time, log_path)
                        logger.warning(f"启动并创建群聊失败: server 进程已退出 (exitcode={rc}, elapsed={elapsed}s)\n{log_content}")
                        await card_service.send_text(chat_id, f"错误: Server 进程意外退出 (code={rc})\n\n{log_content}")
                        return
            else:
                log_content = _read_log_since(start_time, log_path)
                logger.error(f"启动并创建群聊超时 (12s), session={session_name}\n{log_content}")
                await card_service.send_text(chat_id, f"错误: 会话启动超时 (12s)\n\n{log_content}")
                return

            await self._cmd_new_group(user_id, chat_id, session_name)

        except Exception as e:
            logger.error(f"启动并创建群聊失败: {e}")
            await card_service.send_text(chat_id, f"操作失败：{e}")
        finally:
            self._starting_sessions.discard(session_name)

    async def _cmd_kill(self, user_id: str, chat_id: str, args: str,
                        message_id: Optional[str] = None):
        """终止会话"""
        from utils.session import cleanup_session, tmux_session_exists, tmux_kill_session

        session_name = args.strip()
        if not session_name:
            await card_service.send_text(chat_id, "用法: /kill <会话名>")
            return

        sessions = list_active_sessions()
        if not any(s["name"] == session_name for s in sessions):
            await card_service.send_text(chat_id, f"错误: 会话 '{session_name}' 不存在")
            return

        # 解散绑定该会话的专属群聊（必须在断开连接之前，否则 _chat_bindings 已被清除）
        for cid in list(self._group_chat_ids):
            if self._chat_bindings.get(cid) == session_name:
                ok, err = await self._disband_group_via_api(cid)
                if not ok:
                    logger.warning(f"关闭会话时解散群 {cid} 失败: {err}")

        # 断开所有连接到此会话的 chat
        for cid, sname in list(self._chat_sessions.items()):
            if sname == session_name:
                active_slice = self._poller.stop_and_get_active_slice(cid)
                if active_slice:
                    await self._update_card_disconnected(cid, sname, active_slice)
                await self._detach(cid)
                self._remove_binding_by_chat(cid, force=True)

        # 清理所有残留绑定（包括已断开的群聊，其绑定在断开时被保留）
        for cid in [c for c, s in list(self._chat_bindings.items()) if s == session_name]:
            logger.info(f"[KILL] 清理残留绑定: chat_id={cid[:8]}..., session={session_name}, is_group={'是' if cid in self._group_chat_ids else '否'}")
            self._group_chat_ids.discard(cid)
            self._chat_bindings.pop(cid, None)
        self._save_chat_bindings()
        self._save_group_chat_ids()

        if tmux_session_exists(session_name):
            tmux_kill_session(session_name)
        cleanup_session(session_name)

        await card_service.send_text(chat_id, f"✅ 会话 '{session_name}' 已终止")
        await self._cmd_list(user_id, chat_id, message_id=message_id)

    async def _handle_list_detach(self, user_id: str, chat_id: str,
                                   message_id: Optional[str] = None):
        """会话列表卡片中断开连接，就地刷新列表"""
        session_name = self._chat_sessions.get(chat_id, "")
        # 更新流式卡片为已断开状态
        active_slice = self._poller.stop_and_get_active_slice(chat_id)
        if active_slice and session_name:
            await self._update_card_disconnected(chat_id, session_name, active_slice)

        self._remove_binding_by_chat(chat_id)
        await self._detach(chat_id)   # bridge.disconnect + _poller.stop（幂等）
        await self._cmd_list(user_id, chat_id, message_id=message_id)

    async def _update_card_disconnected(self, chat_id: str, session_name: str,
                                        active_slice: 'CardSlice') -> bool:
        """读取最新 blocks 并就地更新卡片为断开状态（disconnected=True）。返回是否成功。"""
        blocks = []
        try:
            import sys as _sys
            _sys.path.insert(0, str(Path(__file__).parent.parent / "server"))
            from shared_state import SharedStateReader, get_mq_path
            mq_path = get_mq_path(session_name)
            if mq_path.exists():
                reader = SharedStateReader(session_name)
                state = reader.read()
                reader.close()
                blocks = state.get("blocks", [])
        except Exception:
            pass
        blocks_slice = blocks[active_slice.start_idx:]
        card = build_stream_card(blocks_slice, disconnected=True, session_name=session_name)
        try:
            return await card_service.update_card(
                card_id=active_slice.card_id,
                sequence=active_slice.sequence + 1,
                card_content=card,
            )
        except Exception as e:
            logger.warning(f"_update_card_disconnected 失败 ({chat_id[:8]}...): {e}")
            return False

    async def _handle_stream_detach(self, user_id: str, chat_id: str,
                                     session_name: str, message_id: Optional[str] = None):
        """流式卡片中断开连接，就地更新卡片为已断开状态"""
        # 停止轮询并获取活跃 CardSlice（原子操作）
        active_slice = self._poller.stop_and_get_active_slice(chat_id)

        # 读取最后快照的 blocks
        blocks = []
        try:
            import sys as _sys
            _sys.path.insert(0, str(Path(__file__).parent.parent / "server"))
            from shared_state import SharedStateReader, get_mq_path
            mq_path = get_mq_path(session_name)
            if mq_path.exists():
                reader = SharedStateReader(session_name)
                state = reader.read()
                reader.close()
                blocks = state.get("blocks", [])
        except Exception:
            pass

        self._remove_binding_by_chat(chat_id)
        # _detach 中 _poller.stop() 幂等（已调用 stop_and_get_active_slice）
        await self._detach(chat_id)

        blocks_slice = blocks[active_slice.start_idx:] if active_slice else blocks
        card = build_stream_card(blocks_slice, disconnected=True, session_name=session_name)

        updated = False
        if active_slice:
            try:
                success = await card_service.update_card(
                    card_id=active_slice.card_id,
                    sequence=active_slice.sequence + 1,
                    card_content=card,
                )
                if success:
                    active_slice.sequence += 1
                    self._detached_slices[chat_id] = active_slice
                    updated = True
            except Exception as e:
                logger.warning(f"_handle_stream_detach 就地更新失败: {e}")

        if not updated:
            await self._send_or_update_card(chat_id, card, message_id)

    async def _handle_stream_reconnect(self, user_id: str, chat_id: str,
                                       session_name: str, message_id: Optional[str] = None):
        """流式卡片中重新连接：冻结旧断开卡片 → 重新 attach"""
        # 冻结旧断开卡片
        old_slice = self._detached_slices.pop(chat_id, None)
        if old_slice:
            try:
                frozen_card = build_stream_card([], is_frozen=True, session_name=session_name)
                await card_service.update_card(
                    card_id=old_slice.card_id,
                    sequence=old_slice.sequence + 1,
                    card_content=frozen_card,
                )
            except Exception as e:
                logger.warning(f"_handle_stream_reconnect 冻结旧卡片失败: {e}")
        elif message_id:
            try:
                frozen_card = build_stream_card([], is_frozen=True, session_name=session_name)
                await card_service.update_card_by_message_id(message_id, frozen_card)
            except Exception as e:
                logger.warning(f"_handle_stream_reconnect 按 message_id 冻结失败: {e}")

        await self._cmd_attach(user_id, chat_id, session_name)

    async def _cmd_help(self, user_id: str, chat_id: str,
                         message_id: Optional[str] = None):
        """显示帮助"""
        card = build_help_card()
        await self._send_or_update_card(chat_id, card, message_id)

    async def _cmd_menu(self, user_id: str, chat_id: str,
                         message_id: Optional[str] = None, page: int = 0):
        """显示快捷操作菜单（内嵌会话列表）"""
        sessions = list_active_sessions()
        current = self._chat_sessions.get(chat_id)
        session_groups = {
            self._chat_bindings[cid]: cid
            for cid in self._group_chat_ids
            if cid in self._chat_bindings
        }
        card = build_menu_card(sessions, current_session=current, session_groups=session_groups, page=page,
                               notify_enabled=self._poller.get_notify_enabled(),
                               urgent_enabled=self._poller.get_urgent_enabled(),
                               bypass_enabled=self._poller.get_bypass_enabled(),
                               desktop_available=(Path.home() / ".codex/ipc/ipc.sock").exists(),
                               desktop_connected=self._desktop.is_attached(chat_id))
        await self._send_or_update_card(chat_id, card, message_id)

    async def _cmd_toggle_notify(self, user_id: str, chat_id: str,
                                  message_id: Optional[str] = None):
        """切换就绪通知开关并刷新菜单卡片"""
        new_value = not self._poller.get_notify_enabled()
        self._poller.set_notify_enabled(new_value)
        await self._cmd_menu(user_id, chat_id, message_id=message_id)

    async def _cmd_toggle_urgent(self, user_id: str, chat_id: str,
                                  message_id: Optional[str] = None):
        """切换加急通知开关并刷新菜单卡片"""
        new_value = not self._poller.get_urgent_enabled()
        self._poller.set_urgent_enabled(new_value)
        await self._cmd_menu(user_id, chat_id, message_id=message_id)

    async def _cmd_toggle_bypass(self, user_id: str, chat_id: str,
                                  message_id: Optional[str] = None):
        """切换新会话 bypass 开关并刷新菜单卡片"""
        new_value = not self._poller.get_bypass_enabled()
        self._poller.set_bypass_enabled(new_value)
        await self._cmd_menu(user_id, chat_id, message_id=message_id)

    async def _cmd_ls(self, user_id: str, chat_id: str, args: str,
                       tree: bool = False, message_id: Optional[str] = None, page: int = 0):
        """查看目录文件结构"""
        all_sessions = list_active_sessions()
        sessions_info = []
        for s in all_sessions:
            pid = s.get("pid")
            cwd = self._get_pid_cwd(pid) if pid else None
            sessions_info.append({"name": s["name"], "cwd": cwd or ""})

        bound_session = self._chat_sessions.get(chat_id)
        if bound_session:
            pid = next((s.get("pid") for s in all_sessions if s["name"] == bound_session), None)
            session_cwd = self._get_pid_cwd(pid) if pid else None
            root = Path(session_cwd) if session_cwd else Path.home()
        else:
            root = Path.home()

        target_arg = args.strip()
        if target_arg:
            target = Path(target_arg).expanduser()
            if not target.is_absolute():
                target = root / target
        else:
            target = root

        target = target.resolve()
        if not target.exists():
            await card_service.send_text(chat_id, f"路径不存在：{target}")
            return

        try:
            if tree:
                entries = self._collect_tree_entries(target)
            else:
                entries = self._collect_ls_entries(target)
        except Exception as e:
            await card_service.send_text(chat_id, f"读取目录失败：{e}")
            return

        session_groups = {
            self._chat_bindings[cid]: cid
            for cid in self._group_chat_ids
            if cid in self._chat_bindings
        }
        card = build_dir_card(target, entries, sessions_info, tree=tree, session_groups=session_groups, page=page)
        await self._send_or_update_card(chat_id, card, message_id)

    async def _cmd_new_group(self, user_id: str, chat_id: str, args: str,
                              message_id: Optional[str] = None):
        """创建专属群聊并绑定 Claude 会话"""
        session_name = args.strip()
        if not session_name:
            await card_service.send_text(chat_id, "用法：/new-group <会话名>\n示例：/new-group myapp")
            return

        sessions = list_active_sessions()
        if not any(s["name"] == session_name for s in sessions):
            await card_service.send_text(chat_id, f"会话 '{session_name}' 不存在，请先 /start 启动")
            return

        session = next((s for s in sessions if s["name"] == session_name), None)
        pid = session.get("pid") if session else None
        cwd = self._get_pid_cwd(pid) if pid else None
        from .card_builder import _get_display_name
        dir_label = _get_display_name(session_name, cwd)

        from . import config
        try:
            import json as _json
            import urllib.request
            import datetime
            _time_str = datetime.datetime.now().strftime("%H-%M")
            group_name = f"{config.GROUP_NAME_PREFIX}{dir_label}-{_time_str}"
            req_body = {
                "name": group_name,
                "description": f"Remote Claude 专属群 - 会话 {session_name}",
                "user_id_list": [user_id],
            }
            token_resp = urllib.request.urlopen(
                urllib.request.Request(
                    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                    data=_json.dumps({"app_id": config.FEISHU_APP_ID, "app_secret": config.FEISHU_APP_SECRET}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST"
                ), timeout=10
            )
            token_data = _json.loads(token_resp.read())
            token = token_data["tenant_access_token"]

            create_resp = urllib.request.urlopen(
                urllib.request.Request(
                    "https://open.feishu.cn/open-apis/im/v1/chats?user_id_type=open_id",
                    data=_json.dumps(req_body).encode(),
                    headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
                    method="POST"
                ), timeout=10
            )
            create_data = _json.loads(create_resp.read())

            if create_data.get("code") != 0:
                await card_service.send_text(chat_id, f"创建群失败：{create_data.get('msg')}")
                return

            group_chat_id = create_data["data"]["chat_id"]
            self._chat_bindings[group_chat_id] = session_name
            self._save_chat_bindings()
            self._group_chat_ids.add(group_chat_id)
            self._save_group_chat_ids()
            logger.info(f"创建群聊成功: group_chat_id={group_chat_id[:8]}..., session={session_name}")
            # 立即 attach，让新群即刻开始接收 Claude 输出
            await self._attach(group_chat_id, session_name, user_id=user_id)

            # 刷新会话列表卡片，使"创建群聊"按钮变为"进入群聊"
            await self._cmd_list(user_id, chat_id, message_id=message_id)
        except Exception as e:
            logger.error(f"创建群失败: {e}")
            await card_service.send_text(chat_id, f"创建群失败：{e}")

    async def _disband_group_via_api(self, group_chat_id: str) -> tuple:
        """调用飞书 API 解散群聊，返回 (ok: bool, err_msg: str)"""
        import json as _json
        import urllib.request
        import urllib.error
        from . import config
        try:
            token_resp = urllib.request.urlopen(
                urllib.request.Request(
                    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                    data=_json.dumps({"app_id": config.FEISHU_APP_ID, "app_secret": config.FEISHU_APP_SECRET}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST"
                ), timeout=10
            )
            token = _json.loads(token_resp.read())["tenant_access_token"]
            try:
                disband_resp = urllib.request.urlopen(
                    urllib.request.Request(
                        f"https://open.feishu.cn/open-apis/im/v1/chats/{group_chat_id}",
                        headers={"Authorization": f"Bearer {token}"},
                        method="DELETE"
                    ), timeout=10
                )
                disband_data = _json.loads(disband_resp.read())
                if disband_data.get("code") == 0:
                    return True, ""
                return False, disband_data.get("msg", "")
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", errors="replace")
                try:
                    err_data = _json.loads(err_body)
                    return False, f"code={err_data.get('code')} {err_data.get('msg', '')}"
                except Exception:
                    return False, f"HTTP {e.code}"
        except Exception as e:
            return False, str(e)

    async def _disband_groups_for_session(self, session_name: str, source: str = ""):
        """解散绑定到指定会话的所有专属群聊"""
        disbanded = []
        for cid in list(self._group_chat_ids):
            if self._chat_bindings.get(cid) == session_name:
                log_prefix = f"[{source}] " if source else ""
                logger.info(f"{log_prefix}自动解散群聊: chat_id={cid[:8]}..., session={session_name}")
                # 先清理本地状态（防止并发协程重入时重复处理）
                self._group_chat_ids.discard(cid)
                self._chat_bindings.pop(cid, None)
                disbanded.append(cid)
                # 停止轮询 + 断开 bridge
                self._poller.stop(cid)
                bridge = self._bridges.pop(cid, None)
                if bridge:
                    await bridge.disconnect()
                self._chat_sessions.pop(cid, None)
                self._detached_slices.pop(cid, None)
                # 调用飞书 API 解散
                ok, err = await self._disband_group_via_api(cid)
                if not ok:
                    logger.warning(f"{log_prefix}解散群 {cid[:8]}... API 失败: {err}")
        if disbanded:
            self._save_chat_bindings()
            self._save_group_chat_ids()

    # ── Session 健康检查 ─────────────────────────────────────────────────────

    HEALTH_CHECK_INTERVAL = 30  # 秒

    def _ensure_health_check_started(self):
        """懒启动健康检查后台任务（首次收到消息时调用）"""
        health_task = getattr(self, "_health_check_task", None)
        if health_task is None or health_task.done():
            self._health_check_task = asyncio.create_task(self._health_check_loop())
            logger.info("Session 健康检查后台任务已启动")
        desktop = getattr(self, "_desktop", None)
        desktop_start_task = getattr(self, "_desktop_start_task", None)
        if desktop is not None and desktop.has_bindings and not desktop.started and (
            desktop_start_task is None or desktop_start_task.done()
        ):
            self._desktop_start_task = asyncio.create_task(self._desktop.start())

    async def _health_check_loop(self):
        """定期检查绑定的 session 是否仍然存活"""
        while True:
            await asyncio.sleep(self.HEALTH_CHECK_INTERVAL)
            try:
                await self._session_health_check()
            except Exception as e:
                logger.error(f"健康检查异常: {e}", exc_info=True)

    async def _session_health_check(self):
        """扫描所有绑定关系，对已死亡 session 自动解散群聊、清理私聊绑定"""
        bound_sessions = set(self._chat_bindings.values())
        if not bound_sessions:
            return

        active = {s["name"] for s in list_active_sessions()}
        dead_sessions = bound_sessions - active
        if not dead_sessions:
            return

        for session_name in dead_sessions:
            logger.info(f"[health-check] 检测到 session '{session_name}' 已关闭")

            # 记录受影响的 chat_id（在 disband 修改 dict 之前）
            affected_chats = [
                cid for cid, sname in list(self._chat_bindings.items())
                if sname == session_name
            ]

            # 解散群聊（复用已有逻辑，内部会清理群聊的绑定/轮询/bridge）
            await self._disband_groups_for_session(session_name, source="health-check")

            # 处理私聊绑定（非群聊的 chat_id）
            cleaned_private = False
            for cid in affected_chats:
                if cid in self._group_chat_ids:
                    continue  # 群聊已由 _disband_groups_for_session 处理
                if self._chat_bindings.get(cid) != session_name:
                    continue  # 已被其他逻辑清理
                logger.info(f"[health-check] 清理私聊绑定: chat_id={cid[:8]}..., session={session_name}")
                self._poller.stop(cid)
                bridge = self._bridges.pop(cid, None)
                if bridge:
                    await bridge.disconnect()
                self._chat_sessions.pop(cid, None)
                self._detached_slices.pop(cid, None)
                self._remove_binding_by_chat(cid, force=True)
                cleaned_private = True

            if cleaned_private:
                self._save_chat_bindings()

    async def _cmd_disband_group(self, user_id: str, chat_id: str, session_name: str,
                                  message_id: Optional[str] = None):
        """解散与指定会话绑定的专属群聊"""
        group_chat_id = next(
            (cid for cid, sname in self._chat_bindings.items() if sname == session_name and cid.startswith("oc_")),
            None
        )
        if not group_chat_id:
            await card_service.send_text(chat_id, f"会话 '{session_name}' 没有绑定群聊")
            return

        try:
            feishu_ok, feishu_msg = await self._disband_group_via_api(group_chat_id)
            if not feishu_ok:
                logger.error(f"解散群 API 失败: {feishu_msg}")

            # 无论 Feishu delete 是否成功，都清理本地绑定
            logger.info(f"[DISBAND] 单群解散: group_chat_id={group_chat_id[:8]}..., session={session_name}")
            self._group_chat_ids.discard(group_chat_id)
            self._save_group_chat_ids()
            self._remove_binding_by_chat(group_chat_id, force=True)
            await self._detach(group_chat_id)

            if not feishu_ok:
                await card_service.send_text(
                    chat_id,
                    f"⚠️ Feishu 群解散失败（{feishu_msg}），已解除本地绑定。如需彻底解散请在飞书群内手动操作"
                )
            await self._cmd_list(user_id, chat_id, message_id=message_id)
        except Exception as e:
            logger.error(f"解散群失败: {e}")
            await card_service.send_text(chat_id, f"解散群失败：{e}")

    # ── 消息转发 ─────────────────────────────────────────────────────────────

    async def _ensure_bridge(self, chat_id: str, user_id: str = None):
        """获取 bridge，断连时尝试从持久化绑定自动恢复"""
        bridge = self._bridges.get(chat_id)
        if bridge and bridge.running:
            return bridge
        if bridge and not bridge.running:
            logger.info(f"bridge 存在但已停止: chat_id={chat_id[:8]}..., session={self._chat_sessions.get(chat_id)}")
        # 尝试从持久化绑定恢复
        saved_session = getattr(self, "_chat_bindings", {}).get(chat_id)
        if saved_session:
            logger.info(f"自动恢复绑定: chat_id={chat_id[:8]}..., session={saved_session}")
            ok = await self._attach(chat_id, saved_session, user_id=user_id)
            if ok:
                return self._bridges.get(chat_id)
            # 区分临时失败和永久失败
            sessions = list_active_sessions()
            if any(s["name"] == saved_session for s in sessions):
                # 会话存在但连接失败 → 临时性故障，保留绑定供重试
                logger.warning(f"自动恢复失败（临时性）: session={saved_session}, 保留绑定")
                return None
            # 会话已不存在 → 永久失败，级联清除
            logger.warning(f"会话 '{saved_session}' 已不存在，清除绑定: chat_id={chat_id[:8]}...")
            # 1. 先停止当前 chat_id 的轮询器和 bridge
            self._poller.stop(chat_id)
            stale_bridge = self._bridges.pop(chat_id, None)
            if stale_bridge:
                await stale_bridge.disconnect()
            self._chat_sessions.pop(chat_id, None)
            self._detached_slices.pop(chat_id, None)
            # 2. 清除绑定
            self._group_chat_ids.discard(chat_id)
            self._remove_binding_by_chat(chat_id, force=True)
            # 3. 解散同会话的其他群聊（内部会清理各自的轮询器/bridge/绑定）
            await self._disband_groups_for_session(saved_session, source="lazy")
            # 4. 确保持久化保存（disband 按绑定匹配遍历，已在上面移除绑定，确保保存）
            self._save_group_chat_ids()
        return None

    async def _forward_to_claude(
        self, user_id: str, chat_id: str, text: str,
        client_message_id: Optional[str] = None,
    ):
        """转发消息给 Claude（输出由 SharedMemoryPoller 自动推卡片）"""
        desktop = getattr(self, "_desktop", None)
        if desktop is not None and desktop.is_attached(chat_id):
            if not await self._desktop.send_message(
                chat_id, text, client_message_id=client_message_id
            ):
                await card_service.send_text(chat_id, "发送到 Codex Desktop 失败，请刷新状态后重试")
            return

        bridge = await self._ensure_bridge(chat_id, user_id=user_id)

        if not bridge:
            await card_service.send_text(
                chat_id, "未连接到任何会话，请先使用 /attach <会话名> 连接"
            )
            return

        success = await bridge.send_input(text)
        if success:
            self._poller.kick(chat_id)
        else:
            await card_service.send_text(chat_id, "发送失败")

    # ── 选项处理 ─────────────────────────────────────────────────────────────

    async def handle_option_select(self, user_id: str, chat_id: str, option_value: str, option_total: int = 0, *, needs_input: bool = False):
        """闭环选项选择：箭头键导航 + 共享内存验证

        发箭头键导航到目标选项，每步从共享内存读取 selected_value 确认是否到位，
        到位后发 Enter 确认。避免数字键在溢出选项上无效的问题。
        """
        self._ensure_health_check_started()
        logger.info(f"处理选项选择: user={user_id[:8]}..., option={option_value}, total={option_total}")
        _track_stats('lark', 'option_select',
                     session_name=self._chat_sessions.get(chat_id, ''),
                     chat_id=chat_id, detail=option_value)

        bridge = await self._ensure_bridge(chat_id, user_id=user_id)
        if not bridge:
            await card_service.send_text(chat_id, "未连接到任何会话，请先使用 /attach <会话名> 连接")
            return

        target = option_value  # 目标选项 value（如 "2"）
        max_steps = max(option_total, 10) if option_total > 0 else 10

        # 记录初始 option_block 的 block_id，防止跨选项交互误操作
        initial_snapshot = self._poller.read_snapshot(chat_id)
        if not initial_snapshot:
            return
        initial_ob = initial_snapshot.get('option_block')
        if not initial_ob:
            return
        initial_block_id = initial_ob.get('block_id', '')

        for step in range(max_steps):
            # 1. 读取当前选中项
            snapshot = self._poller.read_snapshot(chat_id)
            if not snapshot:
                break
            ob = snapshot.get('option_block')
            if not ob:
                break  # option_block 已消失（CLI 已进入下一状态）

            # 检查 block_id 一致性
            if initial_block_id and ob.get('block_id', '') != initial_block_id:
                logger.warning(f"option_block 已切换，中止选项选择")
                break

            current = ob.get('selected_value', '')

            # 闪烁帧重试：❯ 光标字符会时隐时现，selected_value 为空时短暂重试
            if not current:
                for _retry in range(5):  # 最多重试 5 次，共 500ms
                    await asyncio.sleep(0.1)
                    snap = self._poller.read_snapshot(chat_id)
                    if not snap:
                        break
                    retry_ob = snap.get('option_block')
                    if not retry_ob:
                        break
                    current = retry_ob.get('selected_value', '')
                    if current:
                        break

            # 2. 已到位 → 发 Enter（自由输入选项只导航不发 Enter）
            if current == target:
                if needs_input:
                    logger.info(f"自由输入选项已到位: target={target}，不发送 Enter")
                    self._poller.kick(chat_id)
                    return
                logger.info(f"选项已到位: current={current} == target={target}，发送 Enter")
                success = await bridge.send_raw(b"\r")
                if success:
                    self._poller.kick(chat_id)
                else:
                    await card_service.send_text(chat_id, "发送选择失败")
                return

            # 3. 未到位 → 发箭头键
            if current and target:
                try:
                    if int(current) < int(target):
                        logger.info(f"步骤{step}: current={current} < target={target}，发送 ↓")
                        await bridge.send_raw(b"\x1b[B")  # ↓
                    else:
                        logger.info(f"步骤{step}: current={current} > target={target}，发送 ↑")
                        await bridge.send_raw(b"\x1b[A")  # ↑
                except ValueError:
                    logger.warning(f"步骤{step}: 无法比较 current={current!r} 和 target={target!r}，发送 ↓")
                    await bridge.send_raw(b"\x1b[B")
            else:
                # selected_value 重试后仍为空（真正的初始状态），默认向下
                logger.info(f"步骤{step}: selected_value 重试后仍为空，发送 ↓")
                await bridge.send_raw(b"\x1b[B")

            # 4. 等待共享内存更新（轮询直到 selected_value 变为另一个非空值或超时）
            old_selected = current
            deadline = time.time() + 2.0  # 单步超时 2s
            while time.time() < deadline:
                await asyncio.sleep(0.1)  # 100ms 轮询
                snap = self._poller.read_snapshot(chat_id)
                if not snap:
                    break
                new_ob = snap.get('option_block')
                if not new_ob:
                    break  # option_block 消失，退出
                if initial_block_id and new_ob.get('block_id', '') != initial_block_id:
                    break  # block_id 变了，外层会处理
                new_selected = new_ob.get('selected_value', '')
                # 忽略闪烁帧：只有变为另一个非空值才视为真正变化
                if new_selected and new_selected != old_selected:
                    break

        # 超过 max_steps 仍未到位，记录警告
        logger.warning(f"选项选择超步数: target={target}, steps={max_steps}")

    # ── 快捷键发送 ─────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_key_combo(combo: str) -> Optional[bytes]:
        """将用户输入的按键字符串解析为终端转义序列字节，解析失败返回 None"""
        BASE_KEY_MAP = {
            "up":       b"\x1b[A",
            "down":     b"\x1b[B",
            "right":    b"\x1b[C",
            "left":     b"\x1b[D",
            "enter":    b"\r",
            "esc":      b"\x1b",
            "tab":      b"\t",
            "backspace": b"\x7f",
            "delete":   b"\x1b[3~",
            "space":    b" ",
            "home":     b"\x1b[H",
            "end":      b"\x1b[F",
            "pageup":   b"\x1b[5~",
            "pagedown": b"\x1b[6~",
            "f1":  b"\x1bOP",   "f2":  b"\x1bOQ",  "f3":  b"\x1bOR",  "f4":  b"\x1bOS",
            "f5":  b"\x1b[15~", "f6":  b"\x1b[17~","f7":  b"\x1b[18~","f8":  b"\x1b[19~",
            "f9":  b"\x1b[20~", "f10": b"\x1b[21~","f11": b"\x1b[23~","f12": b"\x1b[24~",
        }

        s = combo.strip().lower()
        parts = [p.strip() for p in s.split("+")]

        mods = set()
        keys = []
        for p in parts:
            if p in ("ctrl", "alt", "shift"):
                mods.add(p)
            else:
                keys.append(p)

        if len(keys) != 1:
            return None
        key = keys[0]

        # ctrl+letter → \x01-\x1a
        if mods == {"ctrl"}:
            if len(key) == 1 and 'a' <= key <= 'z':
                return bytes([ord(key) - ord('a') + 1])
            # ctrl+[ = ESC, ctrl+\ = FS 等特殊控制字符
            ctrl_special = {'[': b'\x1b', '\\': b'\x1c', ']': b'\x1d', '^': b'\x1e', '_': b'\x1f'}
            if key in ctrl_special:
                return ctrl_special[key]
            return None

        # alt+key → ESC prefix
        if mods == {"alt"}:
            base = BASE_KEY_MAP.get(key)
            if base:
                return b"\x1b" + base
            if len(key) == 1:
                return b"\x1b" + key.encode()
            return None

        # shift+tab / shift+enter
        if mods == {"shift"}:
            if key == "tab":
                return b"\x1b[Z"
            if key == "enter":
                return b"\x1b[13;2u"
            return None

        # 无修饰键
        if not mods:
            return BASE_KEY_MAP.get(key)

        return None

    async def _cmd_press(self, user_id: str, chat_id: str, args: str):
        """发送任意按键组合到会话"""
        combo = args.strip()
        if not combo:
            await card_service.send_text(
                chat_id,
                "用法：`/press <按键>`\n"
                "例如：`/press ctrl+c`、`/press esc`、`/press ctrl+f`、`/press alt+x`\n"
                "支持：ctrl/alt/shift 修饰键，方向键 up/down/left/right，enter/esc/tab/backspace/delete/space/home/end/pageup/pagedown/f1-f12"
            )
            return

        raw = LarkHandler._parse_key_combo(combo)
        if raw is None:
            await card_service.send_text(chat_id, f"❌ 无法解析按键：`{combo}`\n使用 `/press` 查看支持的按键格式")
            return

        bridge = await self._ensure_bridge(chat_id, user_id=user_id)
        if not bridge:
            await card_service.send_text(chat_id, "❌ 当前未连接到会话，请先使用 /attach 连接")
            return

        success = await bridge.send_raw(raw)
        if success:
            logger.info(f"[press] 发送按键 {combo!r} ({raw!r}) 到会话")
            self._poller.kick(chat_id)
        else:
            await card_service.send_text(chat_id, f"❌ 发送按键 `{combo}` 失败")

    async def send_raw_key(self, user_id: str, chat_id: str, key_name: str):
        """发送原始控制键到 Claude CLI"""
        desktop = getattr(self, "_desktop", None)
        if desktop is not None and desktop.is_attached(chat_id):
            if key_name == "esc":
                await self._cmd_desktop_stop(user_id, chat_id)
            elif key_name != "enter":
                await card_service.send_text(chat_id, "Desktop 模式不支持终端按键，请直接发送文字消息")
            return

        _track_stats('lark', 'raw_key',
                     session_name=self._chat_sessions.get(chat_id, ''),
                     chat_id=chat_id, detail=key_name)
        KEY_MAP = {
            "up": b"\x1b[A",         # ↑ 上箭头
            "down": b"\x1b[B",       # ↓ 下箭头
            "enter": b"\r",          # Enter
            "ctrl_o": b"\x0f",       # Ctrl+O
            "shift_tab": b"\x1b[Z",  # Shift+Tab
            "esc": b"\x1b",          # ESC
        }
        raw = KEY_MAP.get(key_name)
        if not raw:
            logger.warning(f"未知快捷键: {key_name}")
            return

        bridge = await self._ensure_bridge(chat_id, user_id=user_id)
        if not bridge:
            logger.warning(f"send_raw_key: chat_id={chat_id[:8]}... 未连接会话（自动恢复失败）")
            return

        success = await bridge.send_raw(raw)
        if success:
            logger.info(f"已发送快捷键 {key_name} 到 Claude")
            self._poller.kick(chat_id)
        else:
            logger.warning(f"发送快捷键 {key_name} 失败")

    # ── 辅助方法 ─────────────────────────────────────────────────────────────

    async def _send_or_update_card(
        self, chat_id: str, card: dict, message_id: Optional[str] = None
    ):
        """有 message_id 时就地更新原卡片，否则发新消息；更新失败时降级为发新卡片"""
        if message_id:
            success = await card_service.update_card_by_message_id(message_id, card)
            if success:
                return
        await card_service.create_and_send_card(chat_id, card)

    @staticmethod
    def _collect_ls_entries(target) -> list:
        """获取一级目录内容（隐藏文件除外，目录优先）"""
        entries = []
        try:
            items = sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
            for item in items:
                if item.name.startswith('.'):
                    continue
                entries.append({
                    "name": item.name,
                    "full_path": str(item),
                    "is_dir": item.is_dir(),
                    "depth": 0,
                })
        except PermissionError:
            pass
        return entries

    @staticmethod
    def _collect_tree_entries(target, max_depth: int = 2, max_items: int = 60) -> list:
        """获取树状目录内容"""
        entries = []

        def _walk(path, depth: int):
            if depth > max_depth or len(entries) >= max_items:
                return
            try:
                for item in sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                    if len(entries) >= max_items:
                        break
                    if item.name.startswith('.'):
                        continue
                    entries.append({
                        "name": item.name,
                        "full_path": str(item),
                        "is_dir": item.is_dir(),
                        "depth": depth,
                    })
                    if item.is_dir() and depth < max_depth:
                        _walk(item, depth + 1)
            except PermissionError:
                pass

        _walk(target, 0)
        return entries

    async def disconnect_all_for_shutdown(self) -> None:
        """lark stop 时清理所有活跃流式卡片（更新为已断开状态）"""
        # 停止健康检查
        if self._health_check_task and not self._health_check_task.done():
            self._health_check_task.cancel()
        desktop_start_task = getattr(self, "_desktop_start_task", None)
        if desktop_start_task and not desktop_start_task.done():
            self._desktop_start_task.cancel()
        desktop = getattr(self, "_desktop", None)
        if desktop is not None:
            await desktop.close()
        logger.info(f"[SHUTDOWN] 开始清理: bridges={len(self._bridges)}, sessions={len(self._chat_sessions)}")
        chat_ids = list(self._bridges.keys())
        for chat_id in chat_ids:
            session_name = self._chat_sessions.get(chat_id, "")
            active_slice = self._poller.stop_and_get_active_slice(chat_id)
            if active_slice and session_name:
                await self._update_card_disconnected(chat_id, session_name, active_slice)

    @staticmethod
    def _get_pid_cwd(pid: int) -> Optional[str]:
        """获取进程的工作目录（macOS/Linux）"""
        try:
            result = subprocess.run(
                ["lsof", "-p", str(pid), "-a", "-d", "cwd", "-F", "n"],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.splitlines():
                if line.startswith("n"):
                    return line[1:].strip()
        except Exception:
            pass
        return None


# 全局处理器实例
handler = LarkHandler()
