"""
增强 LLM 客户端 — 流式/重试/限流/多 provider 支持
来源: Hermes provider 模式 + plugins/model-providers/

特性:
- 流式响应 (SSE 增量回调)
- 重试 + 指数退避 + jitter
- Rate limiting (令牌桶)
- 多 provider 支持 (DeepSeek, OpenAI, Anthropic, 兼容 API)
- 连接池复用 + 长连接 keepalive
- 请求超时管理
- Token 使用追踪
"""

import asyncio
import json
import logging
import os
import random
import socket
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("qiyue.llm_client")


# ─── 类型定义 ───

class Provider(str, Enum):
    DEEPSEEK = "deepseek"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    CUSTOM = "custom"
    # 添加更多 provider...


@dataclass
class LLMConfig:
    """LLM 客户端配置"""
    provider: str = "deepseek"
    model: str = "deepseek-v4-pro"
    api_key: str = ""
    api_base: str = "https://api.deepseek.com/v1"
    max_tokens: int = 8192
    temperature: float = 0.1
    top_p: float = 1.0
    timeout: float = 120.0
    max_retries: int = 3
    retry_backoff: Tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 30.0)

    # Rate limiting
    max_requests_per_minute: int = 60
    max_tokens_per_minute: int = 200_000

    # 连接
    keepalive: bool = True
    extra_headers: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, prefix: str = "LLM") -> "LLMConfig":
        """从环境变量创建配置"""
        return cls(
            provider=os.getenv(f"{prefix}_PROVIDER", "deepseek"),
            model=os.getenv(f"{prefix}_MODEL", "deepseek-v4-pro"),
            api_key=os.getenv(f"{prefix}_API_KEY", ""),
            api_base=os.getenv(f"{prefix}_API_BASE", "https://api.deepseek.com/v1"),
            max_tokens=int(os.getenv(f"{prefix}_MAX_TOKENS", "8192")),
            temperature=float(os.getenv(f"{prefix}_TEMPERATURE", "0.1")),
            timeout=float(os.getenv(f"{prefix}_TIMEOUT", "120")),
        )


# ─── Rate Limiter (令牌桶) ───

class TokenBucketRateLimiter:
    """令牌桶算法限流器"""

    def __init__(
        self,
        requests_per_minute: int = 60,
        tokens_per_minute: int = 200_000,
    ):
        self.requests_per_minute = requests_per_minute
        self.tokens_per_minute = tokens_per_minute

        # 请求令牌桶
        self._request_tokens = float(requests_per_minute)
        self._request_max = float(requests_per_minute)
        self._request_rate = requests_per_minute / 60.0  # 每秒填充

        # Token 令牌桶
        self._token_tokens = float(tokens_per_minute)
        self._token_max = float(tokens_per_minute)
        self._token_rate = tokens_per_minute / 60.0

        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        """补充令牌"""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._last_refill = now

        self._request_tokens = min(
            self._request_max,
            self._request_tokens + elapsed * self._request_rate,
        )
        self._token_tokens = min(
            self._token_max,
            self._token_tokens + elapsed * self._token_rate,
        )

    def acquire(self, estimated_tokens: int = 1000) -> float:
        """
        获取执行许可。返回需要等待的秒数 (0 = 立即执行)。

        Args:
            estimated_tokens: 预计消耗的 token 数
        """
        with self._lock:
            self._refill()

            # 检查请求令牌
            if self._request_tokens < 1.0:
                wait_requests = (1.0 - self._request_tokens) / self._request_rate
            else:
                wait_requests = 0.0

            # 检查 token 令牌
            if self._token_tokens < estimated_tokens:
                wait_tokens = (estimated_tokens - self._token_tokens) / self._token_rate
            else:
                wait_tokens = 0.0

            wait = max(wait_requests, wait_tokens)

            if wait == 0.0:
                self._request_tokens -= 1.0
                self._token_tokens -= estimated_tokens

            return wait

    async def wait_if_needed(self, estimated_tokens: int = 1000) -> None:
        """如果需要，等待直到获得许可"""
        wait = self.acquire(estimated_tokens)
        if wait > 0:
            logger.debug("[RateLimiter] Waiting %.2fs for rate limit", wait)
            await asyncio.sleep(wait)
            # 重新获取（此时应该能获取到）
            wait2 = self.acquire(estimated_tokens)
            if wait2 > 0:
                await asyncio.sleep(wait2)


