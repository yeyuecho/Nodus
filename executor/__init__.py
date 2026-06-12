"""
执行层 — OpenClaw 能力移植（纯工具，不调 LLM，不管理记忆）

来源: OpenClaw 工具能力 → Python 等价实现

模块:
- browser.py   → BrowserEngine (Playwright CDP)
- search.py    → WebSearch (httpx 多引擎)
- skills.py    → SkillLoader (SKILL.md YAML)
- mcp.py       → MCPClient (MCP 协议)
- sandbox.py   → PluginSandbox (隔离执行)

⚠️ 执行层不包含 LLM 调用能力，也不管理会话记忆。
    ModelAdapter → shared/models.py (仅供思维层)
    SessionStore → 网关层 + 思维层使用
"""

import asyncio

from .browser import BrowserEngine
from .search import WebSearch, SearchResult
from .skills import SkillLoader, Skill
from .mcp import MCPClient, MCPTool
from .sandbox import PluginSandbox, SandboxConfig, SandboxResult


class Executor:
    """执行层入口 — 无状态工具调度（不决策，不记忆）"""

    def __init__(self, skills_dir: str = "skills",
                 browser_headless: bool = True):
        self.browser = BrowserEngine(headless=browser_headless)
        self.search = WebSearch()
        self.skills = SkillLoader(skills_dir)
        self.sandbox = PluginSandbox()
        self.mcp: MCPClient = None

        self.skills.load_all()

    async def execute(self, tool: str, params: dict) -> dict:
        """根据工具名路由到对应引擎（支持动态注册的 _handlers）"""
        # 优先检查动态注册的 handlers
        if hasattr(self, "_handlers") and tool in self._handlers:
            try:
                result = self._handlers[tool](params)
                if asyncio.iscoroutine(result):
                    result = await result
                return {"success": True, "output": result}
            except Exception as e:
                return {"success": False, "error": str(e)}

        # 回退到内置 handlers
        handlers = {
            "browser_navigate": lambda: self.browser.navigate(**params),
            "browser_click": lambda: self.browser.click(**params),
            "browser_screenshot": lambda: self.browser.screenshot(**params),
            "web_search": lambda: self.search.search(**params),
            "web_fetch": lambda: self.search.fetch_text(**params),
            "sandbox_exec": lambda: self.sandbox.execute_script(**params),
        }

        handler = handlers.get(tool)
        if not handler:
            return {"error": f"Unknown tool: {tool}"}

        try:
            result = await handler()
            return {"success": True, "output": result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def close(self):
        await self.browser.close()
        await self.search.close()
