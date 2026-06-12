"""
网关层 — 多平台消息接入 + 会话管理 + 转发思维层 + 推送回复

来源: nanobot (精简版)
定位: 纯消息路由，不调用 LLM
"""

import re
import sys
import logging
import random
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

    # ACK 模板按意图关键词匹配
    ACK_TEMPLATES = {
        "system_check": [
            "正在查看系统信息 🖥️",
            "让我看看你的电脑~",
            "查配置是吧，马上 👀",
        ],
        "search": [
            "正在搜索 🔍",
            "帮你找找看~",
            "搜一下，稍等哦",
        ],
        "file_ops": [
            "收到，准备处理 📝",
            "文件交给我~",
            "开始动手啦 ✍️",
        ],
        "device_control": [
            "正在执行操作 ⚡",
            "好的，我来控制~",
            "收到指令！",
        ],
        "chat": [
            "嗯嗯，我在听~",
            "收到~",
            "好嘞 👋",
            "在呢在呢",
        ],
        "default": [
            "收到，正在处理~",
            "交给我吧 🔍",
            "马上就来~",
        ],
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
        """根据消息内容选一条有温度的 ACK"""
        lowered = text.lower()

        # 系统信息类
        if any(kw in lowered for kw in ["配置", "电脑", "系统", "硬件", "cpu", "内存", "显卡", "硬盘", "设备"]):
            pool = cls.ACK_TEMPLATES["system_check"]
        # 搜索类
        elif any(kw in lowered for kw in ["搜索", "查", "找", "搜", "有没有", "在哪"]):
            pool = cls.ACK_TEMPLATES["search"]
        # 文件操作类
        elif any(kw in lowered for kw in ["写", "改", "修", "代码", "文件", "创建", "删除", "保存"]):
            pool = cls.ACK_TEMPLATES["file_ops"]
        # 设备控制类
        elif any(kw in lowered for kw in ["开", "关", "控制", "空调", "灯", "设备", "启动", "停止"]):
            pool = cls.ACK_TEMPLATES["device_control"]
        # 闲聊类
        elif len(text) < 10 or any(kw in lowered for kw in ["你好", "嗨", "在吗", "谢谢", "晚安", "早"]):
            pool = cls.ACK_TEMPLATES["chat"]
        else:
            pool = cls.ACK_TEMPLATES["default"]

        return random.choice(pool)


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
        1. 情绪感知 + 智能 ACK
        2. 确保会话存在
        3. 秒回 ACK（有温度的）
        4. 追加用户消息到会话（含情绪标签）
        5. 读取会话上下文
        6. 转发给思维层（附带情绪标签）
        """
        sid = self._session_id(msg)
        glog = logging.getLogger("qiyue.gateway")

        # 1. 情绪感知（微秒级，不等 LLM）
        emotion = EmotionDetector.detect(msg.content)
        glog.info(f"[{sid}] Emotion: {emotion['emotion']} ({emotion['confidence']:.2f})")

        # 2. 确保会话存在
        self.sessions.create_session(sid)

        # 3. 秒回 ACK（智能匹配，有温度）
        ack_text = EmotionDetector.pick_ack(msg.content)
        ack = OutgoingMessage(
            reply_to=msg.id,
            content=ack_text,
            content_type="text",
            is_ack=True,
            is_final=False,
            metadata={"emotion": emotion["emotion"]},
        )
        adapter = self.adapters.get(msg.platform) or next(iter(self.adapters.values()), None)
        if adapter:
            await adapter.send(ack)
            glog.debug(f"[ACK] '{ack_text}' via {adapter.__class__.__name__}")

        # 4. 追加用户消息（含情绪标签）
        self.sessions.append_message(sid, "user", msg.content,
                                     metadata={"emotion": emotion["emotion"]})

        # 5. 读取会话上下文
        context = self.sessions.get_context(sid)

        # 6. 转发给思维层（附带情绪标签和上下文）
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
