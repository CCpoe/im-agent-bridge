"""
飞书卡片服务 - 支持创建和实时更新卡片
"""

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Dict, Any, Optional
from dataclasses import dataclass, field

import lark_oapi as lark

logger = logging.getLogger('CardService')
from lark_oapi.api.im.v1 import (
    CreateImageRequest,
    CreateImageRequestBody,
    GetMessageRequest,
    CreateMessageRequest,
    CreateMessageRequestBody,
)
from lark_oapi.api.cardkit.v1 import (
    CreateCardRequest, CreateCardRequestBody,
    UpdateCardRequest, UpdateCardRequestBody, Card
)

from . import config


def _is_element_limit_error(msg: str) -> bool:
    """判断飞书 API 返回的错误是否为元素超限"""
    if not msg:
        return False
    lower = msg.lower()
    return "element exceeds" in lower or "超限" in lower


MAX_CARD_IMAGE_BYTES = 10 * 1024 * 1024


def _is_supported_image(path: Path) -> bool:
    """Accept common raster image signatures supported by Lark messages."""
    try:
        with path.open("rb") as source:
            header = source.read(16)
    except OSError:
        return False
    return bool(
        header.startswith(b"\x89PNG\r\n\x1a\n")
        or header.startswith(b"\xff\xd8\xff")
        or header.startswith((b"GIF87a", b"GIF89a"))
        or (header.startswith(b"RIFF") and header[8:12] == b"WEBP")
    )


class _ElementLimitResult:
    """元素超限的哨兵返回值，__bool__ 为 False 兼容现有 if not success 逻辑"""
    is_element_limit = True

    def __bool__(self):
        return False


import sys as _sys
_sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent.parent))
try:
    from stats import track as _track_stats
except Exception:
    def _track_stats(*args, **kwargs): pass


@dataclass
class CardState:
    """卡片状态"""
    card_id: str
    message_id: Optional[str] = None
    sequence: int = 0
    last_update: float = field(default_factory=time.time)


