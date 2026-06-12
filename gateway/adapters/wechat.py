"""
企业微信适配器 — XML/JSON 消息解析 + Webhook 签名验证
来源: Hermes gateway/platforms/wecom.py + weixin.py

功能:
- XML 消息解析（企业微信回调格式）
- JSON 消息解析（Webhook 推送格式）
- Webhook 签名验证 (SHA1)
- 媒体文件下载（图片/语音/视频）
- Markdown/文本 消息发送

环境变量:
    WECHAT_TOKEN, WECHAT_ENCODING_AES_KEY, WECHAT_CORP_ID
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import struct
import time
import xml.etree.ElementTree as ET
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("qiyue.wechat")


# ═══════════════════════════════════════════
# 签名验证
# ═══════════════════════════════════════════

def verify_webhook_signature(token: str, timestamp: str, nonce: str,
                              signature: str) -> bool:
    """
    验证企业微信 Webhook 签名

    算法: SHA1(sort([token, timestamp, nonce]))
    """
    if not all([token, timestamp, nonce, signature]):
        return False

    params = sorted([token, timestamp, nonce])
    raw = "".join(params)
    computed = hashlib.sha1(raw.encode()).hexdigest()

    return computed == signature


def verify_msg_signature(token: str, timestamp: str, nonce: str,
                          encrypt_msg: str, msg_signature: str) -> bool:
    """
    验证消息签名（含加密体）

    算法: SHA1(sort([token, timestamp, nonce, encrypt_msg]))
    """
    params = sorted([token, timestamp, nonce, encrypt_msg])
    raw = "".join(params)
    computed = hashlib.sha1(raw.encode()).hexdigest()

    return computed == msg_signature


# ═══════════════════════════════════════════
# 消息加解密（AES-256-CBC）
# ═══════════════════════════════════════════

class WeChatCrypto:
    """
    企业微信消息加解密

    协议: AES-256-CBC, PKCS#7 padding
    消息格式: random(16) + msg_len(4) + msg + corp_id
    """

    def __init__(self, token: str, encoding_aes_key: str, corp_id: str = ""):
        self.token = token
        self.corp_id = corp_id

        # AES key = Base64 decode(EncodingAESKey + "=")
        self.aes_key = base64.b64decode(encoding_aes_key + "=")

    def decrypt(self, encrypt_msg: str) -> str:
        """解密消息"""
        try:
            from Crypto.Cipher import AES
        except ImportError:
            logger.error("pycryptodome not available. Run: pip install pycryptodome")
            raise

        cipher = AES.new(self.aes_key, AES.MODE_CBC, self.aes_key[:16])

        # Base64 解码
        encrypted = base64.b64decode(encrypt_msg)

        # 解密
        decrypted = cipher.decrypt(encrypted)

        # 去除 PKCS#7 padding
        pad_len = decrypted[-1]
        decrypted = decrypted[:-pad_len]

        # 解析: random(16) + msg_len(4) + msg + corp_id
        msg_len = struct.unpack("!I", decrypted[16:20])[0]
        msg = decrypted[20:20 + msg_len].decode("utf-8")
        received_corp_id = decrypted[20 + msg_len:].decode("utf-8")

        if received_corp_id != self.corp_id:
            logger.warning(
                f"[WeChat] Corp ID mismatch: expected={self.corp_id}, got={received_corp_id}"
            )

        return msg

    def encrypt(self, msg: str) -> str:
        """加密消息"""
        try:
            from Crypto.Cipher import AES
        except ImportError:
            logger.error("pycryptodome not available. Run: pip install pycryptodome")
            raise

        import random as _random

        # 构造: random(16) + msg_len(4) + msg + corp_id
        random_bytes = bytes([_random.randint(0, 255) for _ in range(16)])
        msg_bytes = msg.encode("utf-8")
        msg_len = struct.pack("!I", len(msg_bytes))
        corp_bytes = self.corp_id.encode("utf-8")
        plain = random_bytes + msg_len + msg_bytes + corp_bytes

        # PKCS#7 padding
        block_size = 32
        pad_len = block_size - len(plain) % block_size
        plain += bytes([pad_len] * pad_len)

        # AES-CBC 加密
        cipher = AES.new(self.aes_key, AES.MODE_CBC, self.aes_key[:16])
        encrypted = cipher.encrypt(plain)

        return base64.b64encode(encrypted).decode()


# ═══════════════════════════════════════════
# XML 消息解析
# ═══════════════════════════════════════════

class WeChatXMLParser:
    """企业微信 XML 消息解析器"""

    @staticmethod
    def parse(xml_str: str) -> dict:
        """
        解析企业微信 XML 消息

        格式示例:
        <xml>
            <ToUserName><![CDATA[corp_id]]></ToUserName>
            <FromUserName><![CDATA[user_id]]></FromUserName>
            <CreateTime>1234567890</CreateTime>
            <MsgType><![CDATA[text]]></MsgType>
            <Content><![CDATA[hello]]></Content>
            <MsgId>123456</MsgId>
            <AgentID>1000001</AgentID>
        </xml>
        """
        try:
            root = ET.fromstring(xml_str)

            result = {}
            for child in root:
                # CDATA 标签提取纯文本
                text = child.text or ""
                result[child.tag] = text

            # 类型转换
            for int_field in ("CreateTime", "MsgId", "AgentID"):
                if int_field in result:
                    try:
                        result[int_field] = int(result[int_field])
                    except (ValueError, TypeError):
                        pass

            return result

        except ET.ParseError as e:
            logger.error(f"[WeChat] XML parse error: {e}")
            return {"error": str(e), "raw": xml_str[:200]}

    @staticmethod
    def to_xml(data: dict) -> str:
        """构造回复 XML"""
        root = ET.Element("xml")
        for key, value in data.items():
            child = ET.SubElement(root, key)
            if isinstance(value, str):
                child.text = value
            else:
                child.text = str(value)

        # CDATA 包装文本字段
        xml_str = ET.tostring(root, encoding="unicode")
        for field in ("ToUserName", "FromUserName", "Content", "MsgType"):
            xml_str = xml_str.replace(
                f"<{field}>{data.get(field, '')}</{field}>",
                f"<{field}><![CDATA[{data.get(field, '')}]]></{field}>"
            )
        return xml_str


# ═══════════════════════════════════════════
# 媒体下载器
# ═══════════════════════════════════════════

class WeChatMediaDownloader:
    """企业微信媒体文件下载"""

    BASE_URL = "https://qyapi.weixin.qq.com/cgi-bin"

    def __init__(self, access_token: str, http_client=None):
        self.access_token = access_token
        self._http = http_client

    async def download(self, media_id: str) -> Optional[bytes]:
        """下载媒体文件，返回字节"""
        url = f"{self.BASE_URL}/media/get"
        params = {
            "access_token": self.access_token,
            "media_id": media_id,
        }

        resp = await self._http.get(url, params=params)
        resp.raise_for_status()

        content_type = resp.headers.get("Content-Type", "")
        if "application/json" in content_type:
            data = resp.json()
            if data.get("errcode", 0) != 0:
                logger.error(f"[WeChat] Media download error: {data}")
                return None

        return resp.content

    async def download_image(self, media_id: str) -> Optional[bytes]:
        """下载图片"""
        return await self.download(media_id)

    async def download_voice(self, media_id: str) -> Optional[bytes]:
        """下载语音"""
        return await self.download(media_id)

    async def download_video(self, media_id: str) -> Optional[bytes]:
        """下载视频"""
        return await self.download(media_id)

    async def upload(self, file_bytes: bytes, filename: str,
                     media_type: str = "file") -> Optional[str]:
        """上传临时素材，返回 media_id"""
        url = f"{self.BASE_URL}/media/upload"
        params = {
            "access_token": self.access_token,
            "type": media_type,
        }

        files = {
            "media": (filename, file_bytes, "application/octet-stream"),
        }

        resp = await self._http.post(url, params=params, files=files)
        resp.raise_for_status()
        data = resp.json()

        if data.get("errcode", 0) != 0:
            logger.error(f"[WeChat] Media upload error: {data}")
            return None

        return data.get("media_id")


# ═══════════════════════════════════════════
# 企业微信适配器
# ═══════════════════════════════════════════

class WeChatAdapter:
    """
    企业微信机器人适配器

    支持两种模式:
    1. Webhook 模式 — 直接 POST URL（简单）
    2. 回调模式 — 需要公网 URL + 签名验证（完整）

    环境变量:
        WECHAT_WEBHOOK_URL: Webhook 完整 URL（模式1）
        WECHAT_TOKEN: 回调 Token（模式2）
        WECHAT_ENCODING_AES_KEY: AES 密钥（模式2）
        WECHAT_CORP_ID: 企业 ID（模式2）
    """

    platform = "wechat"
    MAX_MESSAGE_LENGTH = 4096

    def __init__(self,
                 webhook_url: str = None,
                 token: str = None,
                 encoding_aes_key: str = None,
                 corp_id: str = None):
        self.webhook_url = webhook_url or os.getenv("WECHAT_WEBHOOK_URL", "")
        self.token = token or os.getenv("WECHAT_TOKEN", "")
        self.encoding_aes_key = encoding_aes_key or os.getenv("WECHAT_ENCODING_AES_KEY", "")
        self.corp_id = corp_id or os.getenv("WECHAT_CORP_ID", "")

        self._http: Optional[Any] = None
        self._access_token: Optional[str] = None
        self._crypto: Optional[WeChatCrypto] = None
        self._on_message: Optional[Callable] = None
        self._media_downloader: Optional[WeChatMediaDownloader] = None

        # 初始化加解密器
        if self.encoding_aes_key:
            self._crypto = WeChatCrypto(self.token, self.encoding_aes_key, self.corp_id)

        # 统计
        self.stats = {
            "messages_received": 0,
            "messages_sent": 0,
            "errors": 0,
        }

    # ─── 生命周期 ───

    async def start(self):
        import httpx
        self._http = httpx.AsyncClient(timeout=30.0)

        # 获取 access_token（回调模式需要）
        if self.corp_id:
            await self._get_token()

        logger.info(f"[WeChat] Adapter started (mode={'callback' if self._crypto else 'webhook'})")

    async def stop(self):
        if self._http:
            await self._http.aclose()
        logger.info("[WeChat] Adapter stopped")

    def on_message(self, callback: Callable):
        self._on_message = callback

    # ─── 消息接收（Webhook 回调处理） ───

    def verify_url(self, msg_signature: str, timestamp: str, nonce: str,
                   echostr: str) -> Optional[str]:
        """
        URL 验证（企业微信首次配置回调 URL 时使用）

        解密 echostr 并返回明文。
        """
        if not self._crypto:
            logger.warning("[WeChat] Crypto not initialized, cannot verify URL")
            return None

        if not verify_msg_signature(self.token, timestamp, nonce, echostr, msg_signature):
            logger.error("[WeChat] URL verification: signature mismatch")
            return None

        try:
            return self._crypto.decrypt(echostr)
        except Exception as e:
            logger.error(f"[WeChat] URL verification decrypt failed: {e}")
            return None

    async def handle_webhook(self, data: dict) -> Optional[dict]:
        """处理 Webhook POST 数据"""
        self.stats["messages_received"] += 1

        msg = {
            "id": data.get("MsgId", str(time.time())),
            "platform": "wechat",
            "channel_id": data.get("ChatId", data.get("FromUserName", "")),
            "sender_id": data.get("FromUserName", data.get("Sender", "")),
            "content": self._extract_content(data),
            "content_type": data.get("MsgType", "text"),
            "timestamp": time.time(),
            "raw": data,
        }

        logger.info(
            f"[WeChat] ← {msg['sender_id'][:12]} "
            f"\"{str(msg['content'])[:80]}\""
        )

        if self._on_message:
            result = self._on_message(msg)
            if asyncio.iscoroutine(result):
                await result

        # 如果是 XML 回调模式，返回回复 XML
        if data.get("ToUserName"):
            return {
                "ToUserName": data["FromUserName"],
                "FromUserName": data["ToUserName"],
                "CreateTime": int(time.time()),
                "MsgType": "text",
                "Content": "收到（由柒月自动回复）",
            }

        return None

    async def handle_encrypted_webhook(self, xml_body: str,
                                        msg_signature: str,
                                        timestamp: str,
                                        nonce: str) -> Optional[str]:
        """处理加密的 Webhook 回调（完整 XML 流程）"""
        if not self._crypto:
            return None

        # 1. 解析 XML 提取 Encrypt 字段
        parsed = WeChatXMLParser.parse(xml_body)
        encrypt_msg = parsed.get("Encrypt", "")
        if not encrypt_msg:
            logger.error("[WeChat] No Encrypt field in XML")
            return None

        # 2. 验证签名
        if not verify_msg_signature(self.token, timestamp, nonce, encrypt_msg, msg_signature):
            logger.error("[WeChat] Message signature verification failed")
            return None

        # 3. 解密
        try:
            decrypted_xml = self._crypto.decrypt(encrypt_msg)
        except Exception as e:
            logger.error(f"[WeChat] Decrypt failed: {e}")
            return None

        # 4. 解析明文 XML
        data = WeChatXMLParser.parse(decrypted_xml)
        if "error" in data:
            return None

        # 5. 处理消息
        await self.handle_webhook(data)

        # 6. 构造加密回复
        reply_data = {
            "ToUserName": data.get("FromUserName", ""),
            "FromUserName": data.get("ToUserName", ""),
            "CreateTime": int(time.time()),
            "MsgType": "text",
            "Content": "收到",
        }
        reply_xml = WeChatXMLParser.to_xml(reply_data)
        encrypted_reply = self._crypto.encrypt(reply_xml)

        # 生成回复签名
        reply_timestamp = str(int(time.time()))
        params = sorted([self.token, reply_timestamp, nonce, encrypted_reply])
        reply_signature = hashlib.sha1("".join(params).encode()).hexdigest()

        # 构造最终响应的 XML
        response_xml = f"""<xml>
