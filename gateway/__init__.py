"""
网关层 — 多平台消息接入 + 会话管理 + 转发思维层 + 推送回复

来源: nanobot (精简版)
定位: 纯消息路由。ACK 由 LLM 生成，一句 prompt 搞定。
"""

import asyncio
import logging
from pathlib import Path

from shared.core import IncomingMessage, OutgoingMessage, Platform, EventBus
from data.session_store import SessionStore

logger = logging.getLogger("qiyue.gateway")


class BaseAdapter:
    """平台适配器基类"""
    platform: Platform

    async def start(self): ...
    async def stop(self): ...
    async def send(self, msg: OutgoingMessage): ...
    def on_message(self, callback): ...


class DingTalkAdapter(BaseAdapter):
    platform = Platform.DINGTALK


class WeChatAdapter(BaseAdapter):
    platform = Platform.WECHAT


class FeishuAdapter(BaseAdapter):
    platform = Platform.FEISHU


class MessageRouter:
    """消息路由 — LLM 驱动的自然 ACK + 会话上下文 + 转发 + 持久化"""

    SESSION_PREFIX = "unified:"

    ACK_PROMPT = """用一句话简短自然地回应用户（≤8字），像朋友聊天。
规则：禁止用「收到」「好的」「了解」等客服腔。允许用「嗯」「来了」「好」「在」「嘿嘿」等口语。"""

    def __init__(self, bus: EventBus, adapters: list[BaseAdapter],
                 sessions: SessionStore, llm=None):
        self.bus = bus
        self.adapters = {a.platform: a for a in adapters}
        self.sessions = sessions
        self.llm = llm  # 用于生成 ACK，None 时静默跳过
        self.sessions.init_db()

    def _session_id(self, msg: IncomingMessage) -> str:
        return f"{self.SESSION_PREFIX}{msg.channel_id}"

    async def route(self, msg: IncomingMessage):
        """收到消息 → ACK（fire-and-forget）+ 转发 brain"""
        sid = self._session_id(msg)
        self.sessions.create_session(sid)

        # 1. 秒回 ACK（LLM 生成，fire-and-forget，不阻塞 brain）
        asyncio.create_task(self._send_ack(msg))

        # 2. 追加用户消息
        self.sessions.append_message(sid, "user", msg.content)

        # 3. 读取上下文 + 转发思维层
        context = self.sessions.get_context(sid)
        self.bus.emit("message.received",
                       msg=msg,
                       session_id=sid,
                       context=context)

    async def _send_ack(self, msg: IncomingMessage):
        """LLM 生成自然 ACK，异步推送"""
        if not self.llm:
            return
        try:
            ack_text = await self.llm.chat([
                {"role": "system", "content": self.ACK_PROMPT},
                {"role": "user", "content": msg.content},
            ], max_tokens=10, temperature=0.7)
            ack_text = ack_text.strip().rstrip("。！!~～")
            if not ack_text:
                return
        except Exception:
            return

        ack = OutgoingMessage(
            reply_to=msg.id,
            content=ack_text,
            content_type="text",
            is_ack=True,
            is_final=False,
        )
        adapter = self.adapters.get(msg.platform) or next(iter(self.adapters.values()), None)
        if adapter:
            try:
                await adapter.send(ack)
            except Exception:
                pass

    async def deliver(self, response: OutgoingMessage):
        """思维层回复 → 持久化 + 推送"""
        sid = response.reply_to
        self.sessions.append_message(sid, "assistant", response.content)
        adapter = self._resolve_adapter(response)
        if adapter:
            await adapter.send(response)

    def _resolve_adapter(self, response: OutgoingMessage) -> BaseAdapter:
        for adapter in self.adapters.values():
            return adapter
        return None

    def get_context(self, channel_id: str, limit: int = 30) -> list[dict]:
        sid = f"{self.SESSION_PREFIX}{channel_id}"
        return self.sessions.get_context(sid, limit)

    def compact(self, channel_id: str):
        sid = f"{self.SESSION_PREFIX}{channel_id}"
        self.sessions.compact(sid)