class CardService:
    """飞书卡片服务"""

    def __init__(self):
        self.client: Optional[lark.Client] = None
        self._init_client()
        # chat_id -> CardState
        self._active_cards: Dict[str, CardState] = {}
        # message_id -> CardState（反查，用于按钮点击就地更新）
        self._cards_by_message_id: Dict[str, CardState] = {}

    def _init_client(self):
        """初始化飞书客户端"""
        if config.FEISHU_APP_ID and config.FEISHU_APP_SECRET:
            self.client = lark.Client.builder() \
                .app_id(config.FEISHU_APP_ID) \
                .app_secret(config.FEISHU_APP_SECRET) \
                .build()

    async def create_card(self, card_content: Dict[str, Any]) -> Optional[str]:
        """创建卡片实体，返回 card_id（失败自动重试 1 次）"""
        if not self.client:
            print("[CardService] 客户端未初始化")
            return None

        import asyncio

        for attempt in range(2):
            try:
                request = CreateCardRequest.builder() \
                    .request_body(
                        CreateCardRequestBody.builder()
                        .type("card_json")
                        .data(json.dumps(card_content, ensure_ascii=False))
                        .build()
                    ) \
                    .build()

                response = await asyncio.to_thread(
                    self.client.cardkit.v1.card.create, request
                )

                if response.success():
                    card_id = getattr(getattr(response, "data", None), "card_id", None)
                    return card_id
                else:
                    logger.warning(f"创建卡片失败(attempt={attempt+1}): code={response.code} msg={response.msg}")
            except Exception as e:
                logger.error(f"创建卡片异常(attempt={attempt+1}): {e}")

            if attempt == 0:
                await asyncio.sleep(1)

        _track_stats('error', 'card_api', detail='create_card')
        return None

    async def upload_image(self, image_path: Any) -> Optional[str]:
        """Upload one verified local raster image for use by a card ``img`` node."""
        if not self.client:
            return None
        try:
            path = Path(image_path).expanduser().resolve(strict=True)
            size = path.stat().st_size
        except (OSError, RuntimeError, TypeError, ValueError):
            logger.warning("卡片图片不可读取，已跳过")
            return None
        if not path.is_file() or size <= 0 or size > MAX_CARD_IMAGE_BYTES:
            logger.warning("卡片图片为空、过大或不是普通文件，已跳过")
            return None
        if not _is_supported_image(path):
            logger.warning("卡片图片格式不受支持，已跳过")
            return None

        import asyncio

        for attempt in range(2):
            try:
                with path.open("rb") as image:
                    request = CreateImageRequest.builder() \
                        .request_body(
                            CreateImageRequestBody.builder()
                            .image_type("message")
                            .image(image)
                            .build()
                        ) \
                        .build()
                    response = await asyncio.to_thread(
                        self.client.im.v1.image.create, request
                    )
                if response.success():
                    image_key = getattr(getattr(response, "data", None), "image_key", None)
                    if isinstance(image_key, str) and image_key:
                        return image_key
                else:
                    logger.warning(
                        "上传卡片图片失败(attempt=%s): code=%s msg=%s",
                        attempt + 1,
                        response.code,
                        response.msg,
                    )
            except Exception as error:
                logger.warning(
                    "上传卡片图片异常(attempt=%s): %s",
                    attempt + 1,
                    type(error).__name__,
                )
            if attempt == 0:
                await asyncio.sleep(1)

        _track_stats('error', 'card_api', detail='upload_image')
        return None

    async def send_card(
        self,
        receive_id: str,
        card_id: str,
        *,
        receive_id_type: str = "chat_id",
        message_uuid: Optional[str] = None,
    ) -> Optional[str]:
        """发送卡片消息，返回 message_id。"""
        if not self.client:
            return None

        try:
            import asyncio

            card_content = {
                "type": "card",
                "data": {"card_id": card_id}
            }

            body_builder = CreateMessageRequestBody.builder() \
                .receive_id(receive_id) \
                .msg_type("interactive") \
                .content(json.dumps(card_content))
            if message_uuid:
                body_builder = body_builder.uuid(message_uuid)
            request = CreateMessageRequest.builder() \
                .receive_id_type(receive_id_type) \
                .request_body(body_builder.build()) \
                .build()

            response = await asyncio.to_thread(
                self.client.im.v1.message.create, request
            )

            if response.success():
                message_id = getattr(getattr(response, "data", None), "message_id", None)
                return message_id
            else:
                logger.warning(f"发送卡片失败: code={response.code} msg={response.msg}")
                return None
        except Exception as e:
            logger.error(f"发送卡片异常: {e}")
            return None

    async def create_and_send_card(
        self, chat_id: str, card_content: Dict[str, Any]
    ) -> Optional[str]:
        """创建卡片并发送，内部维护 message_id 反查索引，返回 message_id"""
        card_id = await self.create_card(card_content)
        if not card_id:
            return None
        message_id = await self.send_card(chat_id, card_id)
        if message_id:
            state = CardState(card_id=card_id, message_id=message_id)
            self._cards_by_message_id[message_id] = state
            logger.info(f"已记录卡片: msg={message_id}, card={card_id}")
        return message_id

    async def create_and_send_card_to_user(
        self,
        user_id: str,
        card_content: Dict[str, Any],
        *,
        message_uuid: Optional[str] = None,
    ) -> Optional[str]:
        """发送独立私聊卡片，并登记其 message/card 映射供后续原地接管。"""

        card_id = await self.create_card(card_content)
        if not card_id:
            return None
        message_id = await self.send_card(
            user_id,
            card_id,
            receive_id_type="open_id",
            message_uuid=message_uuid,
        )
        if message_id:
            self._cards_by_message_id[message_id] = CardState(
                card_id=card_id,
                message_id=message_id,
            )
        return message_id

    async def update_card_by_message_id(
        self, message_id: str, card_content: Dict[str, Any]
    ) -> bool:
        """按 message_id 就地更新卡片内容（通过 card_id 反查使用 CardKit update）"""
        state = self._cards_by_message_id.get(message_id)
        if not state:
            logger.warning(f"未找到 message_id 对应的卡片状态: {message_id}")
            return False
        state.sequence += 1
        return await self.update_card(state.card_id, state.sequence, card_content)

    async def update_card(self, card_id: str, sequence: int, card_content: Dict[str, Any]) -> bool:
        """更新卡片内容（失败自动重试 1 次）"""
        if not self.client:
            return False

        import asyncio

        for attempt in range(2):
            try:
                update_uuid = f"{int(time.time() * 1000)}-{uuid.uuid4()}"

                request = UpdateCardRequest.builder() \
                    .card_id(card_id) \
                    .request_body(
                        UpdateCardRequestBody.builder()
                        .uuid(update_uuid)
                        .sequence(sequence)
                        .card(
                            Card.builder()
                            .type("card_json")
                            .data(json.dumps(card_content, ensure_ascii=False))
                            .build()
                        )
                        .build()
                    ) \
                    .build()

                response = await asyncio.to_thread(
                    self.client.cardkit.v1.card.update, request
                )

                if response.success():
                    return True
                else:
                    logger.warning(f"更新卡片失败(attempt={attempt+1}): card_id={card_id} seq={sequence} code={response.code} msg={response.msg}")
                    if _is_element_limit_error(response.msg):
                        # 元素超限是内容问题，重试无意义，直接返回哨兵值
                        logger.warning(f"检测到元素超限错误，跳过重试: card_id={card_id}")
                        return _ElementLimitResult()
            except Exception as e:
                logger.error(f"更新卡片异常(attempt={attempt+1}): card_id={card_id} seq={sequence} error={e}")

            if attempt == 0:
                await asyncio.sleep(1)

        _track_stats('error', 'card_api', detail='update_card')
        return False

    async def send_urgent_app(self, message_id: str, user_ids: list) -> bool:
        """对已有消息发送应用内加急通知，避免发新消息顶高流式卡片"""
        if not self.client:
            return False

        import asyncio
        from lark_oapi.api.im.v1 import UrgentAppMessageRequest, UrgentReceivers

        try:
            request = UrgentAppMessageRequest.builder() \
                .message_id(message_id) \
                .user_id_type("open_id") \
                .request_body(
                    UrgentReceivers.builder()
                    .user_id_list(user_ids)
                    .build()
                ) \
                .build()

            response = await asyncio.to_thread(self.client.im.v1.message.urgent_app, request)
            if response.success():
                logger.info(f"加急通知成功: message_id={message_id}, users={user_ids}")
                return True
            else:
                logger.warning(f"加急通知失败: code={response.code} msg={response.msg}")
                return False
        except Exception as e:
            logger.error(f"加急通知异常: {e}")
            return False

    async def cancel_urgent_app(self, message_id: str, user_ids: list) -> bool:
        """取消已有消息的应用内加急通知"""
        if not self.client:
            return False

        import asyncio
        from lark_oapi.core.model import BaseRequest
        from lark_oapi.core.enum import HttpMethod, AccessTokenType

        try:
            request = BaseRequest()
            request.http_method = HttpMethod.POST
            request.uri = "/open-apis/im/v2/urgent/batch_cancel"
            request.token_types = {AccessTokenType.TENANT}
            request.queries = [("user_id_type", "open_id")]
            request.body = {"message_id": message_id, "receiver_user_ids": user_ids}

            response = await asyncio.to_thread(self.client.request, request)
            if response.success():
                logger.info(f"取消加急成功: message_id={message_id}, code={response.code}")
                return True
            else:
                logger.warning(f"取消加急失败: code={response.code} msg={response.msg}")
                return False
        except Exception as e:
            logger.error(f"取消加急异常: {e}")
            return False

    async def send_text(self, chat_id: str, text: str) -> Optional[str]:
        """发送纯文本消息，返回 message_id（失败返回 None）"""
        if not self.client:
            print(f"[Lark] 消息: {text}")
            return None

        try:
            import asyncio

            request = CreateMessageRequest.builder() \
                .receive_id_type("chat_id") \
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type("text")
                    .content(json.dumps({"text": text}))
                    .build()
                ) \
                .build()

            response = await asyncio.to_thread(
                self.client.im.v1.message.create, request
            )

            if response.success():
                return getattr(getattr(response, "data", None), "message_id", None)
            else:
                logger.warning(f"发送文本失败: code={response.code} msg={response.msg}")
                return None
        except Exception as e:
            logger.error(f"发送文本异常: {e}")
            return None

    # 管理活跃卡片的方法
    def get_active_card(self, chat_id: str) -> Optional[CardState]:
        """获取聊天的活跃卡片"""
        return self._active_cards.get(chat_id)

    def set_active_card(self, chat_id: str, card_state: CardState):
        """设置聊天的活跃卡片"""
        self._active_cards[chat_id] = card_state
        if card_state.message_id:
            self._cards_by_message_id[card_state.message_id] = card_state

    async def update_and_reuse_message_card(
        self,
        chat_id: str,
        message_id: str,
        card_content: Dict[str, Any],
    ) -> bool:
        """Update a sent card and promote it only after the update succeeds."""
        state = self._cards_by_message_id.get(message_id)
        if state is None:
            state = await self._load_message_card_state(chat_id, message_id)
        if state is None:
            return False
        state.sequence += 1
        updated = await self.update_card(state.card_id, state.sequence, card_content)
        if not updated:
            return False
        state.last_update = time.time()
        self._active_cards[chat_id] = state
        return True

    async def _load_message_card_state(
        self,
        chat_id: str,
        message_id: str,
    ) -> Optional[CardState]:
        """Recover a CardKit card_id for a bot message after a process restart."""
        if not self.client or not message_id:
            return None
        import asyncio

        try:
            request = GetMessageRequest.builder().message_id(message_id).build()
            response = await asyncio.to_thread(
                self.client.im.v1.message.get,
                request,
            )
            if not response.success():
                logger.warning(
                    "反查卡片消息失败: code=%s msg=%s",
                    response.code,
                    response.msg,
                )
                return None
            items = getattr(getattr(response, "data", None), "items", None) or []
            for item in items:
                item_message_id = getattr(item, "message_id", None)
                item_chat_id = getattr(item, "chat_id", None)
                item_type = getattr(item, "msg_type", None)
                if item_message_id and item_message_id != message_id:
                    continue
                if chat_id and item_chat_id and item_chat_id != chat_id:
                    continue
                if item_type and item_type != "interactive":
                    continue
                body = getattr(item, "body", None)
                content = getattr(body, "content", None)
                if not isinstance(content, str):
                    continue
                try:
                    payload = json.loads(content)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(payload, dict) or payload.get("type") != "card":
                    continue
                data = payload.get("data") if isinstance(payload, dict) else None
                card_id = data.get("card_id") if isinstance(data, dict) else None
                if not isinstance(card_id, str) or not card_id:
                    continue
                state = CardState(
                    card_id=card_id,
                    message_id=message_id,
                    sequence=int(time.time() * 1000),
                )
                self._cards_by_message_id[message_id] = state
                return state
        except Exception as error:
            logger.warning("反查卡片消息异常: %s", type(error).__name__)
        return None

    def clear_active_card(self, chat_id: str):
        """清除聊天的活跃卡片"""
        if chat_id in self._active_cards:
            del self._active_cards[chat_id]


# 全局实例
card_service = CardService()
