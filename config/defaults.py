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

# 7大核心文件（根目录，全部 Markdown）
CORE_FILES = {
    "SOUL.md": "灵魂——智能体身份定义、7条绝对红线",
    "AGENTS.md": "架构——三层结构、核心能力、启动方式",
    "MEMORY.md": "长期记忆——踩坑记录、设备清单、架构知识",
    "RULES.md": "行为铁律——别动就停、VM先测再上线",
    "USER.md": "用户画像——偏好、设备、智能家居",
    "config.json": "运行时配置——API密钥、通道开关、模型参数",
    "DEPLOY.md": "部署指南——安装、配置、启动、故障排查",
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
