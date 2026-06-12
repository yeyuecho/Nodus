"""
HTTP Webhook 服务器 — 接收三平台消息
来源: nanobot gateway (端口 18791)

协议:
- 钉钉: POST /dingtalk/webhook (JSON, 可选签名验证)
- 微信: POST /wechat/callback (XML/JSON)
- 飞书: POST /feishu/event (JSON, challenge 验证)

启动后作为 asyncio task 运行，收到消息 → IncomingMessage → Gateway.route()
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
from urllib.parse import parse_qs

from aiohttp import web

from shared.core import IncomingMessage, Platform

logger = logging.getLogger("qiyue.webhook")


class WebhookServer:
    """HTTP Webhook 服务器 — 接收三平台消息回调"""

    def __init__(self, host: str = "127.0.0.1", port: int = 18791,
                 on_message=None):
        self.host = host
        self.port = port
        self.on_message = on_message  # async callback(msg: IncomingMessage)
        self._app = web.Application()
        self._runner = None
        self._msg_counter = 0

        # 注册路由
        self._app.router.add_post("/dingtalk/webhook", self._handle_dingtalk)
        self._app.router.add_post("/wechat/callback", self._handle_wechat)
        self._app.router.add_post("/feishu/event", self._handle_feishu)
        self._app.router.add_get("/feishu/event", self._handle_feishu_challenge)
        self._app.router.add_get("/health", self._handle_health)

    async def start(self):
        """启动 HTTP 服务器"""
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        logger.info(f"[Webhook] Listening on http://{self.host}:{self.port}")

    async def stop(self):
        """停止服务器"""
        if self._runner:
            await self._runner.cleanup()
            logger.info("[Webhook] Stopped")

    def _next_id(self) -> str:
        self._msg_counter += 1
        return f"msg_{int(time.time()*1000)}_{self._msg_counter}"

    # ═══ 钉钉 ═══

    async def _handle_dingtalk(self, request: web.Request) -> web.Response:
        """处理钉钉 Outgoing Webhook"""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)

        # 提取消息
        text = body.get("text", {}).get("content", "")
        sender_id = body.get("senderStaffId", body.get("senderId", "unknown"))
        conversation_id = body.get("conversationId", body.get("sessionWebhook", "unknown"))

        if not text:
            return web.json_response({"error": "empty message"}, status=400)

        msg = IncomingMessage(
            id=self._next_id(),
            platform=Platform.DINGTALK,
            channel_id=f"dingtalk:{conversation_id}",
            sender_id=str(sender_id),
            content=text.strip(),
            timestamp=time.time(),
        )

        if self.on_message:
            await self.on_message(msg)

        return web.json_response({"status": "ok"})

    # ═══ 微信 ═══

    async def _handle_wechat(self, request: web.Request) -> web.Response:
        """处理企业微信回调"""
        try:
            body = await request.text()
        except Exception:
            return web.Response(text="error", status=400)

        # 企业微信 XML 格式 → 简化提取
        import re
        text_match = re.search(r"<Content><!\[CDATA\[(.*?)\]\]></Content>", body)
        user_match = re.search(r"<FromUserName><!\[CDATA\[(.*?)\]\]></FromUserName>", body)
        chat_match = re.search(r"<ChatId><!\[CDATA\[(.*?)\]\]></ChatId>", body)

        text = text_match.group(1) if text_match else ""
        sender = user_match.group(1) if user_match else "unknown"
        chat_id = chat_match.group(1) if chat_match else "unknown"

        if not text:
            # 也可能 JSON 格式
            try:
                data = json.loads(body)
                text = data.get("text", {}).get("content", "")
                sender = data.get("from", {}).get("userid", "unknown")
            except json.JSONDecodeError:
                pass

        if not text:
            return web.Response(text="success")

        msg = IncomingMessage(
            id=self._next_id(),
            platform=Platform.WECHAT,
            channel_id=f"wechat:{chat_id}",
            sender_id=sender,
            content=text.strip(),
            timestamp=time.time(),
        )

        if self.on_message:
            await self.on_message(msg)

        return web.Response(text="success")

    # ═══ 飞书 ═══

    async def _handle_feishu_challenge(self, request: web.Request) -> web.Response:
        """飞书 URL 验证 (challenge)"""
        params = request.query
        challenge = params.get("challenge", "")
        if challenge:
            return web.json_response({"challenge": challenge})
        return web.json_response({"status": "ok"})

    async def _handle_feishu(self, request: web.Request) -> web.Response:
        """处理飞书事件回调"""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)

        # 飞书事件格式
        event = body.get("event", {})
        header = body.get("header", {})

        text = ""
        msg_type = event.get("message", {}).get("message_type", "text")
        if msg_type == "text":
            text = event.get("message", {}).get("content", "{}")
            try:
                text = json.loads(text).get("text", "")
            except json.JSONDecodeError:
                pass

        sender_id = header.get("sender_id", "unknown")
        chat_id = event.get("message", {}).get("chat_id", "unknown")

        if not text:
            # 返回 challenge 或空响应
            challenge = body.get("challenge", "")
            if challenge:
                return web.json_response({"challenge": challenge})
            return web.json_response({"status": "ok"})

        msg = IncomingMessage(
            id=self._next_id(),
            platform=Platform.FEISHU,
            channel_id=f"feishu:{chat_id}",
            sender_id=sender_id,
            content=text.strip(),
            timestamp=time.time(),
        )

        if self.on_message:
            await self.on_message(msg)

        return web.json_response({"status": "ok"})

    # ═══ 健康检查 ═══

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({
            "status": "ok",
            "timestamp": time.time(),
            "messages_received": self._msg_counter,
        })