<Encrypt><![CDATA[{encrypted_reply}]]></Encrypt>
<MsgSignature><![CDATA[{reply_signature}]]></MsgSignature>
<TimeStamp>{reply_timestamp}</TimeStamp>
<Nonce><![CDATA[{nonce}]]></Nonce>
</xml>"""

        return response_xml

    # ─── 消息发送 ───

    async def send(self, msg, mentioned_list: List[str] = None) -> bool:
        """发送消息到企业微信"""
        if not self._http:
            await self.start()

        content = msg.content if hasattr(msg, 'content') else str(msg)
        content = content[:self.MAX_MESSAGE_LENGTH]

        # Webhook 模式
        if self.webhook_url:
            return await self._send_webhook(content)

        # API 模式
        return await self._send_api(content, msg, mentioned_list)

    async def send_markdown(self, content: str, chat_id: str = None) -> bool:
        """发送 Markdown 消息"""
        if not self._http:
            await self.start()

        if self.webhook_url:
            body = {
                "msgtype": "markdown",
                "markdown": {"content": content[:self.MAX_MESSAGE_LENGTH]},
            }
            resp = await self._http.post(self.webhook_url, json=body)
            resp.raise_for_status()
            return True

        if self._access_token:
            url = (f"https://qyapi.weixin.qq.com/cgi-bin/message/send"
                   f"?access_token={self._access_token}")
            body = {
                "touser": chat_id or "@all",
                "msgtype": "markdown",
                "agentid": 1000002,
                "markdown": {"content": content[:self.MAX_MESSAGE_LENGTH]},
            }
            resp = await self._http.post(url, json=body)
            resp.raise_for_status()
            return True

        return False

    async def send_image(self, image_data: bytes, chat_id: str = None) -> bool:
        """发送图片消息"""
        if not self._http or not self._access_token:
            return False

        # 先上传获取 media_id
        if not self._media_downloader:
            self._media_downloader = WeChatMediaDownloader(
                self._access_token, self._http
            )

        media_id = await self._media_downloader.upload(
            image_data, "image.png", "image"
        )
        if not media_id:
            return False

        url = (f"https://qyapi.weixin.qq.com/cgi-bin/message/send"
               f"?access_token={self._access_token}")
        body = {
            "touser": chat_id or "@all",
            "msgtype": "image",
            "agentid": 1000002,
            "image": {"media_id": media_id},
        }

        resp = await self._http.post(url, json=body)
        resp.raise_for_status()
        return True

    # ─── 内部方法 ───

    def _extract_content(self, data: dict) -> str:
        """提取消息内容"""
        msg_type = data.get("MsgType", "text")

        if msg_type == "text":
            return data.get("Content", data.get("Text", {}).get("Content", ""))
        elif msg_type == "image":
            return f"[图片: {data.get('MediaId', data.get('PicUrl', ''))}]"
        elif msg_type == "voice":
            return f"[语音: {data.get('MediaId', '')}]"
        elif msg_type == "video":
            return f"[视频: {data.get('MediaId', '')}]"
        elif msg_type == "file":
            return f"[文件: {data.get('FileName', '')}]"
        elif msg_type == "event":
            return f"[事件: {data.get('Event', '')}]"
        else:
            return f"[{msg_type}]"

    async def _send_webhook(self, content: str) -> bool:
        """通过 Webhook 发送"""
        body = {
            "msgtype": "markdown",
            "markdown": {"content": content},
        }
        try:
            resp = await self._http.post(self.webhook_url, json=body)
            resp.raise_for_status()
            self.stats["messages_sent"] += 1
            return True
        except Exception as e:
            self.stats["errors"] += 1
            logger.error(f"[WeChat] Send failed: {e}")
            return False

    async def _send_api(self, content: str, msg, mentioned_list: List[str]) -> bool:
        """通过企业微信 API 发送"""
        await self._ensure_token()
        if not self._access_token:
            return False

        channel_id = msg.channel_id if hasattr(msg, 'channel_id') else None
        url = (f"https://qyapi.weixin.qq.com/cgi-bin/message/send"
               f"?access_token={self._access_token}")

        body = {
            "touser": channel_id or "@all",
            "msgtype": "text",
            "agentid": 1000002,
            "text": {"content": content},
        }

        try:
            resp = await self._http.post(url, json=body)
            resp.raise_for_status()
            self.stats["messages_sent"] += 1
            return True
        except Exception as e:
            self.stats["errors"] += 1
            logger.error(f"[WeChat] API send failed: {e}")
            return False

    async def _get_token(self):
        """获取 access_token"""
        if not self.corp_id:
            return

        try:
            url = (f"https://qyapi.weixin.qq.com/cgi-bin/gettoken"
                   f"?corpid={self.corp_id}"
                   f"&corpsecret={self.token}")
            resp = await self._http.get(url)
            resp.raise_for_status()
            data = resp.json()

            if data.get("errcode", -1) == 0:
                self._access_token = data["access_token"]
                logger.debug("[WeChat] Token obtained")
            else:
                logger.error(f"[WeChat] Token error: {data}")

        except Exception as e:
            logger.error(f"[WeChat] Token request failed: {e}")

    async def _ensure_token(self):
        if not self._access_token:
            await self._get_token()
