"""
网关层 — 多平台消息接入 + 会话管理 + 转发思维层 + 推送回复

来源: nanobot (精简版)
定位: 纯消息路由，不调 LLM，不生成 ACK。ACK 由 brain 统一推送。
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
    """消息路由 — 纯转发，ACK 由 brain 统一生成"""

    SESSION_PREFIX = "unified:"

    def __init__(self, bus, adapters, sessions):
        self.bus = bus
        self.adapters = {a.platform: a for a in adapters}
        self.sessions = sessions
        self.sessions.init_db()

    def _session_id(self, msg):
        return f"{self.SESSION_PREFIX}{msg.channel_id}"

    async def route(self, msg: IncomingMessage):
        sid = self._session_id(msg)
        self.sessions.create_session(sid)
        self.sessions.append_message(sid, "user", msg.content)
        self.bus.emit("message.received",
                       msg=msg, session_id=sid,
                       context=self.sessions.get_context(sid))

    async def deliver(self, response: OutgoingMessage):
        sid = response.reply_to
        if not response.is_ack:
            self.sessions.append_message(sid, "assistant", response.content)
        adapter = next(iter(self.adapters.values()), None)
        if adapter:
            await adapter.send(response)

    def get_context(self, channel_id: str, limit=30):
        return self.sessions.get_context(
            f"{self.SESSION_PREFIX}{channel_id}", limit)

    def compact(self, channel_id: str):
        self.sessions.compact(f"{self.SESSION_PREFIX}{channel_id}")
