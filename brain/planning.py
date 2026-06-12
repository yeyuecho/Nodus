"""
增强规划器 — Few-shot 示例 + 任务拆解模板 + 边界条件处理
来源: Hermes system prompt patterns

功能:
- 意图分类（规则 + LLM 混合）
- Few-shot 示例库（中英文混合）
- 任务拆解模板（代码/浏览器/研究/文件）
- 边界条件处理（空输入/超长/敏感内容/歧义）
- 执行计划生成
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("qiyue.planning")


# ═══════════════════════════════════════════
# 意图分类器（规则引擎）
# ═══════════════════════════════════════════

class IntentClassifier:
    """基于规则的快速意图分类"""

    RULES = [
        # (intent, keywords, priority)
        ("code_fix", ["修复", "bug", "报错", "error", "异常", "fix", "debug", "补丁"], 10),
        ("code_write", ["写一个", "实现", "创建", "开发", "编写", "重构", "建立", "新增"], 10),
        ("code_review", ["审查", "review", "检查代码", "改进", "优化代码"], 8),
        ("shell_exec", ["运行", "执行", "启动", "重启", "停止", "安装", "部署", "编译"], 10),
        ("file_read", ["读取", "查看", "显示", "打开", "内容", "文件"], 9),
        ("file_write", ["写入", "保存", "创建文件", "新建", "修改文件"], 9),
        ("file_search", ["搜索", "查找", "寻找", "grep", "find", "在哪里"], 8),
        ("web_search", ["查一下", "百度", "谷歌", "搜索", "最新", "新闻", "查询"], 8),
        ("web_fetch", ["抓取", "打开网页", "访问", "链接", "网址", "url"], 7),
        ("browser", ["浏览器", "截图", "点击", "登录", "填写表单", "自动化"], 7),
        ("config", ["配置", "设置", "环境变量", "config", "参数", "选项"], 6),
        ("data_query", ["查询", "统计", "分析", "报表", "数据"], 6),
        ("chat_general", ["你好", "谢谢", "帮助", "怎么样", "如何", "是什么", "为什么"], 5),
        ("schedule", ["定时", "计划", "安排", "cron", "日程", "提醒"], 4),
        ("summarize", ["总结", "摘要", "概括", "归纳", "汇总"], 5),
    ]

    @classmethod
    def classify(cls, text: str) -> List[tuple]:
        """
        分类意图，返回 [(intent, score), ...]

        按匹配关键词数量和优先级评分。
        """
        text_lower = text.lower()
        scores = []

        for intent, keywords, base_priority in cls.RULES:
            score = 0
            for kw in keywords:
                if kw.lower() in text_lower:
                    score += base_priority
            if score > 0:
                scores.append((intent, score))

        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:5]

    @classmethod
    def primary_intent(cls, text: str) -> str:
        """获取主要意图"""
        results = cls.classify(text)
        return results[0][0] if results else "chat_general"


# ═══════════════════════════════════════════
# Few-shot 示例库
# ═══════════════════════════════════════════

FEWSHOT_EXAMPLES = {
    "code_fix": [
        {
            "user": "这段代码报错 TypeError: 'NoneType' object is not iterable",
            "plan": {
                "intent": "code_fix",
                "action": "code_diagnose_and_fix",
                "steps": [
                    "1. 读取报错文件和行号",
                    "2. 分析 NoneType 来源（函数返回值/变量未初始化）",
                    "3. 添加 None 检查或默认值",
                    "4. 运行测试验证修复",
                ],
                "tools_needed": ["file_read", "file_patch", "shell_exec"],
            },
        },
    ],
    "code_write": [
        {
            "user": "写一个 Python 脚本，读取 CSV 文件并画图表",
            "plan": {
                "intent": "code_write",
                "action": "create_script",
                "steps": [
                    "1. 确认 CSV 文件路径和格式",
                    "2. 使用 pandas 读取数据",
                    "3. 使用 matplotlib 绘图",
                    "4. 保存图表为 PNG",
                    "5. 添加错误处理",
                ],
                "tools_needed": ["file_write", "shell_exec"],
                "dependencies": ["pandas", "matplotlib"],
            },
        },
    ],
    "web_search": [
        {
            "user": "查一下最新的 Python 3.13 更新内容",
            "plan": {
                "intent": "web_search",
                "action": "search_and_summarize",
                "steps": [
                    "1. Web 搜索 'Python 3.13 release notes'",
                    "2. 抓取官方文档页面",
                    "3. 提取关键更新点",
                    "4. 中文总结回答",
                ],
                "tools_needed": ["web_search", "web_fetch"],
            },
        },
    ],
    "shell_exec": [
        {
            "user": "重启 nginx 服务",
            "plan": {
                "intent": "shell_exec",
                "action": "system_command",
                "steps": [
                    "1. 确认系统类型 (systemctl / service)",
                    "2. 执行重启命令",
                    "3. 检查服务状态",
                    "4. 如有错误则排查日志",
                ],
                "tools_needed": ["shell_exec"],
                "safety": "需要 root 权限，确认用户授权",
            },
        },
    ],
    "file_search": [
        {
            "user": "找到所有用到 deprecated 函数的文件",
            "plan": {
                "intent": "file_search",
                "action": "search_codebase",
                "steps": [
                    "1. 确定搜索目录",
                    "2. 用正则搜索 deprecated 关键词",
                    "3. 按文件分组结果",
                    "4. 生成修改建议清单",
                ],
                "tools_needed": ["file_search", "file_read"],
            },
        },
    ],
}


# ═══════════════════════════════════════════
# 任务拆解模板
# ═══════════════════════════════════════════

class TaskDecomposer:
    """任务拆解器 — 根据意图类型生成子任务计划"""

    @staticmethod
    def decompose(intent: str, user_message: str, context: dict = None) -> dict:
        """
        将用户消息拆解为执行计划

        返回: {
            task_id, intent, action, sub_tasks: [...], skill_match, estimated_duration
        }
        """
        context = context or {}

        plan = {
            "task_id": f"task_{int(time.time())}",
            "intent": intent,
            "original_message": user_message[:500],
        }

        if intent in ("code_fix", "code_review"):
            plan.update(TaskDecomposer._code_plan(user_message, context))
        elif intent == "code_write":
            plan.update(TaskDecomposer._code_write_plan(user_message, context))
        elif intent in ("web_search", "web_fetch"):
            plan.update(TaskDecomposer._web_plan(user_message, context))
        elif intent == "shell_exec":
            plan.update(TaskDecomposer._shell_plan(user_message, context))
        elif intent in ("file_read", "file_write", "file_search"):
            plan.update(TaskDecomposer._file_plan(user_message, context, intent))
        elif intent == "browser":
            plan.update(TaskDecomposer._browser_plan(user_message, context))
        elif intent == "config":
            plan.update(TaskDecomposer._config_plan(user_message, context))
        else:
            plan.update(TaskDecomposer._general_plan(user_message, context))

        return plan

    @staticmethod
    def _code_plan(msg: str, ctx: dict) -> dict:
        return {
            "action": "code_diagnose_and_fix",
            "sub_tasks": [
                {"type": "file", "tool": "file_read", "description": "读取报错文件"},
                {"type": "file", "tool": "file_search", "description": "搜索相关代码"},
                {"type": "code", "tool": "llm_analyze", "description": "LLM 分析根因"},
                {"type": "file", "tool": "file_patch", "description": "应用修复 patch"},
                {"type": "shell", "tool": "shell_exec", "description": "运行测试验证"},
            ],
            "skill_match": "code-diagnostic",
            "estimated_duration": "1-5 min",
        }

    @staticmethod
    def _code_write_plan(msg: str, ctx: dict) -> dict:
        return {
            "action": "create_code",
            "sub_tasks": [
                {"type": "code", "tool": "llm_generate", "description": "LLM 生成代码"},
                {"type": "file", "tool": "file_write", "description": "写入文件"},
                {"type": "shell", "tool": "shell_exec", "description": "安装依赖"},
                {"type": "shell", "tool": "shell_exec", "description": "运行测试"},
                {"type": "code", "tool": "file_patch", "description": "修复发现的问题"},
            ],
            "skill_match": "code-generation",
            "estimated_duration": "2-10 min",
        }

    @staticmethod
    def _web_plan(msg: str, ctx: dict) -> dict:
        return {
            "action": "search_and_summarize",
            "sub_tasks": [
                {"type": "web", "tool": "web_search", "description": "多引擎搜索"},
                {"type": "web", "tool": "web_fetch", "description": "抓取 Top 3 页面"},
                {"type": "llm", "tool": "llm_summarize", "description": "LLM 总结提取"},
                {"type": "llm", "tool": "llm_reply", "description": "组织回复"},
            ],
            "skill_match": "web-research",
            "estimated_duration": "30s-2 min",
        }

    @staticmethod
    def _shell_plan(msg: str, ctx: dict) -> dict:
        return {
            "action": "execute_command",
            "sub_tasks": [
                {"type": "shell", "tool": "shell_exec", "description": "安全检查和执行"},
                {"type": "llm", "tool": "llm_analyze", "description": "分析输出结果"},
                {"type": "llm", "tool": "llm_reply", "description": "报告执行结果"},
            ],
            "skill_match": "shell-execution",
            "estimated_duration": "5s-2 min",
        }

    @staticmethod
    def _file_plan(msg: str, ctx: dict, intent: str) -> dict:
        if intent == "file_search":
            action = "search_filesystem"
            skill = "file-search"
        elif intent == "file_write":
            action = "write_file"
            skill = "file-edit"
        else:
            action = "read_file"
            skill = "file-read"

        return {
            "action": action,
            "sub_tasks": [
                {"type": "file", "tool": intent.replace("file_", "file_"), "description": f"执行{intent}"},
                {"type": "llm", "tool": "llm_process", "description": "处理结果"},
                {"type": "llm", "tool": "llm_reply", "description": "组织回复"},
            ],
            "skill_match": skill,
            "estimated_duration": "1-30s",
        }

    @staticmethod
    def _browser_plan(msg: str, ctx: dict) -> dict:
        return {
            "action": "browser_automation",
            "sub_tasks": [
                {"type": "browser", "tool": "browser_navigate", "description": "导航到目标 URL"},
                {"type": "browser", "tool": "browser_screenshot", "description": "截图确认"},
                {"type": "browser", "tool": "browser_interact", "description": "执行交互操作"},
                {"type": "llm", "tool": "llm_analyze", "description": "分析页面内容"},
            ],
            "skill_match": "browser-automation",
            "estimated_duration": "30s-5 min",
        }

    @staticmethod
    def _config_plan(msg: str, ctx: dict) -> dict:
        return {
            "action": "modify_config",
            "sub_tasks": [
                {"type": "file", "tool": "file_read", "description": "读取当前配置"},
                {"type": "llm", "tool": "llm_analyze", "description": "分析需要的修改"},
                {"type": "file", "tool": "file_patch", "description": "应用配置修改"},
                {"type": "shell", "tool": "shell_exec", "description": "重载服务"},
            ],
            "skill_match": "config-management",
            "estimated_duration": "1-3 min",
        }

    @staticmethod
    def _general_plan(msg: str, ctx: dict) -> dict:
        return {
            "action": "llm_direct_reply",
            "sub_tasks": [
                {"type": "llm", "tool": "llm_reply", "description": "直接回复用户"},
            ],
            "skill_match": None,
            "estimated_duration": "1-5s",
        }


# ═══════════════════════════════════════════
# 边界条件处理器
# ═══════════════════════════════════════════

class EdgeCaseHandler:
    """边界条件和异常输入处理"""

    # 敏感内容模式
    SENSITIVE_PATTERNS = [
        (r"(密码|password|secret)\s*[=:]\s*\S+", "可能包含密码"),
        (r"(token|api_key|apikey)\s*[=:]\s*[\w-]{20,}", "可能包含 API 密钥"),
        (r"\b\d{15,19}\b", "可能包含信用卡号"),
        (r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b", "可能包含邮箱"),
    ]

    # 超长输入阈值
    MAX_INPUT_LENGTH = 50000

    # 无意义输入
    MEANINGLESS_PATTERNS = [
        r"^[a-z]{1,3}$",          # 单字母
        r"^[!@#$%^&*()]+$",       # 纯符号
        r"^[0-9]{20,}$",          # 长数字串
    ]

    @classmethod
    def check(cls, user_message: str) -> dict:
        """
        检查输入的边界条件

        返回: {is_safe, warnings, sanitized_message, handling}
        """
        warnings = []
        sanitized = user_message

        # 1. 空输入
        if not user_message or not user_message.strip():
            return {
                "is_safe": True,
                "warnings": ["empty_input"],
                "sanitized_message": user_message,
                "handling": "ask_clarification",
                "response_hint": "你好像没有输入任何内容？请说明你需要什么帮助。",
            }

        # 2. 超长输入
        if len(user_message) > cls.MAX_INPUT_LENGTH:
            warnings.append(f"input_truncated (original: {len(user_message)} chars)")
            sanitized = user_message[:cls.MAX_INPUT_LENGTH]
            return {
                "is_safe": True,
                "warnings": warnings,
                "sanitized_message": sanitized,
                "handling": "truncate_and_process",
                "response_hint": f"（注意：你的输入较长({len(user_message)}字符)，已截取前{cls.MAX_INPUT_LENGTH}字符处理。）",
            }

        # 3. 敏感内容
        for pattern, desc in cls.SENSITIVE_PATTERNS:
            if re.search(pattern, user_message, re.IGNORECASE):
                warnings.append(f"sensitive_content: {desc}")
                # 脱敏处理
                sanitized = re.sub(pattern, f"[已隐藏 {desc}]", sanitized)

        if any("sensitive_content" in w for w in warnings):
            return {
                "is_safe": True,
                "warnings": warnings,
                "sanitized_message": sanitized,
                "handling": "sanitize_and_process",
                "response_hint": "（已自动隐藏消息中的敏感信息。）",
            }

        # 4. 无意义输入
        for pattern in cls.MEANINGLESS_PATTERNS:
            if re.match(pattern, user_message.strip()):
                return {
                    "is_safe": True,
                    "warnings": ["meaningless_input"],
                    "sanitized_message": user_message,
                    "handling": "ask_clarification",
                    "response_hint": "你的输入似乎不完整。请详细说明你需要什么帮助？",
                }

        # 5. 歧义检测
        ambiguity_score = cls._detect_ambiguity(user_message)
        if ambiguity_score > 0.7:
            return {
                "is_safe": True,
                "warnings": [f"ambiguous_input (score={ambiguity_score:.2f})"],
                "sanitized_message": sanitized,
                "handling": "clarify_or_best_guess",
                "ambiguity_score": ambiguity_score,
            }

        return {
            "is_safe": True,
            "warnings": warnings,
            "sanitized_message": sanitized,
            "handling": "normal",
        }

    @staticmethod
    def _detect_ambiguity(text: str) -> float:
        """检测歧义程度（0-1）"""
        score = 0.0

        # 太短
        if len(text) < 10:
            score += 0.3

        # 代词过多
        pronoun_count = len(re.findall(r'\b(它|他|她|这个|那个|这|那)\b', text))
        score += min(pronoun_count * 0.1, 0.3)

        # 缺少关键动词
        if not re.search(r'(写|做|查|找|运行|执行|修改|删除|创建|读)', text):
            score += 0.2

        return min(score, 1.0)


# ═══════════════════════════════════════════
# 规划引擎（主入口）
# ═══════════════════════════════════════════

@dataclass
class PlanResult:
    """规划结果"""
    task_id: str
    intent: str
    confidence: float
    action: str
    sub_tasks: List[Dict]
    skill_match: Optional[str]
    estimated_duration: str
    edge_case_handling: str
    warnings: List[str]
    sanitized_message: str
    fewshot_used: bool


class Planner:
    """
    增强规划引擎

    流程:
    1. 边界条件检查
    2. 意图分类（规则 + LLM 混合）
    3. Few-shot 匹配
    4. 任务拆解
    5. 生成执行计划
    """

    def __init__(self, llm_client=None):
        self.llm = llm_client  # 可选 LLM 客户端（用于深度分类）

    def plan(self, user_message: str, context: dict = None) -> PlanResult:
        """
        规划入口

        返回完整的 PlanResult，包含执行计划。
        """
        context = context or {}

        # 1. 边界条件检查
        edge = EdgeCaseHandler.check(user_message)

        # 2. 意图分类
        intents = IntentClassifier.classify(edge["sanitized_message"])
        primary_intent = intents[0][0] if intents else "chat_general"
        confidence = intents[0][1] / 100 if intents else 0.5

        # 3. Few-shot 匹配
        fewshot = None
        if primary_intent in FEWSHOT_EXAMPLES:
            fewshot = FEWSHOT_EXAMPLES[primary_intent][0]

        # 4. 任务拆解
        plan = TaskDecomposer.decompose(
            primary_intent,
            edge["sanitized_message"],
            context,
        )

        return PlanResult(
            task_id=plan["task_id"],
            intent=primary_intent,
            confidence=min(confidence, 1.0),
            action=plan.get("action", "llm_direct_reply"),
            sub_tasks=plan.get("sub_tasks", []),
            skill_match=plan.get("skill_match"),
            estimated_duration=plan.get("estimated_duration", "1-5s"),
            edge_case_handling=edge["handling"],
            warnings=edge["warnings"],
            sanitized_message=edge["sanitized_message"],
            fewshot_used=fewshot is not None,
        )

    async def plan_with_llm(self, user_message: str, context: dict = None) -> PlanResult:
        """
        使用 LLM 深度规划

        先用规则做快速分类，如果置信度低则调 LLM 做深度分析。
        """
        result = self.plan(user_message, context)

        # 低置信度 → 调 LLM 深度分析
        if result.confidence < 0.5 and self.llm:
            try:
                llm_plan = await self._llm_deep_plan(user_message, context)
                if llm_plan:
                    result.intent = llm_plan.get("intent", result.intent)
                    result.action = llm_plan.get("action", result.action)
                    result.sub_tasks = llm_plan.get("sub_tasks", result.sub_tasks)
                    result.confidence = llm_plan.get("confidence", result.confidence)
            except Exception as e:
                logger.warning(f"[Planner] LLM deep plan failed: {e}")

        return result

    async def _llm_deep_plan(self, user_message: str, context: dict) -> Optional[dict]:
        """使用 LLM 做深度规划分析"""
        if not self.llm:
            return None

        prompt = f"""Analyze this user request and output a JSON plan:

