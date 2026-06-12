"""
网关层 — 多平台消息接入 + 会话管理 + 转发思维层 + 推送回复

来源: nanobot (精简版)
定位: 纯消息路由，不调用 LLM
"""

import re
import sys
import logging
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.core import IncomingMessage, OutgoingMessage, Platform, EventBus
from data.session_store import SessionStore


# ═══════════════════════════════════════════
# 情绪感知器 — 轻量级关键词分类（不调 LLM）
# ═══════════════════════════════════════════

class EmotionDetector:
    """基于关键词 + 正则的情绪快速分类，微秒级"""

    PATTERNS = {
        "angry": [
            r"(?i)(妈的|操|傻逼|垃圾|废物|有病|什么鬼|气死|烦死|滚|fuck|shit)",
            r"(?i)(搞什么|怎么又|每次都|能不能行|到底行不行)",
        ],
        "frustrated": [
            r"(?i)(不行|没用|不好使|坏了|又挂了|怎么搞的)",
            r"(?i)(算了|放弃了|不搞了|随便吧)",
        ],
        "urgent": [
            r"(?i)(快|赶紧|马上|立刻|急|紧急|救命|在线等)",
        ],
        "happy": [
            r"(?i)(哈哈|太好了|nice|棒|厉害|感谢|谢谢|爱你|喜欢)",
            r"(?i)(ok|好的呢|真不错|nice)",
        ],
        "sad": [
            r"(?i)(难过|伤心|郁闷|失落|想哭|累|疲惫)",
        ],
    }

    # 按情绪的 ACK（不猜测任务内容，只回应情绪状态）
    ACK_BY_EMOTION = {
        "angry": "嗯，我来看看怎么回事",
        "frustrated": "好的，我来处理",
        "urgent": "马上！",
        "happy": "来啦~",
        "sad": "在呢",
        "neutral": "收到~",
    }

    @classmethod
    def detect(cls, text: str) -> dict:
        """分析消息，返回情绪标签和置信度"""
        scores = {}
        for emotion, pattern_groups in cls.PATTERNS.items():
            score = 0
            for pattern in pattern_groups:
                matches = re.findall(pattern, text)
                score += len(matches)
            if score > 0:
                scores[emotion] = min(score, 3)  # 上限 3

        if not scores:
            return {"emotion": "neutral", "confidence": 1.0, "scores": {}}

        # 取最高分情绪
        dominant = max(scores, key=scores.get)
        confidence = min(scores[dominant] / 3.0, 1.0)
        return {
            "emotion": dominant,
            "confidence": round(confidence, 2),
            "scores": scores,
        }

    @classmethod
    def pick_ack(cls, text: str) -> str:
        """先用情绪检测，再选对应 ACK（不调 LLM，微秒级）"""
        result = cls.detect(text)
        emotion = result["emotion"]
        return cls.ACK_BY_EMOTION.get(emotion, cls.ACK_BY_EMOTION["neutral"])


class BaseAdapter:
    """平台适配器基类"""
    platform: Platform

    async def start(self): ...
    async def stop(self): ...
    async def send(self, msg: OutgoingMessage): ...
    def on_message(self, callback): ...


class DingTalkAdapter(BaseAdapter):
    """钉钉适配器 — Webhook + 机器人 API"""
    platform = Platform.DINGTALK


class WeChatAdapter(BaseAdapter):
    """微信适配器 — 企业微信机器人"""
    platform = Platform.WECHAT


class FeishuAdapter(BaseAdapter):
    """飞书适配器 — WebSocket 长连接"""
    platform = Platform.FEISHU


class MessageRouter:
    """消息路由 — 情绪感知 ACK + 会话上下文 + 转发 + 持久化"""

    # 会话 ID 前缀 -> 用于跨通道统一
    SESSION_PREFIX = "unified:"

    def __init__(self, bus: EventBus, adapters: list[BaseAdapter],
                 sessions: SessionStore):
        self.bus = bus
        self.adapters = {a.platform: a for a in adapters}
        self.sessions = sessions
        self.sessions.init_db()

    def _session_id(self, msg: IncomingMessage) -> str:
        """生成统一会话 ID"""
        return f"{self.SESSION_PREFIX}{msg.channel_id}"

    async def route(self, msg: IncomingMessage):
        """
        收到消息 → 完整流程:
        1. 情绪感知
        2. 确保会话存在 + 追加用户消息 + 读取上下文
        3. 转发给思维层

        没有 ACK。Hermes 也没有——LLM 回复的第一句话就是确认，
        工具进度行（🔧 ... ✓ ...）就是「在干活了」的信号。
        抢答一句本身就是机械的，跟换什么词无关。
        """
        sid = self._session_id(msg)
        glog = logging.getLogger("qiyue.gateway")

        # 1. 情绪感知（微秒级）
        emotion = EmotionDetector.detect(msg.content)
        glog.info(f"[{sid}] Emotion: {emotion['emotion']} ({emotion['confidence']:.2f})")

        # 2. 确保会话 + 追加用户消息
        self.sessions.create_session(sid)
        self.sessions.append_message(sid, "user", msg.content,
                                     metadata={"emotion": emotion["emotion"]})

        # 3. 读取上下文 + 转发思维层
        context = self.sessions.get_context(sid)
        self.bus.emit("message.received",
                       msg=msg,
                       session_id=sid,
                       context=context,
                       emotion=emotion)

    async def deliver(self, response: OutgoingMessage):
        """
        思维层回复 → 持久化 + 推送:
        1. 追加助手回复到会话
        2. 通过适配器推送给用户
        """
        # 1. 追加到会话
        sid = response.reply_to  # reply_to 存的是 session_id
        self.sessions.append_message(sid, "assistant", response.content)

        # 2. 推送
        # 从 reply_to 提取平台信息（实际实现中由 Brain 附带）
        adapter = self._resolve_adapter(response)
        if adapter:
            await adapter.send(response)

    def _resolve_adapter(self, response: OutgoingMessage) -> BaseAdapter:
        """根据回复消息确定推送平台"""
        # 简化实现：遍历所有适配器找到对应的
        for adapter in self.adapters.values():
            return adapter  # TODO: 实际按 channel_id 匹配
        return None

    def get_context(self, channel_id: str, limit: int = 30) -> list[dict]:
        """获取指定通道的会话上下文"""
        sid = f"{self.SESSION_PREFIX}{channel_id}"
        return self.sessions.get_context(sid, limit)

    def compact(self, channel_id: str):
        """压缩会话（保留最近 120 条）"""
        sid = f"{self.SESSION_PREFIX}{channel_id}"
        self.sessions.compact(sid)
