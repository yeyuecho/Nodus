"""
LLM 对话循环 — 工具调用多轮迭代
来源: Hermes run_agent.py agent loop 模式

核心循环:
  while iteration < max_iterations:
      response = llm.chat(messages, tools)
      if response.has_tool_calls:
          for each tool_call:
              result = execute_tool(name, args)
              messages.append(tool_result)
      else:
          return response  (最终回复)

特性:
- tool_choice 控制 (auto / required / none / specific)
- 多轮工具调用迭代 (最多 N 轮)
- 错误重试 (指数退避 + jitter)
- token 预算管理
- 流式输出支持
- 中断检测
- 并行/顺序工具执行
"""

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("qiyue.llm_loop")


# ─── 类型定义 ───

class LoopExitReason(str, Enum):
    TEXT_RESPONSE = "text_response"
    MAX_ITERATIONS = "max_iterations"
    BUDGET_EXHAUSTED = "budget_exhausted"
    INTERRUPTED = "interrupted"
    ALL_RETRIES_EXHAUSTED = "all_retries_exhausted"
    EMPTY_RESPONSE = "empty_response"
    ERROR = "error"
    TOOL_ERROR_LIMIT = "tool_error_limit"


class ToolChoice(str, Enum):
    AUTO = "auto"
    REQUIRED = "required"
    NONE = "none"


@dataclass
class ToolDefinition:
    """工具定义 (OpenAI function calling 格式)"""
    name: str
    description: str
    parameters: Dict[str, Any] = field(default_factory=dict)

    def to_openai_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.parameters.get("properties", {}),
                    "required": self.parameters.get("required", []),
                },
            },
        }


@dataclass
class ToolCall:
    """工具调用"""
    id: str
    name: str
    arguments: Dict[str, Any]

    @classmethod
    def from_openai(cls, tc: Any) -> "ToolCall":
        if hasattr(tc, "id"):
            call_id = tc.id
            fn = tc.function
        else:
            call_id = tc.get("id", "")
            fn = tc.get("function", {})
        args = fn.arguments if hasattr(fn, "arguments") else fn.get("arguments", "{}")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        return cls(
            id=call_id,
            name=fn.name if hasattr(fn, "name") else fn.get("name", ""),
            arguments=args,
        )


@dataclass
class ToolResult:
    """工具执行结果"""
    call_id: str
    name: str
    output: Any
    error: Optional[str] = None
    duration_ms: float = 0.0

    def to_message(self) -> Dict[str, Any]:
        content = json.dumps(self.output, ensure_ascii=False) if not isinstance(self.output, str) else self.output
        if self.error:
            content = f"Error: {self.error}\n\nOutput: {content}"
        return {
            "role": "tool",
            "tool_call_id": self.call_id,
            "name": self.name,
            "content": content,
        }


