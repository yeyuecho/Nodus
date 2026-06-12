"""
思维层 — Hermes 五大能力完整实现

全量推理 · 任务规划 · 记忆管理 · 调度决策 · 自我进化
"""

import asyncio
import json
import logging
import sys
import time
from datetime import datetime
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
)

logger = logging.getLogger("qiyue.brain")


# ═══════════════════════════════════════════
# 1. 意图解析器
# ═══════════════════════════════════════════

class Brain:
    """思维中枢 — 组装五大能力，处理每条消息"""

    TRANSLATE_PROMPT = """{persona_style}

现在把以下工具执行结果翻译成用户爱听的人话。

用户原始问题: {user_message}
用户情绪: {emotion}
执行结果: {raw_results}

翻译规则:
1. 不要直接 dump 原始数据，提取用户关心的信息
2. 用自然语言表达，像朋友聊天
3. 可以适当闲聊包装（但不啰嗦，不超过必要的30%）
4. 如果用户情绪是 angry/frustrated，先共情安抚
5. 如果有下一步建议，自然地提出来
6. 用第一人称「我」"""

    # 思考进度动画帧
    SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    STAGES = {
        "intent": "理解中",
        "memory": "回忆中",
        "planning": "思考中",
        "executing": "处理中",
        "replying": "回复中",
    }

    def _show_stage(self, stage: str):
        """输出思考阶段提示（\r 覆盖刷新）"""
        label = self.STAGES.get(stage, stage)
        sys.stdout.write(f"\r  {label}...  ")
        sys.stdout.flush()

    def _clear_stage(self):
        """清除思考阶段提示"""
        sys.stdout.write("\r" + " " * 30 + "\r")
        sys.stdout.flush()


    def __init__(self, llm: LLMClient, bus: EventBus,
                 sessions: SessionStore = None,
                 executor=None,
                 memory=None,
                 persona: Persona = None):
        self.llm = llm
        self.bus = bus
        self.sessions = sessions or SessionStore()
        self.executor = executor
        self.memory = memory  # MemoryStore (持久记忆)
        self.persona = persona or DEFAULT_PERSONA

        # 五大能力
        self.intent_parser = IntentParser(llm)
        self.task_planner = TaskPlanner(llm)
        self.memory = MemoryManager(self.sessions, llm)
        self.router = TaskRouter()
        self.skill_engine = SelfSkillEngine(llm)

        # 可用技能列表 + 工具列表
        self._available_skills: list[str] = []
        self._available_tools: dict = {}  # name -> description

    def register_skill_loader(self, skill_loader):
        """注入技能加载器（来自执行层）"""
        self.skill_engine.skill_loader = skill_loader
        if skill_loader:
            self._available_skills = [s.slug for s in skill_loader.list_all()]

    def register_tools(self, tools: dict):
        """注册可用工具（name -> description）"""
        self._available_tools = tools

    async def handle(self, msg: IncomingMessage,
                     session_id: str = None,
                     context: list[dict] = None,
                     emotion: dict = None,
                     **kwargs):
        """
        完整推理流水线（含人设 + 情绪感知）:
        意图 → 搜索记忆 → 规划 → 路由 → 执行 → 翻译官 → 保存
        """
        start = time.time()
        sid = session_id or f"unified:{msg.channel_id}"

        # 提取情绪标签
        emotion_tag = (emotion or {}).get("emotion", "neutral")
        if emotion_tag != "neutral":
            logger.info(f"[{sid}] Emotion: {emotion_tag}")

        # 1. 意图解析
        self._show_stage("intent")
        intent = await self.intent_parser.parse(msg, context or [])
        logger.info(f"[{sid}] Intent: {intent.intent} ({intent.confidence:.2f})")

        # 2. 搜索相关历史记忆
        self._show_stage("memory")
        memories = await self.memory.search_relevant(intent)
        if memories:
            logger.info(f"[{sid}] Found {len(memories)} relevant memories")

        # 3. 任务规划
        self._show_stage("planning")
        plan = await self.task_planner.plan(
            intent, {},
            available_skills=self._available_skills,
            available_tools=self._available_tools,
        )
        logger.info(f"[{sid}] Plan: {plan.action}, {len(plan.sub_tasks)} sub-tasks")

        # 4. 按 action 处理（传入情绪标签）
        if plan.action == "llm_direct_reply":
            self._show_stage("replying")
            response = await self._generate_reply(msg, context, memories, emotion_tag)

        elif plan.action == "dispatch_executor":
            self._show_stage("executing")
            self.bus.emit("task.dispatched", plan=plan, session_id=sid)
            response = "收到，正在处理，稍等一下哦~"
            asyncio.create_task(self._dispatch_and_reply(plan, msg, sid, context, emotion_tag))

        else:  # self_execute
            self._show_stage("executing")
            response = await self._self_execute(plan, msg, context, emotion_tag)

        self._clear_stage()

        # 5. 保存会话
        if response:
            self.memory.save_interaction(sid, msg.content, response)

        # 6. 尝试生成技能（含偏好学习）
        skill_content = await self.skill_engine.try_generate(
            intent, plan, msg_content=msg.content,
        )
        if skill_content and self.skill_engine.skill_loader:
            logger.info(f"[{sid}] Generated new skill candidate")
            # 保存技能到文件
            slug = intent.intent.replace(" ", "-").lower()
            skill_dir = Path("skills") / slug
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text(skill_content, encoding="utf-8")
            self.skill_engine.skill_loader.load_all()
            self._available_skills = [s.slug for s in self.skill_engine.skill_loader.list_all()]

        # 7. 推送回复
        elapsed = (time.time() - start) * 1000
        logger.info(f"[{sid}] Done in {elapsed:.0f}ms")

        self.bus.emit("response.ready",
                       message_id=msg.id,
                       content=response or "处理完成",
                       session_id=sid,
                       platform=msg.platform,
                       channel_id=msg.channel_id,
                       elapsed_ms=elapsed)

    async def _generate_reply(self, msg: IncomingMessage,
                              context: list[dict],
                              memories: list[dict],
                              emotion_tag: str = "neutral") -> str:
        """直接 LLM 生成回复（含人设 + 情绪策略）"""
        context_str = ""
        if context:
            recent = context[-10:]
            context_str = "最近对话:\n" + "\n".join(
                f"[{m['role']}]: {m['content'][:300]}" for m in recent
            )

        memory_str = ""
        if memories:
            memory_str = "相关历史:\n" + "\n".join(
                f"- {m['content'][:200]}" for m in memories[:3]
            )

        full_context = f"{context_str}\n{memory_str}".strip() or "无上下文"

        # 获取情绪应对策略
        emotion_guide = get_emotion_strategy(self.persona, emotion_tag)

        # 用 persona 构建 system prompt
        system = build_system_prompt(self.persona, role="回复生成")
        system += f"\n\n用户当前情绪: {emotion_tag}\n应对策略: {emotion_guide}"

        return await self.llm.chat([
            {"role": "system", "content": system},
            {"role": "user", "content": f"""上下文: {full_context}

用户说: {msg.content}

请用自然、有温度的语气回复。"""},
        ], temperature=0.7)

    async def _self_execute(self, plan: ExecutionPlan, msg: IncomingMessage,
                            context: list[dict],
                            emotion_tag: str = "neutral") -> str:
        """思维层自己执行任务 → 翻译官 → 人性化回复"""
        if not self.executor or not plan.sub_tasks:
            return await self._generate_reply(msg, context, [], emotion_tag)

        # 构建依赖图 → 并行执行无依赖的子任务
        results = {}
        remaining = list(plan.sub_tasks)

        while remaining:
            ready = [
                st for st in remaining
                if all(dep in results for dep in st.depends_on)
            ]

            if not ready:
                break

            async def run_one(st):
                try:
                    r = await self.executor.execute(st.tool, st.params)
                    return st.id, {"success": r.get("success", True), "output": r}
                except Exception as e:
                    return st.id, {"success": False, "error": str(e)}

            batch = await asyncio.gather(*[run_one(st) for st in ready])
            for task_id, result in batch:
                results[task_id] = result
                logger.info(f"[Parallel] {task_id}: {'OK' if result['success'] else 'FAIL'}")

            remaining = [st for st in remaining if st.id not in results]

        # 按顺序汇总原始结果
        ordered = [results.get(st.id, {"error": "not executed"}) for st in plan.sub_tasks]

        # === 翻译官：将原始结果包装成有温度的人话 ===
        persona_style = build_system_prompt(self.persona, role="结果翻译")
        emotion_guide = get_emotion_strategy(self.persona, emotion_tag)

        translate_prompt = self.TRANSLATE_PROMPT.format(
            persona_style=persona_style,
            user_message=msg.content,
            emotion=f"{emotion_tag}（应对策略: {emotion_guide}）",
            raw_results=json.dumps(ordered, ensure_ascii=False, default=str),
        )

        system = build_system_prompt(self.persona, role="回复生成")
        system += f"\n\n用户当前情绪: {emotion_tag}\n应对策略: {emotion_guide}"

        return await self.llm.chat([
            {"role": "system", "content": system},
            {"role": "user", "content": translate_prompt},
        ], temperature=0.6)

    async def _dispatch_and_reply(self, plan: ExecutionPlan,
                                  msg: IncomingMessage,
                                  session_id: str,
                                  context: list[dict],
                                  emotion_tag: str = "neutral"):
        """异步派发执行层并回复"""
        try:
            response = await self._generate_reply(msg, context, [], emotion_tag)
            self.bus.emit("response.ready",
                           message_id=msg.id,
                           content=response,
                           session_id=session_id,
                           platform=msg.platform,
                           channel_id=msg.channel_id)
        except Exception as e:
            logger.error(f"Dispatch failed: {e}")
            self.bus.emit("response.ready",
                           message_id=msg.id,
                           content=f"抱歉，处理你的请求时遇到了问题: {e}",
                           session_id=session_id,
                           platform=msg.platform,
                           channel_id=msg.channel_id)
