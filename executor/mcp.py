"""
MCP 协议客户端 — Model Context Protocol
等价于 OpenClaw 的 MCP 集成

MCP 允许工具/资源通过标准化协议暴露，支持 stdio 和 HTTP 两种传输。
"""

import asyncio
import json
import subprocess
from typing import Optional
from dataclasses import dataclass, field


@dataclass
class MCPTool:
    name: str
    description: str = ""
    parameters: dict = field(default_factory=dict)


class MCPClient:
    """
    MCP 协议客户端

    支持两种传输:
    - stdio: 启动子进程通信
    - http: HTTP/SSE (预留)

    最小实现 — 支持 stdio transport 的工具发现和调用。
    """

    def __init__(self):
        self._process: Optional[subprocess.Popen] = None
        self._tools: dict[str, MCPTool] = {}
        self._request_id = 0

    async def connect_stdio(self, command: list[str]) -> list[MCPTool]:
        """通过 stdio 连接到 MCP 服务器，返回可用工具列表"""
        self._process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # 发送 initialize 请求
        init_resp = await self._send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "qiyue-heyi", "version": "0.1.0"},
        })

        # 发送 initialized 通知
        await self._send_notification("notifications/initialized", {})

        # 列出工具
        tools_resp = await self._send_request("tools/list", {})
        self._tools.clear()

        for tool_data in tools_resp.get("tools", []):
            tool = MCPTool(
                name=tool_data["name"],
                description=tool_data.get("description", ""),
                parameters=tool_data.get("inputSchema", {}),
            )
            self._tools[tool.name] = tool

        return list(self._tools.values())

    async def call_tool(self, name: str, arguments: dict) -> dict:
        """调用 MCP 工具"""
        if name not in self._tools:
            raise ValueError(f"Unknown tool: {name}")

        resp = await self._send_request("tools/call", {
            "name": name,
            "arguments": arguments,
        })
        return resp.get("content", resp)

    async def list_tools(self) -> list[MCPTool]:
        return list(self._tools.values())

    async def close(self):
        if self._process:
            self._process.stdin.close()
            self._process.stdout.close()
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except (ProcessLookupError, asyncio.TimeoutError):
                pass

    # ─── 内部方法 ───

    async def _send_request(self, method: str, params: dict) -> dict:
        self._request_id += 1
        req = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }
        return await self._send_recv(req)

    async def _send_notification(self, method: str, params: dict):
        req = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        self._process.stdin.write((json.dumps(req) + "\n").encode())
        await self._process.stdin.drain()

    async def _send_recv(self, req: dict) -> dict:
        self._process.stdin.write((json.dumps(req) + "\n").encode())
        await self._process.stdin.drain()

        line = await asyncio.wait_for(
            self._process.stdout.readline(), timeout=30
        )
        return json.loads(line.decode())


# ─── 使用示例 ───
async def _demo():
    # 示例: 连接一个 MCP 文件系统服务器
    client = MCPClient()
    try:
        tools = await client.connect_stdio(["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"])
        print(f"Found {len(tools)} tools:")
        for t in tools:
            print(f"  {t.name}: {t.description[:80]}")
    except Exception as e:
        print(f"MCP demo skipped: {e}")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(_demo())
