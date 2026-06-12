"""
思维层 — Hermes 本体推理循环

一个 while 循环：LLM 拿到工具后自己决定调什么、调几次、何时停。
不做意图解析/任务规划/翻译官等多余抽象。
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Optional

from shared.core import (
    IncomingMessage, OutgoingMessage,
    LLMClient, LLMConfig, EventBus,
)
from data.session_store import SessionStore
from brain.persona import (
    Persona, DEFAULT_PERSONA, build_system_prompt, get_emotion_strategy,
)
from config.defaults import CORE_FILES, ROOT

logger = logging.getLogger("qiyue.brain")


class Brain:
    """思维中枢 — Hermes 式单循环 tool calling"""

    MAX_ITERATIONS = 10  # Nodus 单次对话最多 10 轮工具调用

    def __init__(self, llm: LLMClient, bus: EventBus,
                 sessions: SessionStore = None,
                 executor=None,
                 memory=None,
                 persona: Persona = None):
        self.llm = llm
        self.bus = bus
        self.sessions = sessions or SessionStore()
        self.executor = executor
        self.memory = memory  # MemoryStore (持久记忆，备用)
        self.persona = persona or DEFAULT_PERSONA

        self._available_skills: list[str] = []
        self._available_tools: dict = {}  # name -> description

    def register_skill_loader(self, skill_loader):
        """注入技能加载器（接口兼容）"""
        if skill_loader:
            self._available_skills = [s.slug for s in skill_loader.list_all()]

    def register_tools(self, tools: dict):
        """注册可用工具 (name -> description)"""
        self._available_tools = tools

    async def handle(self, msg: IncomingMessage,
                     session_id: str = None,
                     context: list[dict] = None,
                     emotion: dict = None,
                     **kwargs):
        """
        处理一条用户消息。

        1. 构建 system prompt（persona + 工具 + 核心文件 + 红线）
        2. 启动 tool calling 循环
        3. LLM 自己决定何时调工具、何时回复
        4. 保存会话 + 推送回复
        """
        start = time.time()
        sid = session_id or f"unified:{msg.channel_id}"
        emotion_tag = (emotion or {}).get("emotion", "neutral")

        # ── 1. 构建 system prompt ──
        persona_prompt = build_system_prompt(self.persona, role="回复生成")

        core_files_str = "\n".join(
            f"- {name}: {desc}" for name, desc in CORE_FILES.items()
        )
        tools_str = "\n".join(
            f"- {name}: {desc}" for name, desc in self._available_tools.items()
        ) if self._available_tools else "(无)"

        skills_str = ", ".join(self._available_skills) if self._available_skills else "(无)"

        emotion_guide = get_emotion_strategy(self.persona, emotion_tag)

        system_prompt = f"""{persona_prompt}

## 项目信息
项目根目录: {ROOT}

## 核心文件
{core_files_str}

## 可用工具
{tools_str}

## 已加载技能
{skills_str}

## 行为规范
- 需要获取信息时调用工具，禁止编造数据
- 工具返回结果后直接回复用户，不要反复调用同一个工具
- 用自然口语化中文回复，像朋友聊天，不用客服腔
- 用户说「别动」必须立即停止一切操作
- 禁止向生产通道发送测试消息
- 你是单一进程，不需要启动任何外部服务（没有 OpenClaw/NanoBot/Bridge）

## 用户当前情绪
{emotion_tag}
应对策略: {emotion_guide}
"""

        # ── 2. 构建消息列表 ──
        api_messages = [{"role": "system", "content": system_prompt}]

        # 注入上下文
        if context:
            for m in context[-12:]:
                role = m.get("role", "user")
                content = m.get("content", "")[:500]
                api_messages.append({"role": role, "content": content})

        # 注入持久记忆（如果有 MemoryStore）
        if self.memory and hasattr(self.memory, 'content'):
            mem_content = getattr(self.memory, 'content', '')
            if mem_content:
                api_messages.append({
                    "role": "system",
                    "content": f"## 长期记忆\n{mem_content[:3000]}"
                })

        # 用户消息
        api_messages.append({"role": "user", "content": msg.content})

        # ── 3. 构建 OpenAI 工具格式 ──
        openai_tools = [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": desc,
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                },
            }
            for name, desc in self._available_tools.items()
        ] if self._available_tools else None

        # ── 4. 工具调用循环 ──
        final_response = ""
        for iteration in range(self.MAX_ITERATIONS):
            try:
                resp = await self.llm.chat_with_tools(
                    api_messages,
                    tools=openai_tools,
                    temperature=0.7,
                )
            except Exception as e:
                logger.error(f"[Brain] LLM call failed (iteration {iteration}): {e}")
                final_response = "抱歉，我暂时无法处理这个消息，稍后再试？"
                break

            tool_calls = resp.get("tool_calls") or []
            content = resp.get("content") or ""

            # 没有工具调用 → 最终回复
            if not tool_calls:
                final_response = content or "好的~"
                break

            # 有工具调用 → 进度提示 + 追加 assistant 消息
            tool_names = [tc.get("function", {}).get("name", "?") for tc in tool_calls]
            print(f"\n  🔧 {', '.join(tool_names)}...")

            api_messages.append({
                "role": "assistant",
                "content": content or None,
                "tool_calls": tool_calls,
            })

            # 执行工具
            for tc in tool_calls:
                fn = tc.get("function", {})
                tool_name = fn.get("name", "")
                try:
                    tool_args = json.loads(fn.get("arguments", "{}"))
                except json.JSONDecodeError:
                    tool_args = {}

                t0 = time.time()
                if self.executor:
                    try:
                        result = await self.executor.execute(tool_name, tool_args)
                        tool_output = str(result)
                    except Exception as e:
                        tool_output = f"错误: {e}"
                else:
                    tool_output = "错误: 无执行层"
                dt = (time.time() - t0) * 1000

                # 进度：工具执行耗时
                if dt < 1000:
                    print(f"    ✓ {tool_name} ({dt:.0f}ms)")
                else:
                    print(f"    ✓ {tool_name} ({dt/1000:.1f}s)")

                # 截断过长的工具输出
                if len(tool_output) > 8000:
                    tool_output = tool_output[:8000] + "\n...(内容已截断)"

                api_messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": tool_output,
                })

        else:
            # 达到最大迭代次数 → 强制让 LLM 总结
            logger.warning(f"[Brain] Max iterations ({self.MAX_ITERATIONS}) reached, forcing summary")
            try:
                api_messages.append({
                    "role": "user",
                    "content": "请基于以上工具调用结果，给出最终回复。"
                })
                final = await self.llm.chat(api_messages, temperature=0.7)
                final_response = final or "处理完成。"
            except Exception:
                final_response = "处理完成，但遇到了些问题。"

        # ── 5. 保存会话 ──
        try:
            self.sessions.append_exchange(sid, msg.content, final_response)
        except Exception as e:
            logger.error(f"[Brain] Session save failed: {e}")

        # ── 6. 推送回复 ──
        elapsed = (time.time() - start) * 1000
        logger.info(f"[{sid}] Done in {elapsed:.0f}ms, {len(api_messages)} messages")

        self.bus.emit("response.ready",
                       message_id=msg.id,
                       content=final_response,
                       session_id=sid,
                       platform=msg.platform,
                       channel_id=msg.channel_id,
                       elapsed_ms=elapsed)
