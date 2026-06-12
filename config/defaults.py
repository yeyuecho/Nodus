"""
Nodus 默认配置 —— 启动时一次性加载，不需要每轮拼接

包含：
- 项目路径
- 核心文件清单
- 工具描述
- 系统 prompt 模板
"""

from pathlib import Path

# 项目根目录
ROOT = Path(__file__).parent.parent

# 核心文件清单（Nodus 知道自己有哪些文件）
CORE_FILES = {
    "main.py": "主入口，启动网关和推理循环",
    "cli.py": "CLI 命令行，nodus setup/doctor/start/deploy",
    "SOUL.md": "灵魂文件，身份定义和行为红线",
    "brain/__init__.py": "思维层，推理循环和工具调用",
    "brain/persona.py": "人格定义和系统 prompt 构建",
    "gateway/__init__.py": "网关层，消息路由和情绪感知",
    "gateway/console_adapter.py": "控制台适配器，ACK 和回复输出",
    "executor/shell.py": "命令执行器",
    "executor/files.py": "文件操作工具",
    "shared/core.py": "事件总线 + LLM 客户端",
    "shared/models.py": "多模型适配器",
    "data/memory/MEMORY.md": "长期记忆",
    "data/memory/RULES.md": "行为红线",
    "config/config.json": "运行时配置",
    "pyproject.toml": "Python 包定义",
    "requirements.txt": "依赖清单",
}

# 工具描述（注入到 LLM system prompt）
TOOL_DESCRIPTIONS = {
    "shell_exec": "执行Shell命令，参数: command — systeminfo/tasklist/dir 等",
    "file_read": "读取文件内容，参数: path(必填), offset, limit",
    "file_write": "写入文件，参数: path, content",
    "file_patch": "修改文件内容，参数: path, old_string, new_string",
    "file_search": "搜索文件内容，参数: pattern(必填), path, limit",
    "file_find": "查找文件名，参数: pattern(必填), path",
    "web_search": "网络搜索，参数: query(必填), max_results",
    "web_fetch": "获取网页内容，参数: url(必填)",
}

# 系统 Prompt 模板（启动时构建一次）
SYSTEM_PROMPT_TEMPLATE = """你是{name}（Nodus），一个统一智能体。

项目路径: {root}
核心文件: {core_files}

可用工具:
{tools}

规则:
1. 需要信息时调用工具
2. 工具返回后直接回复用户，不要反复调用
3. 用自然中文，像朋友聊天
4. 禁止编造数据
5. 你是单一进程，不需要启动外部服务

{soul}
{memory}
"""