User: {user_message[:1000]}

Output JSON:
{{
    "intent": "code_fix|code_write|web_search|shell_exec|file_read|file_write|browser|chat_general",
    "action": "specific action description",
    "sub_tasks": [{{"type": "...", "tool": "...", "description": "..."}}],
    "confidence": 0.0-1.0,
    "reasoning": "brief explanation"
}}"""

        try:
            resp = await self.llm.chat_json([
                {"role": "user", "content": prompt}
            ])
            return resp if isinstance(resp, dict) else None
        except Exception:
            return None


# ═══════════════════════════════════════════
# System Prompt 构建器
# ═══════════════════════════════════════════

def build_planning_system_prompt(plan: PlanResult) -> str:
    """根据规划结果构建系统提示"""

    prompt_parts = [
        "你是一个智能助手。请严格按照以下执行计划处理用户请求。",
        "",
        f"## 执行计划",
        f"- 意图: {plan.intent}",
        f"- 动作: {plan.action}",
        f"- 置信度: {plan.confidence:.0%}",
        f"- 预计耗时: {plan.estimated_duration}",
    ]

    if plan.sub_tasks:
        prompt_parts.append("")
        prompt_parts.append("## 子任务")
        for i, st in enumerate(plan.sub_tasks, 1):
            prompt_parts.append(
                f"{i}. [{st.get('type', '?')}] {st.get('description', '')}"
            )

    if plan.warnings:
        prompt_parts.append("")
        prompt_parts.append("## ⚠️ 注意事项")
        for w in plan.warnings:
            prompt_parts.append(f"- {w}")

    if plan.edge_case_handling != "normal":
        prompt_parts.append("")
        prompt_parts.append(f"## 边界处理: {plan.edge_case_handling}")

    prompt_parts.append("")
    prompt_parts.append("请使用可用的工具执行以上计划。如果没有合适的工具，直接回复说明。")

    return "\n".join(prompt_parts)
