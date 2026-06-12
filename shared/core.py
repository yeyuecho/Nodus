"""
共享层 — 事件总线 + LLM 客户端 + 类型定义

这是整个智能体的基础设施，所有层都依赖此模块。
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional
from enum import Enum

logger = logging.getLogger("qiyue.shared")


# ═══════════════════════════════════════════
# 类型定义
# ═══════════════════════════════════════════

class Platform(str, Enum):
    DINGTALK = "dingtalk"
    WECHAT = "weixin"
    FEISHU = "feishu"


@dataclass
class IncomingMessage:
    id: str
    platform: Platform
    channel_id: str
    sender_id: str
    content: str
    content_type: str = "text"
    attachments: List[Dict] = field(default_factory=list)
    timestamp: float = 0.0


@dataclass
class OutgoingMessage:
    reply_to: str
    content: str
    content_type: str = "markdown"
    is_ack: bool = False
    is_final: bool = True
    platform: Optional[Platform] = None       # 回复目标平台
    channel_id: Optional[str] = None           # 回复目标通道
    metadata: dict = field(default_factory=dict)  # 附加元数据（情绪标签等）


@dataclass
class IntentResult:
    intent: str
    confidence: float = 1.0
    parameters: Dict[str, Any] = field(default_factory=dict)
    raw_input: str = ""


class TaskType(str, Enum):
    CODE = "code"
    BROWSER = "browser"
    SHELL = "shell"
    FILE = "file"
    WEB = "web"
    LLM = "llm"


@dataclass
class SubTask:
    id: str
    type: TaskType
    tool: str
    params: Dict[str, Any] = field(default_factory=dict)
    depends_on: List[str] = field(default_factory=list)


@dataclass
class ExecutionPlan:
    task_id: str
    intent: IntentResult
    sub_tasks: List[SubTask] = field(default_factory=list)
    action: str = "llm_direct_reply"
    skill_match: Optional[str] = None


@dataclass
class TaskResult:
    task_id: str
    success: bool = True
    output: Any = None
    error: Optional[str] = None
    duration_ms: float = 0.0


# ═══════════════════════════════════════════
# 事件总线
# ═══════════════════════════════════════════

class EventBus:
    """
    进程内事件总线 — 替代文件轮询

    同步 emit，异步 handler 通过 asyncio.create_task 调度。
    """

    def __init__(self):
        self._listeners: Dict[str, List[Callable]] = {}

    def on(self, event: str, handler: Callable):
        """注册事件监听器"""
        if event not in self._listeners:
            self._listeners[event] = []
        self._listeners[event].append(handler)
        logger.debug(f"[EventBus] {event} → +1 listener ({len(self._listeners[event])} total)")

    def off(self, event: str, handler: Callable):
        """移除事件监听器"""
        if event in self._listeners and handler in self._listeners[event]:
            self._listeners[event].remove(handler)

    def emit(self, event: str, **payload):
        """触发事件 — 同步调用所有 handler"""
        listeners = self._listeners.get(event, [])
        if not listeners:
            logger.debug(f"[EventBus] {event} → 0 listeners (dropped)")
            return

        logger.debug(f"[EventBus] {event} → {len(listeners)} listeners")
        for handler in listeners:
            try:
                result = handler(**payload)
                # 如果 handler 返回协程，调度执行
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception as e:
                logger.error(f"[EventBus] {event} handler error: {e}", exc_info=True)


# ═══════════════════════════════════════════
# LLM 客户端
# ═══════════════════════════════════════════

@dataclass
class LLMConfig:
    provider: str = "deepseek"
    model: str = "deepseek-v4-pro"
    api_key: str = ""
    api_base: str = "https://api.deepseek.com/v1"
    max_tokens: int = 8192
    temperature: float = 0.1
    # 高级配置
    retry_attempts: int = 3
    retry_base_delay: float = 1.0
    retry_max_delay: float = 30.0
    request_timeout: float = 120.0
    max_rpm: int = 0  # 速率限制（每分钟请求数），0=不限制
    stream_timeout: float = 300.0


class RateLimiter:
    """简单的速率限制器（滑动窗口）"""

    def __init__(self, max_rpm: int):
        self.max_rpm = max_rpm
        self._window: List[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self):
        """获取许可（必要时等待）"""
        if self.max_rpm <= 0:
            return

        async with self._lock:
            now = time.time()
            # 清理过期记录
            self._window = [t for t in self._window if now - t < 60]

            if len(self._window) >= self.max_rpm:
                wait_time = 60 - (now - self._window[0]) + 0.1
                if wait_time > 0:
                    await asyncio.sleep(wait_time)
                self._window = self._window[1:]

            self._window.append(time.time())


class LLMClient:
    """
    统一 LLM 调用接口 — 封装 shared/models.py 的 ModelAdapter

    增强功能:
    - 自动重试（指数退避）
    - 速率限制
    - 流式输出
    - Tool calling 支持
    - 多 provider 切换
    """

    def __init__(self, config: LLMConfig = None):
        self.config = config or LLMConfig()
        self._rate_limiter = RateLimiter(self.config.max_rpm) if self.config.max_rpm > 0 else None
        self._call_count: int = 0
        self._total_tokens: int = 0

    # ─── 基础调用 ───

    async def chat(self, messages: List[Dict], **kwargs) -> str:
        """发送对话，返回文本回复（带重试）"""
        last_error = None

        for attempt in range(self.config.retry_attempts):
            try:
                return await self._chat_impl(messages, **kwargs)
            except Exception as e:
                last_error = str(e)
                logger.warning(
                    f"[LLM] Attempt {attempt + 1}/{self.config.retry_attempts} failed: {e}"
                )
                if attempt < self.config.retry_attempts - 1:
                    delay = min(
                        self.config.retry_base_delay * (2 ** attempt),
                        self.config.retry_max_delay,
                    )
                    await asyncio.sleep(delay)

        raise RuntimeError(f"LLM call failed after {self.config.retry_attempts} retries: {last_error}")

    async def _chat_impl(self, messages: List[Dict], **kwargs) -> str:
        """实际的 LLM 调用"""
        await self._check_rate_limit()

        from shared.models import ModelAdapter, ModelConfig

        adapter = ModelAdapter(ModelConfig(
            provider=self.config.provider,
            model=kwargs.get("model", self.config.model),
            api_key=self.config.api_key,
            api_base=self.config.api_base,
            max_tokens=kwargs.get("max_tokens", self.config.max_tokens),
            temperature=kwargs.get("temperature", self.config.temperature),
        ))

        resp = await asyncio.wait_for(
            adapter.chat(
                messages=messages,
                max_tokens=kwargs.get("max_tokens"),
                temperature=kwargs.get("temperature"),
                json_mode=kwargs.get("json_mode", False),
            ),
            timeout=kwargs.get("timeout", self.config.request_timeout),
        )

        self._call_count += 1
        self._total_tokens += resp.usage.get("total_tokens", 0)

        return resp.content

    # ─── JSON 模式 ───

    async def chat_json(self, messages: List[Dict], schema: Dict = None, **kwargs) -> Dict:
        """发送对话，强制返回 JSON"""
        msgs = list(messages)
        if schema:
            msgs.insert(0, {
                "role": "system",
                "content": f"Output valid JSON only. Schema:\n{json.dumps(schema, ensure_ascii=False)}"
            })

        text = await self.chat(msgs, json_mode=True, **kwargs)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            import re
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    pass
            return {"raw": text, "error": "JSON parse failed"}

    # ─── 流式输出 ───

    async def chat_stream(self, messages: List[Dict],
                          on_token: Callable = None,
                          **kwargs) -> str:
        """
        流式对话（逐 token 返回）

        参数:
            on_token: 每个 token 的回调 async def on_token(token: str)
            **kwargs: 传递给 chat 的参数

        返回: 完整文本
        """
        await self._check_rate_limit()

        from shared.models import ModelAdapter, ModelConfig

        adapter = ModelAdapter(ModelConfig(
            provider=self.config.provider,
            model=kwargs.get("model", self.config.model),
            api_key=self.config.api_key,
            api_base=self.config.api_base,
            max_tokens=kwargs.get("max_tokens", self.config.max_tokens),
            temperature=kwargs.get("temperature", self.config.temperature),
        ))

        full_text = []
        try:
            async for token in adapter.stream(messages=messages, model=kwargs.get("model")):
                full_text.append(token)
                if on_token:
                    result = on_token(token)
                    if asyncio.iscoroutine(result):
                        await result
        except asyncio.TimeoutError:
            logger.warning("[LLM] Stream timeout")
        except Exception as e:
            logger.error(f"[LLM] Stream error: {e}")

        self._call_count += 1
        return "".join(full_text)

    # ─── Tool Calling ───

    async def chat_with_tools(self, messages: List[Dict],
                               tools: List[Dict],
                               tool_choice: str = "auto",
                               **kwargs) -> Dict:
        """
        带 tool calling 的对话

        返回: {"content": str, "tool_calls": [...]}
        """
        from shared.models import ModelAdapter, ModelConfig

        adapter = ModelAdapter(ModelConfig(
            provider=kwargs.get("provider", self.config.provider),
            model=kwargs.get("model", self.config.model),
            api_key=self.config.api_key,
            api_base=self.config.api_base,
            max_tokens=kwargs.get("max_tokens", self.config.max_tokens),
            temperature=kwargs.get("temperature", self.config.temperature),
        ))

        resp = await adapter.chat(
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
        )

        self._call_count += 1

        result = {
            "content": resp.content or "",
            "tool_calls": resp.tool_calls or [],
        }

        # DeepSeek 可能把 tool_calls 放在 content 文本里而非原生字段
        if not result["tool_calls"] and result["content"] and "<invoke" in result["content"]:
            import re, json
            calls = []
            for m in re.finditer(
                r'<invoke\s+name\s*=\s*"(\w+)"[^>]*>(.*?)</invoke>',
                result["content"], re.DOTALL,
            ):
                name = m.group(1)
                args = {}
                for pm in re.finditer(
                    r'<parameter\s+name\s*=\s*"(\w+)"[^>]*>(.*?)</parameter>',
                    m.group(2), re.DOTALL,
                ):
                    args[pm.group(1)] = pm.group(2).strip()
                if args:
                    calls.append({
                        "id": f"call_{len(calls)}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    })
            if calls:
                result["tool_calls"] = calls

        return result

    # ─── 多 Provider 切换 ───

    async def switch_provider(self, provider: str, model: str = None,
                               api_key: str = None, api_base: str = None):
        """动态切换 LLM provider"""
        self.config.provider = provider
        if model:
            self.config.model = model
        if api_key:
            self.config.api_key = api_key
        if api_base:
            self.config.api_base = api_base
        logger.info(f"[LLM] Switched to {provider}/{self.config.model}")

    # ─── 统计 ───

    def get_stats(self) -> dict:
        """获取调用统计"""
        return {
            "calls": self._call_count,
            "total_tokens": self._total_tokens,
            "model": self.config.model,
            "provider": self.config.provider,
        }

    # ─── 内部 ───

    async def _check_rate_limit(self):
        """检查速率限制"""
        if self._rate_limiter:
            await self._rate_limiter.acquire()
