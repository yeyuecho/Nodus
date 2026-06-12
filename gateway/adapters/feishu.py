"""
飞书适配器 — WebSocket 长连接 + challenge 验证
来源: Hermes gateway/platforms/feishu.py

功能:
- tenant_access_token / app_access_token 双 token 管理
- WebSocket 长连接实时消息（事件订阅 V2）
- URL challenge 验证（配置回调地址）
- 消息收发（文本/卡片/图片/富文本）
- 消息卡片构建（交互式卡片）
- 文件上传/下载

环境变量:
    FEISHU_APP_ID, FEISHU_APP_SECRET
"""

import asyncio
import hashlib
import json
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("qiyue.feishu")


# ═══════════════════════════════════════════
# Token 管理器（双 token）
# ═══════════════════════════════════════════

class FeishuTokenManager:
    """
    飞书双 Token 管理

    - tenant_access_token: 用于 API 调用（有效期 2h）
    - app_access_token: 用于获取 tenant_access_token（有效期 2h）
    """

    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret

        self._tenant_token: Optional[str] = None
        self._tenant_expires_at: float = 0.0

        self._app_token: Optional[str] = None
        self._app_expires_at: float = 0.0

        self._lock = asyncio.Lock()

    async def get_tenant_token(self, http_client) -> str:
        """获取 tenant_access_token"""
        async with self._lock:
            if self._tenant_token and time.time() < self._tenant_expires_at:
                return self._tenant_token

            url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
            body = {
                "app_id": self.app_id,
                "app_secret": self.app_secret,
            }

            resp = await http_client.post(url, json=body)
            resp.raise_for_status()
            data = resp.json()

            if data.get("code", -1) != 0:
                raise RuntimeError(f"Feishu token error: {data}")

            self._tenant_token = data["tenant_access_token"]
            self._tenant_expires_at = time.time() + data.get("expire", 7200) - 300

            logger.debug("[Feishu] Tenant token refreshed")
            return self._tenant_token

    async def get_app_token(self, http_client) -> str:
        """获取 app_access_token"""
        async with self._lock:
            if self._app_token and time.time() < self._app_expires_at:
                return self._app_token

            url = "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal"
            body = {
                "app_id": self.app_id,
                "app_secret": self.app_secret,
            }

            resp = await http_client.post(url, json=body)
            resp.raise_for_status()
            data = resp.json()

            if data.get("code", -1) != 0:
                raise RuntimeError(f"Feishu app token error: {data}")

            self._app_token = data["app_access_token"]
            self._app_expires_at = time.time() + data.get("expire", 7200) - 300

            logger.debug("[Feishu] App token refreshed")
            return self._app_token

    def invalidate(self):
        """使所有 token 失效（用于强制刷新）"""
        self._tenant_token = None
        self._app_token = None


# ═══════════════════════════════════════════
# Challenge 验证
# ═══════════════════════════════════════════

class FeishuChallengeVerifier:
    """
    飞书 URL Challenge 验证

    配置回调地址时，飞书会发送 POST 请求验证 URL 所有权。
    必须返回解密后的 challenge 字符串。
    """

    def __init__(self, verification_token: str = None):
        self.verification_token = verification_token or os.getenv(
            "FEISHU_VERIFICATION_TOKEN", ""
        )

    def verify(self, body: dict) -> Optional[dict]:
        """
        处理 challenge 请求

        返回: {"challenge": <decrypted_challenge>} 或 None
        """
        # 事件订阅 V2 的 URL 验证
        if body.get("type") == "url_verification":
            challenge = body.get("challenge", "")
            token = body.get("token", "")

            if self.verification_token and token != self.verification_token:
                logger.warning("[Feishu] Verification token mismatch")
                return None

            logger.info(f"[Feishu] URL verification: challenge accepted")
            return {"challenge": challenge}

        return None


# ═══════════════════════════════════════════
# 消息卡片构建器
# ═══════════════════════════════════════════

class FeishuCardBuilder:
    """飞书交互式卡片构建器"""

    @staticmethod
    def text_card(title: str, content: str, color: str = "blue") -> dict:
        """纯文本卡片"""
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": color,
            },
            "elements": [
                {"tag": "markdown", "content": content},
            ],
        }

    @staticmethod
    def button_card(title: str, content: str, buttons: List[dict]) -> dict:
        """带按钮的卡片"""
        elements = [{"tag": "markdown", "content": content}]

        actions = []
        for btn in buttons:
            actions.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": btn.get("text", "按钮")},
                "type": btn.get("type", "default"),
                "value": btn.get("value", {}),
            })

        if actions:
            elements.append({"tag": "action", "actions": actions})

        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "blue",
            },
            "elements": elements,
        }

    @staticmethod
    def image_card(title: str, img_key: str, alt: str = "") -> dict:
        """图片卡片"""
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
            },
            "elements": [
                {"tag": "img", "img_key": img_key, "alt": {"tag": "plain_text", "content": alt}},
            ],
        }

    @staticmethod
    def multi_column_card(title: str, columns: List[dict]) -> dict:
        """多列布局卡片"""
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "blue",
            },
            "elements": [
                {
                    "tag": "column_set",
                    "flex_mode": "bisect",
                    "background_style": "default",
                    "columns": columns,
                },
            ],
        }


