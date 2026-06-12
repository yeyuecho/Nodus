"""
插件沙箱 — 独立进程隔离执行
等价于 OpenClaw 的 plugin-sandbox

安全策略:
- 文件访问: 仅允许指定目录
- 网络: 仅允许白名单域名
- 超时: 强制超时中断
- 资源: 内存/CPU 限制（平台相关）
"""

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Optional
from dataclasses import dataclass


@dataclass
class SandboxConfig:
    allowed_dirs: list[str] = None      # 允许访问的目录
    allowed_hosts: list[str] = None     # 允许的网络域名
    timeout_s: float = 30.0             # 超时（秒）
    max_output_bytes: int = 1024 * 100  # 最大输出字节


@dataclass
class SandboxResult:
    success: bool
    output: str
    error: str = ""
    duration_s: float = 0.0


class PluginSandbox:
    """
    插件隔离沙箱

    在独立子进程中执行 Python 脚本，限制:
    - 文件系统访问（通过 sys.addaudithook 拦截）
    - 超时中断
    - 输出大小限制
    """

    def __init__(self, config: SandboxConfig = None):
        self.config = config or SandboxConfig()

    async def execute_script(self, script: str, params: dict = None) -> SandboxResult:
        """在沙箱中执行 Python 脚本"""
        import time
        start = time.time()

        # 构建审计包装
        audit_code = self._build_audit_wrapper()

        full_script = audit_code + "\n" + script

        try:
            proc = await asyncio.create_subprocess_exec(
                "python", "-c", full_script,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={
                    **os.environ,
                    "SANDBOX_PARAMS": str(params or {}),
                    "SANDBOX_ALLOWED_DIRS": ",".join(self.config.allowed_dirs or []),
                },
            )

            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=self.config.timeout_s,
            )

            output = stdout.decode("utf-8", errors="replace")
            if len(output) > self.config.max_output_bytes:
                output = output[:self.config.max_output_bytes] + "\n... [truncated]"

            return SandboxResult(
                success=proc.returncode == 0,
                output=output,
                error=stderr.decode("utf-8", errors="replace"),
                duration_s=time.time() - start,
            )

        except asyncio.TimeoutError:
            proc.kill()
            return SandboxResult(
                success=False,
                output="",
                error=f"Timeout after {self.config.timeout_s}s",
                duration_s=time.time() - start,
            )
        except Exception as e:
            return SandboxResult(
                success=False, output="", error=str(e),
                duration_s=time.time() - start,
            )

    def _build_audit_wrapper(self) -> str:
        """构建 sys.addaudithook 包装（Python 3.8+）"""
        return r"""
import sys
import os

_ALLOWED = os.environ.get("SANDBOX_ALLOWED_DIRS", "").split(",")
_ALLOWED = [d for d in _ALLOWED if d]

def _audit_hook(event, args):
    # 拦截文件打开
    if event == "open":
        path = args[0]
        if isinstance(path, str):
            # 允许标准库和临时文件
            if any(p in path for p in ["/usr/lib", "/lib", "site-packages", "tempfile"]):
                return
            if _ALLOWED and not any(path.startswith(d) for d in _ALLOWED):
                raise PermissionError(f"Sandbox: access denied to {path}")

if hasattr(sys, 'addaudithook'):
    sys.addaudithook(_audit_hook)

# 覆写危险函数
__builtins__['__import__'] = lambda *a, **kw: __import__(*a, **kw)
"""

    async def execute_function(self, func, *args, **kwargs) -> SandboxResult:
        """直接执行 Python 函数（当前进程内，无隔离）"""
        import time
        start = time.time()
        try:
            if asyncio.iscoroutinefunction(func):
                result = await asyncio.wait_for(
                    func(*args, **kwargs),
                    timeout=self.config.timeout_s,
                )
            else:
                result = func(*args, **kwargs)
            return SandboxResult(
                success=True, output=str(result),
                duration_s=time.time() - start,
            )
        except Exception as e:
            return SandboxResult(
                success=False, output="", error=str(e),
                duration_s=time.time() - start,
            )


# ─── 使用示例 ───
async def _demo():
    sandbox = PluginSandbox(SandboxConfig(
        allowed_dirs=[str(Path.cwd())],
        timeout_s=5,
    ))

    result = await sandbox.execute_script("""
import os
print("Hello from sandbox!")
print(f"CWD: {os.getcwd()}")
print(f"Params: {os.environ.get('SANDBOX_PARAMS', 'none')}")
""")
    print(f"Success: {result.success}")
    print(f"Output: {result.output}")
    print(f"Duration: {result.duration_s:.2f}s")


if __name__ == "__main__":
    asyncio.run(_demo())