@dataclass
class LLMResponse:
    """LLM 响应"""
    content: str
    tool_calls: List[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    model: str = ""
    usage: Dict[str, int] = field(default_factory=dict)

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0

    @property
    def total_tokens(self) -> int:
        return self.usage.get("total_tokens", 0)


@dataclass
class LoopResult:
    """对话循环结果"""
    final_response: str
    messages: List[Dict[str, Any]]
    exit_reason: LoopExitReason
    iterations: int
    total_tokens: int = 0
    tool_calls_made: int = 0
    duration_ms: float = 0.0
    errors: List[str] = field(default_factory=list)
    budget_consumed: int = 0  # 消耗的 iteration_budget
    budget_remaining: int = 0  # 剩余的 iteration_budget


# ─── Token 预算管理器 ───

@dataclass
class TokenBudget:
    """Token 预算追踪器 — 等价于 Hermes AIAgent.iteration_budget"""
    total: int = 200_000      # 总 budget（剩余 iteration 数）
    remaining: int = 200_000  # 剩余 budget
    last_reset: float = 0.0

    def consume(self, count: int = 1) -> bool:
        """消耗 budget，返回是否有剩余"""
        self.remaining -= count
        return self.remaining > 0

    def is_exhausted(self) -> bool:
        return self.remaining <= 0

    def reset(self, total: int):
        self.total = total
        self.remaining = total
        self.last_reset = time.time()


# ─── 对话循环 ───

class LLMLoop:
    """
    LLM 工具调用循环管理器

    管理多轮对话和工具调用的核心循环。
    独立于具体的 LLM 客户端实现。

    特性:
    - Token 预算管理 (iteration_budget 模式)
    - 单轮宽限调用 (budget_grace_call)
    - 多轮工具调用迭代
    - 错误重试 (指数退避 + jitter)
    - 流式输出支持
    - 中断检测
    - 并行/顺序工具执行
    """

    # 默认配置
    DEFAULT_MAX_ITERATIONS: int = 90
    DEFAULT_MAX_TOOL_ERRORS: int = 10
    DEFAULT_TOOL_TIMEOUT: float = 300.0
    DEFAULT_RETRY_BACKOFF: Tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 30.0)
    MAX_PARALLEL_TOOLS: int = 8

    def __init__(
        self,
        llm_client: Any,  # LLMClient (duck typed)
        tool_executor: Callable[[str, Dict[str, Any]], Any],
        *,
        max_iterations: int = None,
        max_tool_errors: int = None,
        tool_timeout: float = None,
        token_budget: int = None,  # 新增: token 预算
    ):
        self.llm = llm_client
        self._execute_tool = tool_executor
        self.max_iterations = max_iterations or self.DEFAULT_MAX_ITERATIONS
        self.max_tool_errors = max_tool_errors or self.DEFAULT_MAX_TOOL_ERRORS
        self.tool_timeout = tool_timeout or self.DEFAULT_TOOL_TIMEOUT

        # Token 预算
        self.budget = TokenBudget(
            total=token_budget or self.DEFAULT_MAX_ITERATIONS * 2000,
            remaining=token_budget or self.DEFAULT_MAX_ITERATIONS * 2000,
        )
        self._budget_grace_call: bool = False  # 单轮宽限

        # 运行时状态
        self._interrupt_requested = False
        self._iteration_count = 0
        self._tool_error_count = 0
        self._total_tokens = 0

    def request_interrupt(self) -> None:
        """请求中断当前循环"""
        self._interrupt_requested = True

    async def run(
        self,
        messages: List[Dict[str, Any]],
        tools: List[ToolDefinition] = None,
        *,
        system_message: str = None,
        tool_choice: ToolChoice = ToolChoice.AUTO,
        stream_callback: Callable[[str], None] = None,
        tool_progress_callback: Callable[[str, str], None] = None,
    ) -> LoopResult:
        """
        执行对话循环直到模型返回最终回复。

        Args:
            messages: 初始消息列表 (至少包含一个 user 消息)
            tools: 可用工具定义列表
            system_message: 系统提示 (注入为第一条消息)
            tool_choice: 工具选择策略
            stream_callback: 流式输出回调 (每个文本 delta)
            tool_progress_callback: 工具执行进度回调 (tool_name, status)

        Returns:
            LoopResult with final response and metadata
        """
        start_time = time.time()
        self._interrupt_requested = False
        self._iteration_count = 0
        self._tool_error_count = 0
        self._total_tokens = 0
        self._budget_grace_call = False

        errors: List[str] = []
        total_tool_calls = 0
        budget_start = self.budget.remaining

        # 构建消息列表
        msgs = list(messages)
        if system_message:
            msgs.insert(0, {"role": "system", "content": system_message})

        # 转换工具定义
        tool_schemas = [t.to_openai_schema() for t in (tools or [])] if tools else None

        # 主循环 — 包含 budget 检查
        while True:
            # ── 迭代数检查 ──
            if self._iteration_count >= self.max_iterations:
                if not self._budget_grace_call:
                    break

            # ── Budget 检查（含单轮宽限）──
            if self.budget.is_exhausted() and not self._budget_grace_call:
                logger.warning("[LLMLoop] Budget exhausted at %d tokens", self._total_tokens)
                return LoopResult(
                    final_response="",
                    messages=msgs,
                    exit_reason=LoopExitReason.BUDGET_EXHAUSTED,
                    iterations=self._iteration_count,
                    total_tokens=self._total_tokens,
                    tool_calls_made=total_tool_calls,
                    duration_ms=(time.time() - start_time) * 1000,
                    errors=errors,
                    budget_consumed=budget_start - self.budget.remaining,
                    budget_remaining=self.budget.remaining,
                )

            if self._interrupt_requested:
                return LoopResult(
                    final_response="",
                    messages=msgs,
                    exit_reason=LoopExitReason.INTERRUPTED,
                    iterations=self._iteration_count,
                    total_tokens=self._total_tokens,
                    tool_calls_made=total_tool_calls,
                    duration_ms=(time.time() - start_time) * 1000,
                    errors=errors,
                    budget_consumed=budget_start - self.budget.remaining,
                    budget_remaining=self.budget.remaining,
                )

            # ── API 调用 ──
            try:
                response = await self._api_call_with_retry(
                    msgs, tool_schemas, tool_choice, stream_callback
                )
            except Exception as e:
                logger.error("[LLMLoop] API call failed: %s", e)
                errors.append(f"API error: {e}")
                return LoopResult(
                    final_response="",
                    messages=msgs,
                    exit_reason=LoopExitReason.ALL_RETRIES_EXHAUSTED,
                    iterations=self._iteration_count,
                    total_tokens=self._total_tokens,
                    tool_calls_made=total_tool_calls,
                    duration_ms=(time.time() - start_time) * 1000,
                    errors=errors,
                    budget_consumed=budget_start - self.budget.remaining,
                    budget_remaining=self.budget.remaining,
                )

            self._iteration_count += 1
            self._total_tokens += response.total_tokens

            # 消耗 budget
            budget_cost = response.total_tokens or 2000
            self.budget.consume(budget_cost)

            # ── 无工具调用 → 最终回复 ──
            if not response.has_tool_calls:
                if response.content:
                    msgs.append({
                        "role": "assistant",
                        "content": response.content,
                    })
                return LoopResult(
                    final_response=response.content,
                    messages=msgs,
                    exit_reason=LoopExitReason.TEXT_RESPONSE,
                    iterations=self._iteration_count,
                    total_tokens=self._total_tokens,
                    tool_calls_made=total_tool_calls,
                    duration_ms=(time.time() - start_time) * 1000,
                    errors=errors,
                    budget_consumed=budget_start - self.budget.remaining,
                    budget_remaining=self.budget.remaining,
                )

            # ── 有工具调用 → 执行 ──
            if response.tool_calls:
                total_tool_calls += len(response.tool_calls)

            # 构建 assistant 消息（含 tool_calls）
            assistant_msg = {
                "role": "assistant",
                "content": response.content or None,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                    for tc in response.tool_calls
                ],
            }
            msgs.append(assistant_msg)

            # 去重
            unique_calls = self._deduplicate_tool_calls(response.tool_calls)
            if len(unique_calls) < len(response.tool_calls):
                logger.warning(
                    "[LLMLoop] Removed %d duplicate tool calls",
                    len(response.tool_calls) - len(unique_calls),
                )

            # 执行工具调用
            if len(unique_calls) == 1:
                tool_results = await self._execute_single_tool(
                    unique_calls[0], tool_progress_callback
                )
            else:
                tool_results = await self._execute_parallel_tools(
                    unique_calls[:self.MAX_PARALLEL_TOOLS], tool_progress_callback
                )

            # 添加工具结果
            for result in tool_results:
                msgs.append(result.to_message())
                if result.error:
                    self._tool_error_count += 1
                    errors.append(f"Tool '{result.name}' error: {result.error}")

            # 工具错误限制
            if self._tool_error_count >= self.max_tool_errors:
                logger.warning(
                    "[LLMLoop] Tool error limit reached (%d). Stopping loop.",
                    self._tool_error_count,
                )
                try:
                    final_response = await self._api_call_with_retry(
                        msgs, tool_schemas, ToolChoice.NONE, stream_callback
                    )
                    if final_response.content:
                        msgs.append({"role": "assistant", "content": final_response.content})
                        return LoopResult(
                            final_response=final_response.content,
                            messages=msgs,
                            exit_reason=LoopExitReason.TOOL_ERROR_LIMIT,
                            iterations=self._iteration_count,
                            total_tokens=self._total_tokens,
                            tool_calls_made=total_tool_calls,
                            duration_ms=(time.time() - start_time) * 1000,
                            errors=errors,
                            budget_consumed=budget_start - self.budget.remaining,
                            budget_remaining=self.budget.remaining,
                        )
                except Exception:
                    pass

                return LoopResult(
                    final_response="",
                    messages=msgs,
                    exit_reason=LoopExitReason.TOOL_ERROR_LIMIT,
                    iterations=self._iteration_count,
                    total_tokens=self._total_tokens,
                    tool_calls_made=total_tool_calls,
                    duration_ms=(time.time() - start_time) * 1000,
                    errors=errors,
                    budget_consumed=budget_start - self.budget.remaining,
                    budget_remaining=self.budget.remaining,
                )

        # ── 达到最大迭代次数 ──
        logger.warning(
            "[LLMLoop] Max iterations reached (%d).",
            self.max_iterations,
        )
        return LoopResult(
            final_response="",
            messages=msgs,
            exit_reason=LoopExitReason.MAX_ITERATIONS,
            iterations=self._iteration_count,
            total_tokens=self._total_tokens,
            tool_calls_made=total_tool_calls,
            duration_ms=(time.time() - start_time) * 1000,
            errors=errors,
            budget_consumed=budget_start - self.budget.remaining,
            budget_remaining=self.budget.remaining,
        )

    # ─── API 调用 (含重试) ───

    async def _api_call_with_retry(
        self,
        messages: List[Dict[str, Any]],
        tool_schemas: Optional[List[Dict[str, Any]]],
        tool_choice: ToolChoice,
        stream_callback: Optional[Callable],
    ) -> LLMResponse:
        """带指数退避重试的 API 调用"""
        last_error = None

        for attempt, delay in enumerate(self._backoff_sequence()):
            try:
                return await self._single_api_call(
                    messages, tool_schemas, tool_choice, stream_callback
                )
            except Exception as e:
                last_error = e
                error_msg = str(e)[:200]

                # 不可重试的错误
                if self._is_non_retryable(e):
                    raise

                logger.warning(
                    "[LLMLoop] API call attempt %d failed: %s. Retrying in %.1fs...",
                    attempt + 1, error_msg, delay,
                )

                if self._interrupt_requested:
                    raise

                await asyncio.sleep(delay)

        raise last_error

    async def _single_api_call(
        self,
        messages: List[Dict[str, Any]],
        tool_schemas: Optional[List[Dict[str, Any]]],
        tool_choice: ToolChoice,
        stream_callback: Optional[Callable],
    ) -> LLMResponse:
        """单次 API 调用 (非流式或流式)"""
        # 构建 API 参数
        kwargs: Dict[str, Any] = {
            "messages": messages,
        }
        if tool_schemas:
            kwargs["tools"] = tool_schemas
            if tool_choice == ToolChoice.REQUIRED:
                kwargs["tool_choice"] = "required"
            elif tool_choice == ToolChoice.NONE:
                kwargs["tool_choice"] = "none"
            # AUTO: 不设置 tool_choice，使用默认行为

        # 流式调用
        if stream_callback:
            return await self._stream_api_call(kwargs, stream_callback)

        # 非流式调用
        resp = await self.llm.chat(**kwargs)

        tool_calls = []
        raw_tool_calls = resp.get("tool_calls") or []
        for tc in raw_tool_calls:
            try:
                tool_calls.append(ToolCall.from_openai(tc))
            except Exception as e:
                logger.warning("[LLMLoop] Failed to parse tool call: %s", e)

        return LLMResponse(
            content=resp.get("content", "") or "",
            tool_calls=tool_calls,
            finish_reason=resp.get("finish_reason", "stop"),
            model=resp.get("model", ""),
            usage=resp.get("usage", {}),
        )

    async def _stream_api_call(
        self,
        kwargs: Dict[str, Any],
        stream_callback: Callable[[str], None],
    ) -> LLMResponse:
        """流式 API 调用，累积内容和工具调用"""
        accumulated_content: List[str] = []
        accumulated_tool_calls: Dict[int, Dict[str, Any]] = {}
        finish_reason = "stop"
        model = ""
        usage = {}

        try:
            async for chunk in self.llm.stream(**kwargs):
                # 文本增量
                delta = chunk.get("content") or ""
                if delta:
                    accumulated_content.append(delta)
                    try:
                        stream_callback(delta)
                    except Exception:
                        pass

                # 工具调用增量
                tool_deltas = chunk.get("tool_calls") or []
                for td in tool_deltas:
                    idx = td.get("index", 0)
                    if idx not in accumulated_tool_calls:
                        accumulated_tool_calls[idx] = {
                            "id": td.get("id", ""),
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        }
                    entry = accumulated_tool_calls[idx]
                    if td.get("id"):
                        entry["id"] = td["id"]
                    fn = td.get("function", {})
                    if fn.get("name"):
                        entry["function"]["name"] += fn["name"]
                    if fn.get("arguments"):
                        entry["function"]["arguments"] += fn["arguments"]

                # 结束原因
                if chunk.get("finish_reason"):
                    finish_reason = chunk["finish_reason"]
                if chunk.get("model"):
                    model = chunk["model"]
                if chunk.get("usage"):
                    usage = chunk["usage"]

        except Exception as e:
            logger.error("[LLMLoop] Stream error: %s", e)
            # 如果累积了部分内容，返回部分结果
            if accumulated_content:
                pass  # 继续处理部分结果
            else:
                raise

        # 解析工具调用
        tool_calls = []
        for tc_data in sorted(accumulated_tool_calls.values(), key=lambda x: list(accumulated_tool_calls.keys())[list(accumulated_tool_calls.values()).index(x)]):
            try:
                args_str = tc_data.get("function", {}).get("arguments", "{}")
                tool_calls.append(ToolCall(
                    id=tc_data.get("id", ""),
                    name=tc_data.get("function", {}).get("name", ""),
                    arguments=json.loads(args_str) if args_str else {},
                ))
            except Exception as e:
                logger.warning("[LLMLoop] Failed to parse streamed tool call: %s", e)

        return LLMResponse(
            content="".join(accumulated_content),
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            model=model,
            usage=usage,
        )

    # ─── 工具执行 ───

    async def _execute_single_tool(
        self,
        tool_call: ToolCall,
        progress_cb: Optional[Callable],
    ) -> List[ToolResult]:
        """执行单个工具调用"""
        if progress_cb:
            try:
                progress_cb(tool_call.name, "started")
            except Exception:
                pass

        start = time.time()
        try:
            output = self._execute_tool(tool_call.name, tool_call.arguments)
            if asyncio.iscoroutine(output):
                output = await asyncio.wait_for(output, timeout=self.tool_timeout)

            duration = (time.time() - start) * 1000

            if progress_cb:
                try:
                    progress_cb(tool_call.name, "completed")
                except Exception:
                    pass

            return [ToolResult(
                call_id=tool_call.id,
                name=tool_call.name,
                output=output,
                duration_ms=duration,
            )]

        except asyncio.TimeoutError:
            logger.error("[LLMLoop] Tool timeout: %s (%.1fs)", tool_call.name, self.tool_timeout)
            return [ToolResult(
                call_id=tool_call.id,
                name=tool_call.name,
                output="",
                error=f"Tool timed out after {self.tool_timeout}s",
                duration_ms=self.tool_timeout * 1000,
            )]
        except Exception as e:
            logger.error("[LLMLoop] Tool error: %s → %s", tool_call.name, e)
            return [ToolResult(
                call_id=tool_call.id,
                name=tool_call.name,
                output="",
                error=str(e),
                duration_ms=(time.time() - start) * 1000,
            )]

    async def _execute_parallel_tools(
        self,
        tool_calls: List[ToolCall],
        progress_cb: Optional[Callable],
    ) -> List[ToolResult]:
        """并行执行多个工具调用"""
        tasks = [
            self._execute_single_tool(tc, None)
            for tc in tool_calls
        ]
        results_list = await asyncio.gather(*tasks)
        # Flatten: each task returns [ToolResult]
        all_results = []
        for results in results_list:
            all_results.extend(results)

        if progress_cb:
            try:
                progress_cb("batch", f"completed ({len(all_results)} tools)")
            except Exception:
                pass

        return all_results

    # ─── 辅助 ───

    def _backoff_sequence(self):
        """生成指数退避序列 (带 jitter)"""
        for delay in self.DEFAULT_RETRY_BACKOFF:
            jitter = delay * 0.2 * random.random()
            yield delay + jitter
        # 最后一个延迟持续重复
        last = self.DEFAULT_RETRY_BACKOFF[-1]
        while True:
            jitter = last * 0.2 * random.random()
            yield last + jitter

    @staticmethod
    def _is_non_retryable(error: Exception) -> bool:
        """检查错误是否不可重试"""
        msg = str(error).lower()
        non_retryable = [
            "invalid api key", "unauthorized", "authentication",
            "model not found", "does not exist",
            "content filter", "content policy",
            "context length exceeded", "too many tokens",
        ]
        for keyword in non_retryable:
            if keyword in msg:
                return True
        return False

    @staticmethod
    def _deduplicate_tool_calls(tool_calls: List[ToolCall]) -> List[ToolCall]:
        """移除同一轮内的重复工具调用"""
        seen: Set[Tuple[str, str]] = set()
        unique: List[ToolCall] = []
        for tc in tool_calls:
            key = (tc.name, json.dumps(tc.arguments, sort_keys=True))
            if key not in seen:
                seen.add(key)
                unique.append(tc)
        return unique

    # ─── 统计 ───

    @property
    def stats(self) -> Dict[str, Any]:
        return {
            "iterations": self._iteration_count,
            "total_tokens": self._total_tokens,
            "tool_errors": self._tool_error_count,
            "interrupted": self._interrupt_requested,
        }