# ─── 增强 LLM 客户端 ───

class EnhancedLLMClient:
    """
    增强 LLM 客户端 — 生产级 API 调用封装。

    特性:
    - 自动重试 (指数退避 + jitter)
    - Rate limiting (令牌桶)
    - 流式响应 (async generator)
    - 多 provider 支持
    - 连接保活
    - Token 使用追踪
    - 请求日志
    """

    def __init__(self, config: LLMConfig = None):
        self.config = config or LLMConfig()
        self._client: Optional[Any] = None
        self._client_lock = threading.RLock()
        self._rate_limiter = TokenBucketRateLimiter(
            requests_per_minute=self.config.max_requests_per_minute,
            tokens_per_minute=self.config.max_tokens_per_minute,
        )

        # 统计
        self.stats = {
            "total_requests": 0,
            "total_tokens": 0,
            "total_retries": 0,
            "total_errors": 0,
            "last_request_time": 0.0,
        }

    # ─── 客户端管理 ───

    @property
    def client(self) -> Any:
        """懒初始化 OpenAI 兼容客户端"""
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    self._client = self._create_client()
        return self._client

    def _create_client(self) -> Any:
        """创建 HTTP 客户端（支持 OpenAI 兼容 + Anthropic）"""
        if self.config.provider == "anthropic":
            return self._create_anthropic_client()
        return self._create_openai_compatible_client()

    def _create_openai_compatible_client(self) -> Any:
        """创建 OpenAI 兼容客户端"""
        try:
            from openai import AsyncOpenAI
        except ImportError:
            raise ImportError(
                "openai package required. Install: pip install openai"
            )

        http_client_kwargs: Dict[str, Any] = {}
        if self.config.keepalive:
            http_client_kwargs["socket_options"] = self._build_keepalive_options()

        import httpx
        http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.config.timeout, connect=30.0),
            limits=httpx.Limits(
                max_keepalive_connections=20,
                max_connections=50,
                keepalive_expiry=30.0,
            ),
            **http_client_kwargs,
        )

        extra_headers = dict(self.config.extra_headers)
        extra_headers.setdefault("User-Agent", "QiyueAgent/1.0")

        return AsyncOpenAI(
            api_key=self.config.api_key,
            base_url=self.config.api_base,
            http_client=http_client,
            default_headers=extra_headers,
            max_retries=0,  # 我们自己管理重试
        )

    def _create_anthropic_client(self) -> Any:
        """创建 Anthropic 原生客户端"""
        try:
            from anthropic import AsyncAnthropic
        except ImportError:
            raise ImportError(
                "anthropic package required for Anthropic provider. "
                "Install: pip install anthropic"
            )

        import httpx
        http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.config.timeout, connect=30.0),
            limits=httpx.Limits(
                max_keepalive_connections=10,
                max_connections=30,
                keepalive_expiry=30.0,
            ),
        )

        return AsyncAnthropic(
            api_key=self.config.api_key,
            base_url=self.config.api_base or "https://api.anthropic.com",
            http_client=http_client,
            max_retries=0,
        )

    @staticmethod
    def _build_keepalive_options() -> List[Tuple[int, int, int]]:
        """构建 TCP keepalive socket 选项"""
        opts = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
        if hasattr(socket, "TCP_KEEPIDLE"):
            opts.append((socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30))
            opts.append((socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10))
            opts.append((socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3))
        elif hasattr(socket, "TCP_KEEPALIVE"):
            opts.append((socket.IPPROTO_TCP, socket.TCP_KEEPALIVE, 30))
        return opts

    async def close(self) -> None:
        """关闭客户端连接"""
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None

    # ─── Chat (非流式) ───

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        model: str = None,
        max_tokens: int = None,
        temperature: float = None,
        tools: List[Dict[str, Any]] = None,
        tool_choice: str = None,
        json_mode: bool = False,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        发送对话请求 (非流式)。

        Returns:
            {
                "content": str,
                "tool_calls": list | None,
                "finish_reason": str,
                "model": str,
                "usage": {"prompt_tokens", "completion_tokens", "total_tokens"},
            }
        """
        # 估计 token 消耗
        estimated_tokens = self._estimate_tokens(messages)
        await self._rate_limiter.wait_if_needed(estimated_tokens)

        return await self._retry_with_backoff(
            self._do_chat,
            messages=messages,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=tools,
            tool_choice=tool_choice,
            json_mode=json_mode,
            **kwargs,
        )

    async def _do_chat(
        self,
        messages: List[Dict[str, Any]],
        model: str = None,
        max_tokens: int = None,
        temperature: float = None,
        tools: List[Dict[str, Any]] = None,
        tool_choice: str = None,
        json_mode: bool = False,
        **kwargs,
    ) -> Dict[str, Any]:
        """执行实际的 API 调用（自动适配 provider）"""
        if self.config.provider == "anthropic":
            return await self._anthropic_chat(
                messages, model, max_tokens, temperature, tools, tool_choice, **kwargs
            )
        return await self._openai_chat(
            messages, model, max_tokens, temperature, tools, tool_choice, json_mode, **kwargs
        )

    async def _openai_chat(
        self,
        messages: List[Dict[str, Any]],
        model: str = None,
        max_tokens: int = None,
        temperature: float = None,
        tools: List[Dict[str, Any]] = None,
        tool_choice: str = None,
        json_mode: bool = False,
        **kwargs,
    ) -> Dict[str, Any]:
        """OpenAI 兼容 API 调用"""
        api_kwargs: Dict[str, Any] = {
            "model": model or self.config.model,
            "messages": messages,
            "max_tokens": max_tokens or self.config.max_tokens,
            "temperature": temperature if temperature is not None else self.config.temperature,
            "top_p": self.config.top_p,
        }

        if tools:
            api_kwargs["tools"] = tools
            if tool_choice:
                api_kwargs["tool_choice"] = tool_choice

        if json_mode:
            api_kwargs["response_format"] = {"type": "json_object"}

        api_kwargs.update(kwargs)

        start = time.time()
        resp = await self.client.chat.completions.create(**api_kwargs)
        elapsed = time.time() - start

        choice = resp.choices[0]
        message = choice.message

        tool_calls = None
        if message.tool_calls:
            tool_calls = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in message.tool_calls
            ]

        usage = {}
        if resp.usage:
            usage = {
                "prompt_tokens": resp.usage.prompt_tokens,
                "completion_tokens": resp.usage.completion_tokens,
                "total_tokens": resp.usage.total_tokens,
            }

        self.stats["total_requests"] += 1
        self.stats["total_tokens"] += usage.get("total_tokens", 0)
        self.stats["last_request_time"] = elapsed

        logger.debug(
            "[LLM] %s -> %d tokens in %.2fs (%.1f tok/s)",
            resp.model, usage.get("total_tokens", 0),
            elapsed,
            usage.get("total_tokens", 0) / elapsed if elapsed > 0 else 0,
        )

        return {
            "content": message.content or "",
            "tool_calls": tool_calls,
            "finish_reason": choice.finish_reason,
            "model": resp.model,
            "usage": usage,
            "elapsed": elapsed,
        }

    async def _anthropic_chat(
        self,
        messages: List[Dict[str, Any]],
        model: str = None,
        max_tokens: int = None,
        temperature: float = None,
        tools: List[Dict[str, Any]] = None,
        tool_choice: str = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        通过 Anthropic Messages API 调用（自动翻译 OpenAI 格式 → Anthropic 格式）。

        Anthropic API 差异:
        - 端点: POST /v1/messages
        - 认证: x-api-key header
        - 系统提示: system 参数（不在 messages 中）
        - 工具定义: tools 字段（格式兼容但略有不同）
        - 响应: content 数组（text 块 + tool_use 块）
        - 无原生 json_mode（通过 system prompt 模拟）
        """
        # 1. 分离 system 消息
        system_prompt = ""
        anthropic_messages = []
        for msg in messages:
            if msg.get("role") == "system":
                system_prompt += msg.get("content", "") + "\n"
            else:
                anthropic_messages.append(msg)

        # 2. 翻译工具定义
        anthropic_tools = None
        if tools:
            anthropic_tools = []
            for tool in tools:
                fn = tool.get("function", tool)
                anthropic_tool = {
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters", {
                        "type": "object",
                        "properties": {},
                    }),
                }
                anthropic_tools.append(anthropic_tool)

        # 3. 构建 API 参数
        api_kwargs = {
            "model": model or self.config.model,
            "max_tokens": max_tokens or self.config.max_tokens,
            "messages": anthropic_messages,
        }

        if system_prompt.strip():
            api_kwargs["system"] = system_prompt.strip()
        if temperature is not None:
            api_kwargs["temperature"] = temperature
        elif self.config.temperature:
            api_kwargs["temperature"] = self.config.temperature
        if anthropic_tools:
            api_kwargs["tools"] = anthropic_tools
            if tool_choice == "required":
                # Anthropic: tool_choice = {"type": "any"}
                api_kwargs["tool_choice"] = {"type": "any"}
            elif tool_choice == "none":
                # 不传 tools 就不会调用
                api_kwargs.pop("tools", None)

        api_kwargs.update({
            k: v for k, v in kwargs.items()
            if k not in ("json_mode",)
        })

        # 4. 调用
        start = time.time()
        resp = await asyncio.to_thread(
            self.client.messages.create, **api_kwargs
        )
        elapsed = time.time() - start

        # 5. 解析响应 → OpenAI 兼容格式
        text_content = ""
        tool_calls = None
        finish_reason = resp.stop_reason or "stop"

        for block in resp.content:
            if block.type == "text":
                text_content += block.text
            elif block.type == "tool_use":
                if tool_calls is None:
                    tool_calls = []
                tool_calls.append({
                    "id": block.id,
                    "type": "function",
                    "function": {
                        "name": block.name,
                        "arguments": json.dumps(block.input, ensure_ascii=False),
                    },
                })

        usage = {}
        if resp.usage:
            usage = {
                "prompt_tokens": resp.usage.input_tokens,
                "completion_tokens": resp.usage.output_tokens,
                "total_tokens": resp.usage.input_tokens + resp.usage.output_tokens,
            }

        # 更新统计
        self.stats["total_requests"] += 1
        self.stats["total_tokens"] += usage.get("total_tokens", 0)
        self.stats["last_request_time"] = elapsed

        logger.debug(
            "[LLM] %s (Anthropic) -> %d tokens in %.2fs (%.1f tok/s)",
            resp.model, usage.get("total_tokens", 0),
            elapsed,
            usage.get("total_tokens", 0) / elapsed if elapsed > 0 else 0,
        )

        return {
            "content": text_content,
            "tool_calls": tool_calls,
            "finish_reason": finish_reason,
            "model": resp.model,
            "usage": usage,
            "elapsed": elapsed,
        }

    # ─── Stream (流式) ───

    async def stream(
        self,
        messages: List[Dict[str, Any]],
        *,
        model: str = None,
        max_tokens: int = None,
        temperature: float = None,
        tools: List[Dict[str, Any]] = None,
        **kwargs,
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        流式对话 (async generator)。

        Yields:
            {
                "content": str,           # 文本增量 (可能为空)
                "tool_calls": list | None, # 工具调用增量
                "finish_reason": str | None,
                "model": str | None,
                "usage": dict | None,      # 仅在最后一个 chunk
            }
        """
        estimated_tokens = self._estimate_tokens(messages)
        await self._rate_limiter.wait_if_needed(estimated_tokens)

        api_kwargs: Dict[str, Any] = {
            "model": model or self.config.model,
            "messages": messages,
            "max_tokens": max_tokens or self.config.max_tokens,
            "temperature": temperature if temperature is not None else self.config.temperature,
            "top_p": self.config.top_p,
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        if tools:
            api_kwargs["tools"] = tools

        api_kwargs.update(kwargs)

        start = time.time()
        total_tokens = 0

        # 流式调用 (带重试)
        for attempt_idx in range(self.config.max_retries + 1):
            try:
                stream = await self.client.chat.completions.create(**api_kwargs)
                async for chunk in stream:
                    result: Dict[str, Any] = {
                        "content": "",
                        "tool_calls": None,
                        "finish_reason": None,
                        "model": None,
                        "usage": None,
                    }

                    if chunk.choices:
                        delta = chunk.choices[0].delta
                        if delta.content:
                            result["content"] = delta.content
                        if delta.tool_calls:
                            result["tool_calls"] = [
                                {
                                    "index": tc.index,
                                    "id": tc.id,
                                    "type": "function",
                                    "function": {
                                        "name": tc.function.name or "",
                                        "arguments": tc.function.arguments or "",
                                    },
                                }
                                for tc in delta.tool_calls
                            ]
                        if chunk.choices[0].finish_reason:
                            result["finish_reason"] = chunk.choices[0].finish_reason

                    if chunk.model:
                        result["model"] = chunk.model

                    if chunk.usage:
                        result["usage"] = {
                            "prompt_tokens": chunk.usage.prompt_tokens,
                            "completion_tokens": chunk.usage.completion_tokens,
                            "total_tokens": chunk.usage.total_tokens,
                        }
                        total_tokens = result["usage"]["total_tokens"]

                    yield result

                # 流式调用成功
                break

            except Exception as e:
                self.stats["total_errors"] += 1
                if attempt_idx < self.config.max_retries and self._is_retryable(e):
                    delay = self._get_backoff_delay(attempt_idx)
                    logger.warning(
                        "[LLM] Stream attempt %d failed: %s. Retrying in %.1fs...",
                        attempt_idx + 1, str(e)[:100], delay,
                    )
                    self.stats["total_retries"] += 1
                    await asyncio.sleep(delay)
                else:
                    raise

        elapsed = time.time() - start
        self.stats["total_requests"] += 1
        self.stats["total_tokens"] += total_tokens
        self.stats["last_request_time"] = elapsed

    # ─── Chat JSON ───

    async def chat_json(
        self,
        messages: List[Dict[str, Any]],
        schema: Dict[str, Any] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """发送对话，强制返回 JSON"""
        msgs = list(messages)
        if schema:
            msgs.insert(0, {
                "role": "system",
                "content": (
                    f"Respond with valid JSON only. "
                    f"Follow this schema:\n{json.dumps(schema, ensure_ascii=False)}"
                ),
            })

        resp = await self.chat(msgs, json_mode=True, **kwargs)
        content = resp["content"]

        # 尝试直接解析
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass

        # 尝试提取 JSON 块
        import re
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass

        return {"raw": content, "error": "JSON parse failed"}

    # ─── 批量调用 ───

    async def batch_chat(
        self,
        requests: List[Dict[str, Any]],
        max_concurrent: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        批量并发调用。

        Args:
            requests: [{"messages": [...], "model": "...", ...}, ...]
            max_concurrent: 最大并发数

        Returns:
            按顺序返回结果列表
        """
        semaphore = asyncio.Semaphore(max_concurrent)

        async def bounded(req):
            async with semaphore:
                return await self.chat(**req)

        tasks = [bounded(req) for req in requests]
        return await asyncio.gather(*tasks, return_exceptions=True)

    # ─── 重试逻辑 ───

    async def _retry_with_backoff(self, func: Callable, *args, **kwargs) -> Any:
        """带指数退避的重试包装器"""
        last_error = None

        for attempt_idx in range(self.config.max_retries + 1):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                last_error = e
                self.stats["total_errors"] += 1

                if attempt_idx < self.config.max_retries and self._is_retryable(e):
                    delay = self._get_backoff_delay(attempt_idx)
                    logger.warning(
                        "[LLM] Attempt %d/%d failed: %s. Retrying in %.1fs...",
                        attempt_idx + 1, self.config.max_retries + 1,
                        str(e)[:120], delay,
                    )
                    self.stats["total_retries"] += 1
                    await asyncio.sleep(delay)
                else:
                    break

        raise last_error

    def _get_backoff_delay(self, attempt: int) -> float:
        """计算退避延迟 (指数退避 + jitter)"""
        if attempt < len(self.config.retry_backoff):
            base = self.config.retry_backoff[attempt]
        else:
            base = self.config.retry_backoff[-1]
        jitter = base * 0.2 * random.random()
        return base + jitter

    @staticmethod
    def _is_retryable(error: Exception) -> bool:
        """检查错误是否可重试"""
        msg = str(error).lower()

        # 不可重试
        non_retryable = [
            "invalid api key", "unauthorized", "authentication",
            "model not found", "does not exist",
            "content filter", "content policy violation",
            "context length exceeded", "too many tokens",
            "invalid request", "bad request",
        ]
        for keyword in non_retryable:
            if keyword in msg:
                return False

        # 可重试
        retryable = [
            "rate limit", "too many requests", "429",
            "server error", "500", "502", "503", "504",
            "timeout", "timed out",
            "connection", "network",
            "overloaded",
        ]
        for keyword in retryable:
            if keyword in msg:
                return True

        # 默认：5xx 可重试，4xx 不可
        if "status" in msg or "error" in msg:
            for code in ["500", "502", "503", "504", "529"]:
                if code in msg:
                    return True

        return False

    # ─── Token 估算 ───

    @staticmethod
    def _estimate_tokens(messages: List[Dict[str, Any]]) -> int:
        """粗略估算消息的 token 数"""
        total = 0
        for msg in messages:
            content = msg.get("content", "") or ""
            # 简单估算: 1 token ≈ 4 characters (conservative)
            total += len(str(content)) // 3

            # 工具调用
            tool_calls = msg.get("tool_calls") or []
            for tc in tool_calls:
                fn = tc.get("function", {})
                total += len(str(fn.get("arguments", ""))) // 3
                total += 20  # overhead per tool call

        return max(total, 100)  # 最少 100 tokens

    # ─── Provider 切换 ───

    def switch_provider(self, provider: str, api_key: str = None, api_base: str = None) -> None:
        """动态切换 provider"""
        self.config.provider = provider
        if api_key:
            self.config.api_key = api_key
        if api_base:
            self.config.api_base = api_base
        # 强制重建客户端
        old_client = self._client
        self._client = None
        if old_client:
            try:
                import asyncio as _asyncio
                loop = _asyncio.get_event_loop()
                if loop.is_running():
                    _asyncio.create_task(old_client.close())
                else:
                    _asyncio.run(old_client.close())
            except Exception:
                pass
        logger.info("[LLM] Switched provider to: %s", provider)

    # ─── 健康检查 ───

    async def health_check(self) -> Dict[str, Any]:
        """检查 API 连接健康状态"""
        try:
            resp = await self.chat(
                [{"role": "user", "content": "ping"}],
                max_tokens=1,
                temperature=0,
            )
            return {
                "healthy": True,
                "model": resp.get("model", "unknown"),
                "latency_ms": resp.get("elapsed", 0) * 1000,
            }
        except Exception as e:
            return {
                "healthy": False,
                "error": str(e)[:200],
            }

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()


def create_llm_client(
    provider: str = "deepseek",
    api_key: str = None,
    model: str = "deepseek-v4-pro",
    **kwargs,
) -> "EnhancedLLMClient":
    """快速创建 LLM 客户端"""
    import os
    config = LLMConfig(
        provider=provider,
        api_key=api_key or os.getenv("DEEPSEEK_API_KEY", ""),
        api_base=kwargs.get("api_base", "https://api.deepseek.com/v1"),
        model=model,
        max_tokens=kwargs.get("max_tokens", 8192),
        temperature=kwargs.get("temperature", 0.1),
        max_retries=kwargs.get("max_retries", 3),
        rate_limit_rpm=kwargs.get("rate_limit_rpm", 0),
        rate_limit_tpm=kwargs.get("rate_limit_tpm", 0),
        timeout=kwargs.get("timeout", 60.0),
    )
    return EnhancedLLMClient(config)


# 别名
LLMProvider = Provider
