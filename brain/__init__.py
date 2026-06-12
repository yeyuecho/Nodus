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
    _INJECTED_SOUL, _INJECTED_MEMORY,
)
from config.defaults import (
    ROOT as PROJECT_ROOT,
    CORE_FILES,
    TOOL_DESCRIPTIONS,
    SYSTEM_PROMPT_TEMPLATE,
)

logger = logging.getLogger("qiyue.brain")


# ═══════════════════════════════════════════
# 1. 意图解析器
# ═══════════════════════════════════════════

class IntentParser:
    """LLM 驱动的意图识别 + 参数提取"""

    INTENT_PROMPT = """分析用户输入，输出结构化意图。

用户输入: {user_message}

输出 JSON（只输出 JSON，不要其他文字）:
{{
  "intent": "系统诊断 | 信息查询 | 代码修复 | 浏览器操作 | 文档处理 | 架构设计 | 日常对话 | 文件操作",
  "confidence": 0.0-1.0,
  "parameters": {{}},
  "complexity": "simple | complex",
  "reasoning": "简短判断依据"
}}

注意：
- "怎么样"/"状态"/"运行情况"/"还好吗" → 系统诊断
- "配置"/"硬件"/"内存"/"CPU"/"电脑" → 系统诊断
- "查"/"搜"/"找"/"有没有" → 信息查询
- 纯打招呼/闲聊 → 日常对话"""

    def __init__(self, llm: LLMClient):
        self.llm = llm

    async def parse(self, msg: IncomingMessage, context: list[dict]) -> IntentResult:
        """解析用户意图"""
        # 构建上下文
        context_str = ""
        if context:
            recent = context[-6:]  # 最近 6 条
            context_str = "\n".join(
                f"[{m['role']}]: {m['content'][:200]}" for m in recent
            )

        prompt = self.INTENT_PROMPT.format(user_message=msg.content)
        if context_str:
            prompt += f"\n\n对话上下文:\n{context_str}"

        try:
            # 用 persona 风格的 system prompt
            system = build_system_prompt(role="意图解析")
            result = await self.llm.chat_json([
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ], temperature=0.1)

            intent = result.get("intent", "日常对话")
            confidence = float(result.get("confidence", 0.8))
            params = result.get("parameters", {})

            return IntentResult(
                intent=intent,
                confidence=confidence,
                parameters=params,
                raw_input=msg.content,
            )
        except Exception as e:
            logger.error(f"Intent parsing failed: {e}")
            return IntentResult(
                intent="日常对话",
                confidence=0.5,
                raw_input=msg.content,
            )


# ═══════════════════════════════════════════
# 2. 任务规划器
# ═══════════════════════════════════════════

