"""
网关层 — 多平台消息接入 + 会话管理 + 转发思维层 + 推送回复

来源: nanobot (精简版)
定位: 纯消息路由。ACK 由 LLM 生成，一句 prompt 搞定。
"""

import asyncio
import logging

from shared.core import IncomingMessage, OutgoingMessage, Platform, EventBus
from data.session_store import SessionStore

logger = logging.getLogger("qiyue.gateway")


class BaseAdapter:
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
    """消息路由 — LLM 生成 ACK + 会话管理 + 转发 brain"""

    SESSION_PREFIX = "unified:"
    ACK_PROMPT = (
        "你收到用户的消息。只输出一个简短确认词（≤5字），不要解释，不要重复用户的话。"
        "像朋友聊天，不许用「收到」「好的」。"
        "示例：用户说「帮我查配置」→「好」。用户说「柒月」→「在」。用户说「气死了」→「嗯，我看下」。"
    )

    def __init__(self, bus, adapters, sessions, llm=None):
        self.bus = bus
        self.adapters = {a.platform: a for a in adapters}
        self.sessions = sessions
        self.llm = llm
        self.sessions.init_db()

    def _session_id(self, msg):
        return f"{self.SESSION_PREFIX}{msg.channel_id}"

    async def route(self, msg: IncomingMessage):
        sid = self._session_id(msg)
        self.sessions.create_session(sid)

        # ACK: LLM 生成，fire-and-forget
        asyncio.create_task(self._ack(msg))

        # 追加消息 + 转发 brain
        self.sessions.append_message(sid, "user", msg.content)
        self.bus.emit("message.received",
                       msg=msg, session_id=sid,
                       context=self.sessions.get_context(sid))

    async def _ack(self, msg):
        if not self.llm:
            logger.warning("[ACK] no LLM client — ACK disabled")
            return
        logger.info(f"[ACK] start: '{msg.content[:30]}'")
        try:
            import time; t0 = time.time()
            text = await self.llm.chat([
                {"role": "system", "content": self.ACK_PROMPT},
                {"role": "user", "content": msg.content},
            ], max_tokens=15, temperature=0)
            dt = (time.time() - t0) * 1000
            text = text.strip()
            logger.info(f"[ACK] done ({dt:.0f}ms): '{text}'")
            if not text:
                logger.warning("[ACK] empty — using fallback '嗯'")
                text = "嗯"
        except Exception as e:
            logger.error(f"[ACK] FAIL: {type(e).__name__}: {e}")
            return

        ack = OutgoingMessage(reply_to=msg.id, content=text,
                              is_ack=True, is_final=False)
        adapter = next(iter(self.adapters.values()), None)
        if adapter:
            await adapter.send(ack)

    async def deliver(self, response: OutgoingMessage):
        sid = response.reply_to
        self.sessions.append_message(sid, "assistant", response.content)
        adapter = next(iter(self.adapters.values()), None)
        if adapter:
            await adapter.send(response)

    def get_context(self, channel_id: str, limit=30):
        return self.sessions.get_context(
            f"{self.SESSION_PREFIX}{channel_id}", limit)

    def compact(self, channel_id: str):
        self.sessions.compact(f"{self.SESSION_PREFIX}{channel_id}")
