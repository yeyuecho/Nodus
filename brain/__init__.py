"""
思维层 — 直接从 Hermes AIAgent 提取的核心推理引擎

无 IntentParser、TaskPlanner 等中间抽象层。
一次 LLM 调用，自己决定是否调工具，循环直到回复。
"""

import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Optional

from shared.core import (
    IncomingMessage, OutgoingMessage, IntentResult, ExecutionPlan,
    TaskResult, LLMClient, LLMConfig, EventBus, SubTask, TaskType,
    Platform,
)
from data.session_store import SessionStore
from brain.persona import (
    Persona, DEFAULT_PERSONA, build_system_prompt, get_emotion_strategy,
    _INJECTED_SOUL, _INJECTED_MEMORY,
)
from config.defaults import (
    ROOT as PROJECT_ROOT,
    CORE_FILES,
    TOOL_DESCRIPTIONS,
    SYSTEM_PROMPT_TEMPLATE,
)

logger = logging.getLogger("qiyue.brain")


class Brain:
    """思维中枢 — 直接从 Hermes AIAgent 提取的推理循环"""

    def __init__(self, llm: LLMClient, bus: EventBus,
                 sessions: SessionStore = None,
                 executor=None,
                 memory=None,
                 persona: Persona = None):
        self.llm = llm
        self.bus = bus
        self.sessions = sessions or SessionStore()
        self.executor = executor
        self.memory = memory
        self.persona = persona or DEFAULT_PERSONA
        self._available_tools: dict = {}

    def register_tools(self, tools: dict):
        self._available_tools = tools

    def register_skill_loader(self, skill_loader):
        pass  # 技能引擎预留接口

    # ─── 进度显示 ───

    def _show_stage(self, stage: str):
        labels = {"intent": "理解中", "executing": "处理中", "replying": "回复中"}
        sys.stdout.write(f"\r  {labels.get(stage, stage)}...  ")
        sys.stdout.flush()

    def _clear_stage(self):
        sys.stdout.write("\r" + " " * 30 + "\r")
        sys.stdout.flush()

    # ─── 工具定义 ───

    def _build_tool_defs(self) -> list:
        specs = {
            "shell_exec": ("执行命令", {"command": {"type": "string", "description": "命令，如 systeminfo / dir"}}),
            "file_read": ("读文件", {"path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}}),
            "file_write": ("写文件", {"path": {"type": "string"}, "content": {"type": "string"}}),
            "file_search": ("搜索文件内容", {"pattern": {"type": "string"}, "path": {"type": "string"}}),
            "file_find": ("查找文件名", {"pattern": {"type": "string"}, "path": {"type": "string"}}),
            "web_search": ("网络搜索", {"query": {"type": "string"}}),
        }
        defs = []
        for name, (desc, props) in specs.items():
            if name in self._available_tools:
                defs.append({
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": desc,
                        "parameters": {
                            "type": "object",
                            "properties": props,
                            "required": list(props.keys())[:1],
                        },
                    },
                })
        return defs

    # ─── 主推理循环（Hermes AIAgent 原版模式）───

    async def handle(self, msg: IncomingMessage,
                     session_id: str = None,
                     context: list = None,
                     emotion: dict = None,
                     **kwargs):
        start = time.time()
        sid = session_id or f"unified:{msg.channel_id}"

        system = f"你是{self.persona.name}（Nodus）。项目: {PROJECT_ROOT}。核心文件: {', '.join(CORE_FILES.keys())}。用中文。"
        if _INJECTED_SOUL:
            system += f"\n{_INJECTED_SOUL[:600]}"
        if _INJECTED_MEMORY:
            system += f"\n{_INJECTED_MEMORY[:600]}"

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": msg.content},
        ]
        tool_defs = self._build_tool_defs()
        response = None

        for turn in range(3):
            self._show_stage("intent" if turn == 0 else "executing")
            try:
                resp = await self.llm.chat_with_tools(messages, tool_defs)
            except Exception as e:
                response = f"出错：{e}"
                break

            tool_calls = resp.get("tool_calls") or []
            if not tool_calls:
                response = resp.get("content", "") or "嗯"
                break

            messages.append({"role": "assistant", "content": None, "tool_calls": tool_calls})
            for tc in tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                args_str = fn.get("arguments", "{}")
                try:
                    args = json.loads(args_str) if isinstance(args_str, str) else args_str
                except Exception:
                    args = {}
                try:
                    r = await asyncio.wait_for(self.executor.execute(name, args), timeout=10.0)
                    out = json.dumps(r, ensure_ascii=False, default=str)[:2000]
                except asyncio.TimeoutError:
                    out = '{"error":"timeout"}'
                except Exception as e:
                    out = json.dumps({"error": str(e)})
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", f"t{turn}"),
                    "content": out,
                })
        else:
            self._show_stage("replying")
            messages.append({"role": "user", "content": "请总结回复用户。"})
            try:
                response = await self.llm.chat(messages, temperature=0.7)
            except Exception:
                response = "处理完成"

        self._clear_stage()
        if response and self.memory:
            self.memory.save_interaction(sid, msg.content, response)

        elapsed = (time.time() - start) * 1000
        logger.info(f"[{sid}] Done in {elapsed:.0f}ms")
        self.bus.emit("response.ready",
                       message_id=msg.id, content=response or "处理完成",
                       session_id=sid, platform=msg.platform,
                       channel_id=msg.channel_id, elapsed_ms=elapsed)