# ═══════════════════════════════════════════
# 飞书适配器
# ═══════════════════════════════════════════

class FeishuAdapter:
    """
    飞书机器人适配器

    支持:
    - WebSocket 长连接（事件订阅 V2）
    - HTTP API 消息收发
    - 交互式卡片
    - 文件上传
    - Challenge URL 验证

    环境变量:
        FEISHU_APP_ID, FEISHU_APP_SECRET
        FEISHU_VERIFICATION_TOKEN (可选，用于 URL 验证)
    """

    platform = "feishu"
    MAX_MESSAGE_LENGTH = 30000

    def __init__(self,
                 app_id: str = None,
                 app_secret: str = None,
                 verification_token: str = None):
        self.app_id = app_id or os.getenv("FEISHU_APP_ID", "")
        self.app_secret = app_secret or os.getenv("FEISHU_APP_SECRET", "")

        # Token 管理
        self._token_mgr = FeishuTokenManager(self.app_id, self.app_secret)

        # Challenge 验证
        self._challenge_verifier = FeishuChallengeVerifier(verification_token)

        # 运行时
        self._http: Optional[Any] = None
        self._ws: Optional[Any] = None  # WebSocket 连接
        self._running = False
        self._on_message: Optional[Callable] = None
        self._reconnect_attempt = 0

        # 统计
        self.stats = {
            "messages_received": 0,
            "messages_sent": 0,
            "reconnects": 0,
            "errors": 0,
        }

    # ─── 生命周期 ───

    async def start(self):
        import httpx
        self._http = httpx.AsyncClient(timeout=30.0)
        self._running = True

        # 预热 token
        await self._token_mgr.get_tenant_token(self._http)

        logger.info(f"[Feishu] Adapter started (app_id={self.app_id[:8] if self.app_id else 'N/A'})")

    async def stop(self):
        self._running = False
        if self._ws:
            await self._ws.close()
        if self._http:
            await self._http.aclose()
        logger.info("[Feishu] Adapter stopped")

    def on_message(self, callback: Callable):
        self._on_message = callback

    # ─── URL Challenge 验证 ───

    def handle_challenge(self, body: dict) -> Optional[dict]:
        """处理飞书 URL 验证请求"""
        return self._challenge_verifier.verify(body)

    # ─── 消息接收（事件处理） ───

    async def handle_event(self, body: dict) -> Optional[dict]:
        """
        处理飞书事件推送

        飞书事件格式:
        {
            "schema": "2.0",
            "header": {"event_id": "...", "event_type": "im.message.receive_v1", ...},
            "event": {
                "sender": {"sender_id": {"open_id": "..."}},
                "message": {"message_id": "...", "chat_id": "...", "content": "{...}"}
            }
        }
        """
        # Challenge 验证
        challenge_response = self.handle_challenge(body)
        if challenge_response:
            return challenge_response

        # 事件类型检查
        header = body.get("header", {})
        event_type = header.get("event_type", "")

        if "im.message.receive_v1" not in event_type:
            return None

        self.stats["messages_received"] += 1

        event = body.get("event", {})
        sender = event.get("sender", {})
        message = event.get("message", {})

        # 解析消息内容
        content_str = message.get("content", "{}")
        try:
            content_data = json.loads(content_str)
            text = content_data.get("text", "")
        except json.JSONDecodeError:
            text = content_str

        msg = {
            "id": message.get("message_id", ""),
            "platform": "feishu",
            "channel_id": message.get("chat_id", ""),
            "sender_id": sender.get("sender_id", {}).get("open_id", ""),
            "content": text,
            "content_type": message.get("msg_type", "text"),
            "timestamp": time.time(),
            "event_type": event_type,
            "raw": body,
        }

        logger.info(
            f"[Feishu] ← {msg['sender_id'][:12]} "
            f"\"{text[:80]}\""
        )

        if self._on_message:
            result = self._on_message(msg)
            if asyncio.iscoroutine(result):
                await result

        return None

    # ─── 消息发送 ───

    async def send(self, msg, receive_id_type: str = "chat_id") -> bool:
        """发送消息到飞书"""
        if not self._http:
            await self.start()

        content = msg.content if hasattr(msg, 'content') else str(msg)
        channel_id = msg.channel_id if hasattr(msg, 'channel_id') else None

        token = await self._token_mgr.get_tenant_token(self._http)

        url = "https://open.feishu.cn/open-apis/im/v1/messages"
        params = {"receive_id_type": receive_id_type}

        body = {
            "receive_id": channel_id or "oc_default",
            "msg_type": "text",
            "content": json.dumps({"text": content[:self.MAX_MESSAGE_LENGTH]}),
        }

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        try:
            resp = await self._http.post(url, params=params, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()

            if data.get("code", -1) != 0:
                logger.error(f"[Feishu] API error: {data}")
                self.stats["errors"] += 1
                return False

            self.stats["messages_sent"] += 1
            logger.debug(f"[Feishu] Message sent: {len(content)} chars")
            return True

        except Exception as e:
            self.stats["errors"] += 1
            logger.error(f"[Feishu] Send failed: {e}")
            return False

    async def send_card(self, card: dict, receive_id: str = None,
                        receive_id_type: str = "chat_id") -> bool:
        """发送交互式卡片"""
        if not self._http:
            await self.start()

        token = await self._token_mgr.get_tenant_token(self._http)

        url = "https://open.feishu.cn/open-apis/im/v1/messages"
        params = {"receive_id_type": receive_id_type}

        body = {
            "receive_id": receive_id or "oc_default",
            "msg_type": "interactive",
            "content": json.dumps(card),
        }

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        try:
            resp = await self._http.post(url, params=params, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()

            if data.get("code", -1) != 0:
                logger.error(f"[Feishu] Card send error: {data}")
                return False

            self.stats["messages_sent"] += 1
            return True

        except Exception as e:
            self.stats["errors"] += 1
            logger.error(f"[Feishu] Card send failed: {e}")
            return False

    async def send_markdown(self, title: str, content: str, receive_id: str = None) -> bool:
        """发送 Markdown 消息"""
        card = FeishuCardBuilder.text_card(title, content)
        return await self.send_card(card, receive_id)

    async def send_image(self, image_key: str, receive_id: str = None) -> bool:
        """发送图片消息"""
        if not self._http:
            await self.start()

        token = await self._token_mgr.get_tenant_token(self._http)

        url = "https://open.feishu.cn/open-apis/im/v1/messages"
        params = {"receive_id_type": "chat_id"}

        body = {
            "receive_id": receive_id or "oc_default",
            "msg_type": "image",
            "content": json.dumps({"image_key": image_key}),
        }

        headers = {"Authorization": f"Bearer {token}"}

        try:
            resp = await self._http.post(url, params=params, headers=headers, json=body)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"[Feishu] Image send failed: {e}")
            return False

    async def upload_image(self, image_data: bytes, filename: str = "image.png") -> Optional[str]:
        """上传图片，返回 image_key"""
        if not self._http:
            await self.start()

        token = await self._token_mgr.get_tenant_token(self._http)

        url = "https://open.feishu.cn/open-apis/im/v1/images"
        headers = {"Authorization": f"Bearer {token}"}

        files = {
            "image_type": (None, "message"),
            "image": (filename, image_data, "image/png"),
        }

        try:
            resp = await self._http.post(url, headers=headers, files=files)
            resp.raise_for_status()
            data = resp.json()

            if data.get("code", -1) != 0:
                logger.error(f"[Feishu] Image upload error: {data}")
                return None

            return data.get("data", {}).get("image_key")
        except Exception as e:
            logger.error(f"[Feishu] Image upload failed: {e}")
            return None

    async def upload_file(self, file_data: bytes, filename: str,
                          file_type: str = "stream") -> Optional[str]:
        """上传文件，返回 file_key"""
        if not self._http:
            await self.start()

        token = await self._token_mgr.get_tenant_token(self._http)

        url = "https://open.feishu.cn/open-apis/im/v1/files"
        headers = {"Authorization": f"Bearer {token}"}

        files = {
            "file_type": (None, file_type),
            "file_name": (None, filename),
            "file": (filename, file_data, "application/octet-stream"),
        }

        try:
            resp = await self._http.post(url, headers=headers, files=files)
            resp.raise_for_status()
            data = resp.json()

            if data.get("code", -1) != 0:
                logger.error(f"[Feishu] File upload error: {data}")
                return None

            return data.get("data", {}).get("file_key")
        except Exception as e:
            logger.error(f"[Feishu] File upload failed: {e}")
            return None

    # ─── 批量消息 ───

    async def send_batch(self, messages: List[dict]) -> dict:
        """批量发送消息（最多 100 条）"""
        if not self._http:
            await self.start()

        token = await self._token_mgr.get_tenant_token(self._http)

        url = "https://open.feishu.cn/open-apis/im/v1/batch_message/send"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        body = {"messages": messages[:100]}

        try:
            resp = await self._http.post(url, headers=headers, json=body)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"[Feishu] Batch send failed: {e}")
            return {"code": -1, "msg": str(e)}