class TaskPlanner:
    """任务拆解 + 编排 + 路由决策"""

    PLANNING_PROMPT = """基于意图，制定执行计划。你必须判断是否需要调用工具。

意图: {intent}
参数: {params}
置信度: {confidence}
可用技能: {skills}
可用工具: {tools}

## 判断规则（严格遵守，违反将被惩罚）

### 必须用 self_execute（调用工具）的情况：
- 系统诊断/系统状态/运行情况 → tool=shell_exec, command=systeminfo 或 tasklist
- 查看配置/硬件/CPU/内存/磁盘 → tool=shell_exec, command=systeminfo
- 读文件/列出目录/查看内容 → tool=file_read/file_search/file_find
- 写文件/改代码 → tool=file_write/file_patch
- 执行命令/脚本 → tool=shell_exec
- 任何"读/看/检查/列出"本地文件或系统 → 必须用工具，禁止编造

### 必须用 dispatch_executor 的情况：
- 网页搜索/查资料 → tool=web_search
- 打开网页/截图 → tool=browser_navigate

### 可以用 llm_direct_reply 的情况（仅限以下）：
- 纯聊天/打招呼/情感交流
- 解释概念/回答问题（完全不需要访问本地系统）
- 用户只是闲聊，没让你做任何事

## 输出 JSON（严格遵守格式）:
{{"action": "self_execute", "skill_match": null, "reasoning": "需要查看系统配置", "sub_tasks": [{{"id": "sub_1", "type": "shell", "tool": "shell_exec", "params": {{"command": "systeminfo"}}, "depends_on": []}}]}}"""

    def __init__(self, llm: LLMClient):
        self.llm = llm

    async def plan(self, intent: IntentResult, context: dict,
                   available_skills: list[str] = None,
                   available_tools: dict = None) -> ExecutionPlan:
        """制定执行计划"""
        task_id = f"task_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        # 低置信度 → 直接回复，不冒险规划
        if intent.confidence < 0.5:
            return ExecutionPlan(
                task_id=task_id,
                intent=intent,
                action="llm_direct_reply",
            )

        # 格式化工具列表为描述形式
        if available_tools:
            tools_str = "\n".join(f"  - {name}: {desc}" for name, desc in available_tools.items())
        else:
            tools_str = "无"

        prompt = self.PLANNING_PROMPT.format(
            intent=intent.intent,
            params=json.dumps(intent.parameters, ensure_ascii=False),
            confidence=intent.confidence,
            skills=", ".join(available_skills or ["无"]),
            tools=tools_str,
        )

        try:
            system = build_system_prompt(role="任务规划")
            result = await self.llm.chat_json([
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ], temperature=0.1)

            action = result.get("action", "llm_direct_reply")
            skill_match = result.get("skill_match")
            sub_tasks_raw = result.get("sub_tasks", [])

            sub_tasks = []
            for st in sub_tasks_raw:
                try:
                    task_type = TaskType(st.get("type", "llm"))
                except ValueError:
                    task_type = TaskType.LLM

                sub_tasks.append(SubTask(
                    id=st.get("id", f"sub_{len(sub_tasks)}"),
                    type=task_type,
                    tool=st.get("tool", ""),
                    params=st.get("params", {}),
                    depends_on=st.get("depends_on", []),
                ))

            return ExecutionPlan(
                task_id=task_id,
                intent=intent,
                sub_tasks=sub_tasks,
                action=action,
                skill_match=skill_match,
            )
        except Exception as e:
            logger.error(f"Task planning failed: {e}")
            return ExecutionPlan(
                task_id=task_id,
                intent=intent,
                action="llm_direct_reply",
            )


# ═══════════════════════════════════════════
# 3. 记忆管理器
# ═══════════════════════════════════════════

class MemoryManager:
    """记忆管理 — 搜索 + 上下文 + 事实提取"""

    SEARCH_PROMPT = """根据当前意图，判断是否需要搜索历史记忆。

当前意图: {intent}
参数: {params}

如果需要搜索，返回关键词列表；如果不需要，返回空列表。

输出 JSON:
{{"need_search": true/false, "keywords": ["关键词1", "关键词2"]}}"""

    def __init__(self, sessions: SessionStore, llm: LLMClient):
        self.sessions = sessions
        self.llm = llm
        self.sessions.init_db()

    async def search_relevant(self, intent: IntentResult) -> list[dict]:
        """根据意图搜索相关历史记忆"""
        try:
            resp = await self.llm.chat_json([
                {"role": "system", "content": "判断是否需要搜索记忆。只输出 JSON。"},
                {"role": "user", "content": self.SEARCH_PROMPT.format(
                    intent=intent.intent,
                    params=json.dumps(intent.parameters, ensure_ascii=False),
                )},
            ], temperature=0.1)

            if resp.get("need_search") and resp.get("keywords"):
                query = " OR ".join(resp["keywords"])
                return self.sessions.search(query, limit=5)
        except Exception as e:
            logger.error(f"Memory search failed: {e}")

        return []

    def get_context(self, session_id: str, limit: int = 30) -> list[dict]:
        """获取会话上下文"""
        return self.sessions.get_context(session_id, limit)

    def save_interaction(self, session_id: str, user_msg: str, assistant_msg: str):
        """保存一轮对话"""
        self.sessions.append_exchange(session_id, user_msg, assistant_msg)

    def extract_facts(self, content: str) -> list[str]:
        """从内容中提取关键事实（简化版：按句号分割取前3句）"""
        sentences = [s.strip() for s in content.split("。") if s.strip()]
        return sentences[:3]


# ═══════════════════════════════════════════
# 4. 任务路由器
# ═══════════════════════════════════════════

class TaskRouter:
    """任务路由 — 判断谁来执行"""

    # 思维层自己执行的类型
    SELF_EXECUTE = {TaskType.CODE, TaskType.SHELL, TaskType.FILE, TaskType.LLM}
    # 派发执行层的类型
    DISPATCH = {TaskType.BROWSER, TaskType.WEB}

    def route(self, plan: ExecutionPlan) -> dict:
        """拆分任务: {self: [SubTask], dispatch: [SubTask]}"""
        result = {"self": [], "dispatch": []}

        for st in plan.sub_tasks:
            if st.type in self.DISPATCH:
                result["dispatch"].append(st)
            else:
                result["self"].append(st)

        return result


# ═══════════════════════════════════════════
# 5. 自我进化引擎
# ═══════════════════════════════════════════

class SelfSkillEngine:
    """自动技能生成引擎 + 用户偏好学习"""

    SKILL_PROMPT = """基于以下执行记录，生成一个技能骨架。

任务类型: {intent}
参数: {params}
执行计划: {plan}
执行结果: {result}
用户偏好: {preferences}

生成 SKILL.md 内容（YAML frontmatter + Markdown body）:

---
name: {自动生成技能名}
version: 0.1.0
source: self_generated
triggers:
  - {触发关键词}
tools: {所需工具列表}
user_preferences:
  - {从交互中提取的用户偏好}
---

## When to Use
{使用场景}

## Workflow
{步骤}

## Output Format
{输出格式}

## User Preferences Learned
{记录的偏好}

只输出技能内容，不要解释。"""

    def __init__(self, llm: LLMClient, skill_loader=None):
        self.llm = llm
        self.skill_loader = skill_loader
        self.preferences_path = Path("data/memory/user_preferences.json")

    def load_preferences(self) -> dict:
        """加载已有用户偏好"""
        if self.preferences_path.exists():
            try:
                return json.loads(self.preferences_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"preferences": [], "patterns": []}

    def save_preferences(self, prefs: dict):
        """保存用户偏好"""
        self.preferences_path.parent.mkdir(parents=True, exist_ok=True)
        self.preferences_path.write_text(
            json.dumps(prefs, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _detect_preferences(self, msg_content: str, plan: ExecutionPlan,
                            result_output: str) -> list[str]:
        """从一轮交互中检测用户偏好（轻量规则）"""
        prefs = []
        lowered = msg_content.lower()

        # 格式偏好
        if any(kw in lowered for kw in ["表格", "列表", "列出来"]):
            prefs.append("偏好列表/表格格式输出")
        if any(kw in lowered for kw in ["简单", "一句话", "简短"]):
            prefs.append("偏好简洁回复")
        if any(kw in lowered for kw in ["详细", "展开", "多说"]):
            prefs.append("偏好详细解释")
        if any(kw in lowered for kw in ["图表", "图", "可视化"]):
            prefs.append("偏好数据可视化/图表")

        # 行为模式
        if plan.action == "self_execute" and plan.sub_tasks:
            tools_used = [st.tool for st in plan.sub_tasks]
            prefs.append(f"常用工具: {', '.join(tools_used)}")

        # 从执行结果推断
        if result_output and len(result_output) > 500:
            prefs.append("接受较长输出")

        return prefs

    async def try_generate(
        self, intent: IntentResult, plan: ExecutionPlan,
        result: TaskResult = None,
        msg_content: str = "",
    ) -> Optional[str]:
        """尝试生成新技能（含偏好学习）"""
        # 条件：无匹配技能 + 置信度 > 0.7 + 非 ad-hoc
        if intent.confidence < 0.7:
            return None
        if plan.skill_match:
            return None
        if plan.action == "llm_direct_reply":
            return None

        # 检测并累积用户偏好
        result_output = result.output if result else ""
        new_prefs = self._detect_preferences(msg_content, plan, result_output)

        if new_prefs:
            existing = self.load_preferences()
            for pref in new_prefs:
                if pref not in existing["preferences"]:
                    existing["preferences"].append(pref)
            self.save_preferences(existing)
            logger.info(f"[SkillEngine] Learned {len(new_prefs)} new preference(s)")

        try:
            prefs = self.load_preferences()
            prefs_str = "\n".join(f"- {p}" for p in prefs.get("preferences", [])[:10])

            content = await self.llm.chat([
                {"role": "system", "content": "你是技能生成器，输出 SKILL.md 格式的技能定义。记录用户偏好。"},
                {"role": "user", "content": self.SKILL_PROMPT.format(
                    intent=intent.intent,
                    params=json.dumps(intent.parameters, ensure_ascii=False),
                    plan=json.dumps({
                        "action": plan.action,
                        "sub_tasks": [{"type": st.type, "tool": st.tool} for st in plan.sub_tasks],
                    }, ensure_ascii=False),
                    result=result_output,
                    preferences=prefs_str or "暂无",
                )},
            ], temperature=0.3)

            return content
        except Exception as e:
            logger.error(f"Skill generation failed: {e}")
            return None


# ═══════════════════════════════════════════
# Brain 主类
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

    # ═══════════════════════════════════════════
    # Hermes 风格推理循环
    # ═══════════════════════════════════════════

    def _build_tool_defs(self) -> list[dict]:
        """将注册的工具转换为 OpenAI tool calling 格式"""
        tool_names = {
            "shell_exec": ("执行Shell命令", {"command": {"type": "string", "description": "要执行的命令，如 systeminfo / tasklist / dir"}}),
            "file_read": ("读取文件", {"path": {"type": "string", "description": "文件路径"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}}),
            "file_write": ("写入文件", {"path": {"type": "string"}, "content": {"type": "string"}}),
            "file_patch": ("修改文件内容", {"path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}}),
            "file_search": ("搜索文件内容", {"pattern": {"type": "string"}, "path": {"type": "string"}, "limit": {"type": "integer"}}),
            "file_find": ("查找文件", {"pattern": {"type": "string"}, "path": {"type": "string"}}),
            "web_search": ("网络搜索", {"query": {"type": "string"}, "max_results": {"type": "integer"}}),
            "web_fetch": ("获取网页", {"url": {"type": "string"}}),
        }
        defs = []
        # 只暴露当前已注册的工具
        for name, (desc, props) in tool_names.items():
            if name in self._available_tools:
                defs.append({
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": desc,
                        "parameters": {
                            "type": "object",
                            "properties": props,
                            "required": list(props.keys())[:1],  # 第一个参数必填
                        },
                    },
                })
        return defs

    def _parse_text_tool_calls(self, content: str) -> list:
        """从 LLM 文本输出中解析 tool call"""
        import re
        calls = []
        # <invoke name="xxx">...<parameter name="yyy">zzz</parameter>...</invoke>
        invoke_re = re.compile(
            r'<invoke\s+name\s*=\s*"(\w+)"[^>]*>(.*?)</invoke>',
            re.DOTALL,
        )
        param_re = re.compile(
            r'<parameter\s+name\s*=\s*"(\w+)"[^>]*>(.*?)</parameter>',
            re.DOTALL,
        )
        for m in invoke_re.finditer(content):
            name = m.group(1)
            params_str = m.group(2)
            args = {}
            for pm in param_re.finditer(params_str):
                args[pm.group(1)] = pm.group(2).strip()
            if args:
                calls.append({"function": {"name": name, "arguments": json.dumps(args)}})
        return calls

    async def _reason_loop(self, msg: IncomingMessage,
                           context: list[dict],
                           emotion_tag: str,
                           max_turns: int = 2) -> str:
        """
        工具调用 → 汇总回复（最多2轮）
        """
        try:
            # 用预构建模板生成 system prompt（含路径、工具、核心文件）
            system = SYSTEM_PROMPT_TEMPLATE.format(
                name=self.persona.name,
                root=str(PROJECT_ROOT),
                core_files=", ".join(CORE_FILES.keys()),
                tools="\n".join(f"  {k}: {v}" for k, v in TOOL_DESCRIPTIONS.items()),
                soul=_INJECTED_SOUL[:300] or "",
                memory=_INJECTED_MEMORY[:300] or "",
            )

            context_str = ""
            if context:
                recent = context[-4:]
                context_str = "最近:\n" + "\n".join(
                    f"[{m['role']}]: {m['content'][:150]}" for m in recent
                )

            user_content = msg.content
            if context_str:
                user_content = f"{context_str}\n\n用户: {msg.content}"

            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ]

            tool_defs = self._build_tool_defs()

            for turn in range(max_turns):
                self._show_stage("planning" if turn == 0 else "executing")

                try:
                    resp = await self.llm.chat_with_tools(
                        messages, tool_defs,
                        temperature=0.5,
                    )
                except Exception as e:
                    logger.error(f"[_reason_loop] LLM error: {e}")
                    return f"出错了：{e}"

                tool_calls = resp.get("tool_calls", [])

                # 如果原生 tool_calls 为空，尝试从文本中解析
                if not tool_calls:
                    content = resp.get("content", "")
                    if "<invoke" in content or "<function_call" in content or "<tool_call" in content:
                        tool_calls = self._parse_text_tool_calls(content)
                        if tool_calls:
                            logger.info(f"[_reason_loop] Parsed {len(tool_calls)} text-format tool calls")
                        else:
                            # 解析失败，去掉工具调用语法只返回人话部分
                            import re
                            clean = re.sub(r'<invoke[^>]*>.*?</invoke>', '', content, flags=re.DOTALL)
                            clean = re.sub(r'<function_calls>.*?</function_calls>', '', clean, flags=re.DOTALL)
                            clean = re.sub(r'<tool_calls>.*?</tool_calls>', '', clean, flags=re.DOTALL)
                            return clean.strip() or "嗯？"

                if not tool_calls:
                    return resp.get("content", "") or "嗯？"

                # 只执行第一个工具调用
                tc = tool_calls[0]
                name = tc.get("function", {}).get("name", tc.get("name", "unknown"))
                args = tc.get("function", {}).get("arguments", tc.get("params", tc.get("args", {})))
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}

                try:
                    result = await asyncio.wait_for(
                        self.executor.execute(name, args), timeout=10.0,
                    )
                    output = json.dumps(result, ensure_ascii=False, default=str)[:2000]
                except asyncio.TimeoutError:
                    output = json.dumps({"error": "timeout"})
                except Exception as e:
                    output = json.dumps({"error": str(e)})

                # 追加结果并强制 LLM 总结
                messages.append({"role": "assistant", "content": "", "tool_calls": [tc]})
                messages.append({"role": "tool", "tool_call_id": tc.get("id", "t0"), "content": output})
                messages.append({"role": "user", "content": "请基于以上结果，用自然中文回复用户。不要继续调用工具。"})

                self._show_stage("replying")
                try:
                    final = await self.llm.chat(messages, temperature=0.7)
                    return final or "处理完成~"
                except Exception:
                    return "处理超时，请重试~"

            return "处理完成~"

        except Exception as e:
            logger.error(f"[_reason_loop] Fatal: {e}")
            return f"出错了：{e}"


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
        """Hermes-style: tool calling + execute + iterate + respond."""
        start = time.time()
        sid = session_id or f"unified:{msg.channel_id}"

        system = f"你是{self.persona.name}（Nodus）。项目路径: {PROJECT_ROOT}。用中文。"
        messages = [{"role": "system", "content": system}, {"role": "user", "content": msg.content}]
        tool_defs = self._build_tool_defs()
        response = None

        for turn in range(5):
            self._show_stage("planning" if turn == 0 else "executing")
            try:
                resp = await self.llm.chat_with_tools(messages, tool_defs)
            except Exception as e:
                response = f"出错了：{e}"; break

            tool_calls = resp.get("tool_calls") or []
            if not tool_calls:
                response = resp.get("content", "") or "嗯"; break

            messages.append({"role": "assistant", "content": resp.get("content") or None, "tool_calls": tool_calls})
            for tc in tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                args_str = fn.get("arguments", "{}")
                try: args = json.loads(args_str) if isinstance(args_str, str) else args_str
                except: args = {}
                try:
                    r = await asyncio.wait_for(self.executor.execute(name, args), timeout=10.0)
                    out = json.dumps(r, ensure_ascii=False, default=str)[:3000]
                except asyncio.TimeoutError: out = '{"error":"timeout"}'
                except Exception as e: out = json.dumps({"error": str(e)})
                messages.append({"role": "tool", "tool_call_id": tc.get("id", f"t{turn}"), "content": out})
        else:
            self._show_stage("replying")
            messages.append({"role": "user", "content": "请总结以上信息回复用户。"})
            try: response = await self.llm.chat(messages, temperature=0.7)
            except: response = "处理完成"

        self._clear_stage()
        if response: self.memory.save_interaction(sid, msg.content, response)
        elapsed = (time.time() - start) * 1000
        logger.info(f"[{sid}] Done in {elapsed:.0f}ms")
        self.bus.emit("response.ready", message_id=msg.id, content=response or "处理完成", session_id=sid, platform=msg.platform, channel_id=msg.channel_id, elapsed_ms=elapsed)
        start = time.time()
        sid = session_id or f"unified:{msg.channel_id}"

        self._show_stage("planning")

        # 把所有信息注入 system prompt，LLM 不需要调工具
        system = SYSTEM_PROMPT_TEMPLATE.format(
            name=self.persona.name,
            root=str(PROJECT_ROOT),
            core_files=", ".join(CORE_FILES.keys()),
            tools="",
            soul=_INJECTED_SOUL[:500] or "",
            memory=_INJECTED_MEMORY[:500] or "",
        )
        system += "\n\n直接回复用户，用自然中文。不要调用工具，不要输出 XML。"

        try:
            response = await self.llm.chat([
                {"role": "system", "content": system},
                {"role": "user", "content": msg.content},
            ], temperature=0.7)
        except Exception as e:
            response = f"出错了：{e}"

        # 过滤可能的 XML 标签
        import re
        response = re.sub(r'<invoke[^>]*>.*?</invoke>', '', response, flags=re.DOTALL)
        response = re.sub(r'<function_calls>.*?</function_calls>', '', response, flags=re.DOTALL)
        response = re.sub(r'<parameter[^>]*>.*?</parameter>', '', response, flags=re.DOTALL)
        response = response.strip() or "嗯？"

        self._clear_stage()

        if response:
            self.memory.save_interaction(sid, msg.content, response)

        elapsed = (time.time() - start) * 1000
        logger.info(f"[{sid}] Done in {elapsed:.0f}ms")

        self.bus.emit("response.ready",
                       message_id=msg.id, content=response,
                       session_id=sid, platform=msg.platform,
                       channel_id=msg.channel_id, elapsed_ms=elapsed)

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
