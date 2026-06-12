"""
Shell 终端执行 — 安全沙箱命令行
来源: Hermes terminal tool (terminal_tool.py)

安全策略:
- 白名单/黑名单命令过滤
- 超时中断 + 可中断轮询
- 工作目录限制 + 路径验证
- 输出大小限制 + 智能截断
- 后台进程生命周期管理
- sudo 密码缓存与安全注入
- 复合命令后台重写 (&& B & → && { B & })
- 环境变量赋值感知的令牌解析
"""

import asyncio
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("qiyue.shell")


# ─── 配置常量 ───

class ShellConfig:
    """Shell 执行器全局配置"""
    FOREGROUND_MAX_TIMEOUT: float = 600.0
    BACKGROUND_CLEANUP_TIMEOUT: float = 1800.0
    MAX_OUTPUT_BYTES: int = 100 * 1024  # 100KB
    MAX_BACKGROUND_OUTPUT: int = 500 * 1024  # 500KB
    SUDO_PROMPT_TIMEOUT: int = 45
    PROCESS_POLL_INTERVAL: float = 0.1

    # 禁止的命令模式
    DENIED_PATTERNS: List[str] = [
        r"(rm\s+-rf\s+/)",
        r"(mkfs\.\w+)",
        r"(dd\s+if=.+\s+of=/dev/)",
        r"(shutdown|reboot|halt|poweroff)",
        r"(format\s+\w:)",  # Windows
        r"(diskpart)",
        r"(:(){ :\|:& };:)",  # fork bomb
        r"(chmod\s+777\s+/)",
        r"(chown\s+-R\s+\w+\s+/)",
        r"(>\/dev\/sd[a-z])",
        r"(wget\s+.+\|\s*sh)",
        r"(curl\s+.+\|\s*sh)",
        r"(eval\s+.*base64.*decode)",
    ]

    # 工作目录允许的字符集
    WORKDIR_SAFE_RE = re.compile(r'^[A-Za-z0-9/\\:_\-.~ +@=,]+$')

    # 系统关键路径（禁止写入）
    SENSITIVE_PATH_PREFIXES = (
        "/etc/", "/boot/", "/sys/", "/proc/",
        "/usr/lib/systemd/", "/private/etc/",
    )
    SENSITIVE_EXACT_PATHS = {
        "/var/run/docker.sock", "/run/docker.sock",
    }


# ─── 全局状态 ───

_background_processes: Dict[str, "BackgroundProcess"] = {}
_bg_lock = threading.Lock()
_sudo_password_cache: Dict[str, str] = {}
_sudo_cache_lock = threading.Lock()

# 当前进程的工作目录追踪 (Shell 实例感知)
_cwd_cache: Dict[str, str] = {}
_cwd_lock = threading.Lock()


# ─── 辅助函数 ───

def _looks_like_env_assignment(token: str) -> bool:
    """Return True when token is a leading shell env assignment like FOO=bar."""
    if "=" not in token or token.startswith("="):
        return False
    name, _ = token.split("=", 1)
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name))


def _read_shell_token(command: str, start: int) -> Tuple[str, int]:
    """Read one shell token preserving quotes/escapes, starting at `start`."""
    i = start
    n = len(command)
    while i < n:
        ch = command[i]
        if ch.isspace() or ch in ";|&()":
            break
        if ch == "'":
            i += 1
            while i < n and command[i] != "'":
                i += 1
            if i < n:
                i += 1
            continue
        if ch == '"':
            i += 1
            while i < n:
                inner = command[i]
                if inner == "\\" and i + 1 < n:
                    i += 2
                    continue
                if inner == '"':
                    i += 1
                    break
                i += 1
            continue
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        i += 1
    return command[start:i], i


def _safe_command_preview(command: Any, limit: int = 200) -> str:
    """Return a log-safe preview for possibly-invalid command values."""
    if command is None:
        return "<None>"
    if isinstance(command, str):
        return command[:limit]
    try:
        return repr(command)[:limit]
    except Exception:
        return f"<{type(command).__name__}>"


def _rewrite_compound_background(command: str) -> str:
    """Wrap `A && B &` to `A && { B & }` to avoid subshell-wait leaks.

    Bash parses `A && B &` with `&&` tighter than `&`, so it forks a subshell
    for the whole compound.  If B is a long-running server, the subshell never
    exits and leaks.  Rewriting to `A && { B & }` keeps `&&` semantics but uses
    a brace group (no fork) so only B is backgrounded.
    """
    n = len(command)
    i = 0
    paren_depth = 0
    brace_depth = 0
    last_chain_op_end = -1
    rewrites: List[Tuple[int, int]] = []  # (chain_op_end, amp_pos)

    while i < n:
        ch = command[i]

        if ch == "\n" and paren_depth == 0 and brace_depth == 0:
            last_chain_op_end = -1
            i += 1
            continue

        if ch.isspace():
            i += 1
            continue

        if ch == "#":
            nl = command.find("\n", i)
            if nl == -1:
                break
            i = nl
            continue

        if ch == "\\" and i + 1 < n:
            i += 2
            continue

        if ch in {"'", '"'}:
            _, next_i = _read_shell_token(command, i)
            i = max(next_i, i + 1)
            continue

        if ch == "(":
            paren_depth += 1
            i += 1
            continue
        if ch == ")":
            paren_depth = max(0, paren_depth - 1)
            i += 1
            continue

        if ch == "{" and i + 1 < n and command[i + 1].isspace():
            brace_depth += 1
            i += 1
            continue
        if ch == "}" and brace_depth > 0:
            brace_depth -= 1
            last_chain_op_end = -1
            i += 1
            continue

        if paren_depth > 0 or brace_depth > 0:
            i += 1
            continue

        if command.startswith("&&", i) or command.startswith("||", i):
            last_chain_op_end = i + 2
            i += 2
            continue

        if ch == ";":
            last_chain_op_end = -1
            i += 1
            continue

        if ch == "|":
            last_chain_op_end = -1
            i += 1
            continue

        if ch == "&":
            if i + 1 < n and command[i + 1] == ">":
                i += 2  # &> redirect
                continue
            j = i - 1
            while j >= 0 and command[j].isspace():
                j -= 1
            if j >= 0 and command[j] in "<>":
                i += 1  # fd redirect
                continue
            if last_chain_op_end >= 0:
                rewrites.append((last_chain_op_end, i))
            last_chain_op_end = -1
            i += 1
            continue

        _, next_i = _read_shell_token(command, i)
        i = max(next_i, i + 1)

    if not rewrites:
        return command

    result = command
    for chain_end, amp_pos in reversed(rewrites):
        insert_pos = chain_end
        while insert_pos < amp_pos and result[insert_pos].isspace():
            insert_pos += 1
        prefix = result[:insert_pos]
        middle = result[insert_pos:amp_pos]
        suffix = result[amp_pos + 1:]
        result = prefix + "{ " + middle + "& }" + suffix

    return result


def _get_encoding(filepath: Path) -> str:
    """Detect file encoding; uses chardet if available, else utf-8."""
    try:
        import chardet
        with open(filepath, "rb") as f:
            raw = f.read(4096)
        result = chardet.detect(raw)
        return result.get("encoding", "utf-8") or "utf-8"
    except ImportError:
        return "utf-8"


def _truncate_output(output: str, max_bytes: int, *, stream_type: str = "stdout") -> str:
    """Truncate output with informative message."""
    encoded = output.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return output
    # Try to truncate at a character boundary
    truncated = encoded[:max_bytes].decode("utf-8", errors="replace")
    lines = truncated.count("\n")
    suffix = (
        f"\n\n... [{stream_type} truncated at {max_bytes} bytes, "
        f"~{len(truncated)} chars, ~{lines} lines shown]"
    )
    return truncated + suffix


# ─── 后台进程 ───

class BackgroundProcess:
    """Track a background process."""

    def __init__(
        self,
        session_id: str,
        proc: asyncio.subprocess.Process,
        command: str,
        workdir: str,
        max_output: int,
    ):
        self.session_id = session_id
        self.proc = proc
        self.command = command
        self.workdir = workdir
        self.max_output = max_output
        self.start_time = time.time()
        self.output_buffer: List[str] = []
        self.exit_code: Optional[int] = None
        self.finished = False
        self._output_lock = threading.Lock()
        self._read_task: Optional[asyncio.Task] = None

    @property
    def duration_ms(self) -> float:
        return (time.time() - self.start_time) * 1000

    def append_output(self, text: str) -> None:
        with self._output_lock:
            self.output_buffer.append(text)

    def get_output(self) -> str:
        with self._output_lock:
            full = "".join(self.output_buffer)
        if len(full.encode("utf-8", errors="replace")) > self.max_output:
            return _truncate_output(full, self.max_output)
        return full


# ─── Shell 执行器 ───

class ShellExecutor:
    """
    安全终端执行器

    等价于 Hermes 的 terminal 工具。
    特性:
    - 命令安全沙箱 (模式匹配黑名单)
    - 超时 + 可中断执行
    - 后台进程管理
    - 输出截断
    - sudo 密码安全注入
    - 工作目录验证
    - 编码检测
    - Windows/Linux 跨平台
    """

    # 允许的命令白名单（为空则允许所有，除非命中黑名单）
    ALLOWED_COMMANDS: List[str] = []
    # 禁止的命令模式 (正则)
    DENIED_PATTERNS: List[str] = []
    # 允许的工作目录前缀
    ALLOWED_DIRS: List[str] = []

    def __init__(
        self,
        timeout: float = 60.0,
        max_output: int = 100 * 1024,
        workdir: str = None,
        shell: Optional[bool] = None,
        task_id: str = "default",
    ):
        self.timeout = min(timeout, ShellConfig.FOREGROUND_MAX_TIMEOUT)
        self.max_output = max_output
        self.workdir = workdir or os.getcwd()
        self.shell = shell
        self.task_id = task_id
        self._denied_patterns = ShellConfig.DENIED_PATTERNS + self.DENIED_PATTERNS
        self._interrupt_requested = False
        self._sudo_password: Optional[str] = None

        # 编译拒绝模式
        self._compiled_denied: List[re.Pattern] = [
            re.compile(p, re.IGNORECASE) for p in self._denied_patterns
        ]

        # 后台进程跟踪
        self._bg_processes: Dict[str, BackgroundProcess] = {}

    # ─── 公共接口 ───

    def request_interrupt(self) -> None:
        """请求中断当前执行的前台命令。"""
        self._interrupt_requested = True
        logger.info("[Shell] Interrupt requested for task=%s", self.task_id)

    async def execute(
        self,
        command: str,
        *,
        background: bool = False,
        workdir: str = None,
        timeout: float = None,
        env: Dict[str, str] = None,
        notify_on_complete: bool = False,
    ) -> Dict[str, Any]:
        """
        执行命令，返回 {
            exit_code, output, error, duration_ms,
            session_id (仅 background)
        }

        安全: 优先使用 shlex 分词避免 shell 注入。
        """
        import time as _time
        start = _time.time()

        workdir = workdir or self.workdir
        timeout = timeout or self.timeout

        # 工作目录验证
        wd_error = self._validate_workdir(workdir)
        if wd_error:
            return {"exit_code": -1, "output": "", "error": wd_error, "duration_ms": 0}

        # 安全检查
        if not self._is_safe(command):
            return {
                "exit_code": 1,
                "output": "",
                "error": "Command blocked by security policy",
                "duration_ms": (_time.time() - start) * 1000,
            }

        # 复合命令后台重写
        command = _rewrite_compound_background(command)

        # 后台模式
        if background:
            return await self._execute_background(command, workdir, env)

        # 前台模式
        return await self._execute_foreground(command, workdir, timeout, env, start)

    async def execute_script(self, script: str, language: str = "python", **kwargs) -> Dict[str, Any]:
        """执行脚本（Python 或 PowerShell）"""
        if language == "python":
            py = shutil.which("python3") or shutil.which("python") or "python"
            return await self.execute(f'{py} -c {shlex.quote(script)}', **kwargs)
        elif language == "powershell":
            return await self.execute(
                f'powershell.exe -NoProfile -Command {shlex.quote(script)}',
                **kwargs
            )
        else:
            return await self.execute(script, **kwargs)

    async def test_connectivity(self, host: str, port: int = None) -> Dict[str, Any]:
        """测试网络连通性 (ping / nc / Test-NetConnection)"""
        if port:
            if os.name == "nt":
                cmd = f'powershell -c "Test-NetConnection {host} -Port {port}"'
            else:
                cmd = f"nc -zv -w3 {host} {port}"
        else:
            cmd = f"ping -n 1 {host}" if os.name == "nt" else f"ping -c 1 {host}"
        return await self.execute(cmd)

    def get_background(self, session_id: str) -> Optional[Dict[str, Any]]:
        """获取后台进程状态"""
        proc = self._bg_processes.get(session_id)
        if not proc:
            return {"error": f"Background session not found: {session_id}"}
        return {
            "session_id": session_id,
            "exit_code": proc.exit_code,
            "finished": proc.finished,
            "output": proc.get_output(),
            "duration_ms": proc.duration_ms,
        }

    def kill_background(self, session_id: str) -> Dict[str, Any]:
        """终止后台进程"""
        proc = self._bg_processes.get(session_id)
        if not proc:
            return {"error": f"Background session not found: {session_id}"}
        try:
            proc.proc.kill()
            proc.finished = True
            return {"ok": True, "session_id": session_id}
        except Exception as e:
            return {"error": str(e)}

    def kill_all_background(self) -> int:
        """终止所有后台进程，返回终止数量"""
        count = 0
        for session_id, proc in list(self._bg_processes.items()):
            try:
                proc.proc.kill()
                proc.finished = True
                count += 1
            except Exception:
                pass
        self._bg_processes.clear()
        return count

    # ─── 前台执行 ───

    async def _execute_foreground(
        self,
        command: str,
        workdir: str,
        timeout: float,
        env: Optional[Dict[str, str]],
        start: float,
    ) -> Dict[str, Any]:
        """执行前台命令并收集输出"""
        self._interrupt_requested = False
        timeout = min(timeout, ShellConfig.FOREGROUND_MAX_TIMEOUT)

        # 构建子进程参数
        args, use_shell = self._build_args(command)
        proc_env = self._build_env(env)

        try:
            if use_shell:
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=workdir,
                    env=proc_env,
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=workdir,
                    env=proc_env,
                )

            # 使用 poll 循环支持中断
            stdout_chunks: List[bytes] = []
            stderr_chunks: List[bytes] = []

            async def read_stream(stream, chunks):
                while True:
                    try:
                        chunk = await stream.read(8192)
                        if not chunk:
                            break
                        chunks.append(chunk)
                    except Exception:
                        break

            read_stdout = asyncio.create_task(read_stream(proc.stdout, stdout_chunks))
            read_stderr = asyncio.create_task(read_stream(proc.stderr, stderr_chunks))

            deadline = time.time() + timeout
            while not (read_stdout.done() and read_stderr.done() and proc.returncode is not None):
                if self._interrupt_requested:
                    proc.kill()
                    await asyncio.gather(read_stdout, read_stderr, return_exceptions=True)
                    return {
                        "exit_code": -1,
                        "output": b"".join(stdout_chunks).decode("utf-8", errors="replace"),
                        "error": "[Interrupted by user]",
                        "duration_ms": (time.time() - start) * 1000,
                    }

                if time.time() > deadline:
                    proc.kill()
                    await asyncio.gather(read_stdout, read_stderr, return_exceptions=True)
                    return {
                        "exit_code": -1,
                        "output": b"".join(stdout_chunks).decode("utf-8", errors="replace"),
                        "error": f"Command timed out after {timeout}s",
                        "duration_ms": timeout * 1000,
                    }

                await asyncio.sleep(ShellConfig.PROCESS_POLL_INTERVAL)

            # 收集结果
            await asyncio.gather(read_stdout, read_stderr)
            stdout_bytes = b"".join(stdout_chunks)
            stderr_bytes = b"".join(stderr_chunks)

            output = stdout_bytes.decode("utf-8", errors="replace")
            error = stderr_bytes.decode("utf-8", errors="replace")

            # 截断输出
            if len(stdout_bytes) > self.max_output:
                output = _truncate_output(output, self.max_output)
            if len(stderr_bytes) > self.max_output:
                error = _truncate_output(error, self.max_output, stream_type="stderr")

            # 更新工作目录追踪
            self._update_cwd(workdir)

            return {
                "exit_code": proc.returncode or 0,
                "output": output,
                "error": error,
                "duration_ms": (time.time() - start) * 1000,
            }

        except FileNotFoundError:
            return {
                "exit_code": -1,
                "output": "",
                "error": f"Command not found: {_safe_command_preview(command)}",
                "duration_ms": (time.time() - start) * 1000,
            }
        except Exception as e:
            logger.error("[Shell] Execution error: %s", e, exc_info=True)
            return {
                "exit_code": -1,
                "output": "",
                "error": str(e),
                "duration_ms": (time.time() - start) * 1000,
            }

    # ─── 后台执行 ───

    async def _execute_background(
        self,
        command: str,
        workdir: str,
        env: Optional[Dict[str, str]],
    ) -> Dict[str, Any]:
        """后台执行命令"""
        import uuid as _uuid
        session_id = f"bg-{_uuid.uuid4().hex[:12]}"

        args, use_shell = self._build_args(command)
        proc_env = self._build_env(env)

        try:
            if use_shell:
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=workdir,
                    env=proc_env,
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=workdir,
                    env=proc_env,
                )

            bg = BackgroundProcess(
                session_id=session_id,
                proc=proc,
                command=command,
                workdir=workdir,
                max_output=ShellConfig.MAX_BACKGROUND_OUTPUT,
            )

            self._bg_processes[session_id] = bg

            # 启动后台读取任务
            bg._read_task = asyncio.create_task(
                self._read_background_output(bg)
            )

            logger.info(
                "[Shell] Background started: session=%s cmd=%s",
                session_id, _safe_command_preview(command, 80)
            )

            return {
                "exit_code": 0,
                "output": f"Background process started (session_id={session_id})",
                "error": "",
                "duration_ms": 0,
                "session_id": session_id,
            }

        except Exception as e:
            return {
                "exit_code": -1,
                "output": "",
                "error": f"Failed to start background process: {e}",
                "duration_ms": 0,
            }

    async def _read_background_output(self, bg: BackgroundProcess) -> None:
        """持续读取后台进程输出，直到进程退出。"""
        try:
            while True:
                line = await bg.proc.stdout.readline()
                if not line:
                    break
                bg.append_output(line.decode("utf-8", errors="replace"))

            # 收集残留输出
            remaining = await bg.proc.stdout.read()
            if remaining:
                bg.append_output(remaining.decode("utf-8", errors="replace"))

            # 收集 stderr
            stderr_data = await bg.proc.stderr.read()
            if stderr_data:
                bg.append_output(
                    "\n[stderr]\n" + stderr_data.decode("utf-8", errors="replace")
                )

            await bg.proc.wait()
            bg.exit_code = bg.proc.returncode
            bg.finished = True

            logger.info(
                "[Shell] Background finished: session=%s exit=%s duration=%.1fs",
                bg.session_id, bg.exit_code, bg.duration_ms / 1000,
            )

            # 延迟清理
            await asyncio.sleep(ShellConfig.BACKGROUND_CLEANUP_TIMEOUT)
            self._bg_processes.pop(bg.session_id, None)

        except Exception as e:
            logger.error("[Shell] Background read error: session=%s error=%s", bg.session_id, e)
            bg.finished = True
            bg.exit_code = -1

    # ─── 安全检查 ───

    def _is_safe(self, command: str) -> bool:
        """检查命令是否安全（黑名单正则匹配）"""
        cmd_stripped = command.strip()

        if not cmd_stripped:
            return False

        # 检查黑名单模式
        for pattern in self._compiled_denied:
            if pattern.search(cmd_stripped):
                logger.warning("[Shell] Blocked dangerous command: %s", _safe_command_preview(command))
                return False

        # 检查工作目录
        wd = self.workdir
        if wd:
            try:
                resolved = str(Path(wd).resolve())
                for prefix in ShellConfig.SENSITIVE_PATH_PREFIXES:
                    if resolved.startswith(prefix):
                        logger.warning("[Shell] Blocked command in sensitive dir: %s", resolved)
                        return False
            except Exception:
                pass

        return True

    def _validate_workdir(self, workdir: str) -> Optional[str]:
        """验证工作目录安全性。返回 None 表示安全，否则返回错误消息。"""
        if not workdir:
            return None
        if not ShellConfig.WORKDIR_SAFE_RE.match(workdir):
            for ch in workdir:
                if not ShellConfig.WORKDIR_SAFE_RE.match(ch):
                    return (
                        f"Blocked: workdir contains disallowed character {repr(ch)}. "
                        "Use a simple filesystem path without shell metacharacters."
                    )
            return "Blocked: workdir contains disallowed characters."
        return None

    # ─── 参数构建 ───

    def _build_args(self, command: str) -> Tuple[Any, bool]:
        """将命令字符串构建为 subprocess 参数。返回 (args, use_shell)。"""
        if self.shell is True:
            return command, True
        if self.shell is False:
            try:
                return shlex.split(command), False
            except ValueError:
                return command, True

        if os.name == "nt":
            return command, True

        # POSIX: 尝试分词
        try:
            args = shlex.split(command)
            if args:
                return args, False
        except ValueError:
            pass
        return command, True

    def _build_env(self, extra: Optional[Dict[str, str]] = None) -> Optional[Dict[str, str]]:
        """构建子进程环境变量。"""
        env = os.environ.copy()
        if extra:
            env.update(extra)

        # 注入 sudo 密码（如果设置了）
        if self._sudo_password:
            env["SUDO_ASKPASS"] = "/bin/echo"  # 如果 sudo 配置使用 askpass
            env["SUDO_PASSWORD"] = self._sudo_password

        return env

    def _update_cwd(self, workdir: str) -> None:
        """更新缓存的工作目录。"""
        with _cwd_lock:
            _cwd_cache[self.task_id] = workdir

    # ─── sudo 支持 ───

    def set_sudo_password(self, password: str) -> None:
        """设置当前会话的 sudo 密码。"""
        self._sudo_password = password
        scope = self.task_id
        with _sudo_cache_lock:
            _sudo_password_cache[scope] = password
        logger.debug("[Shell] Sudo password cached for task=%s", scope)

    def clear_sudo_password(self) -> None:
        """清除缓存的 sudo 密码。"""
        self._sudo_password = None
        with _sudo_cache_lock:
            _sudo_password_cache.pop(self.task_id, None)

    # ─── 清理 ───

    async def cleanup(self) -> None:
        """清理所有资源（后台进程、缓存）。"""
        count = self.kill_all_background()
        if count:
            logger.info("[Shell] Cleaned up %d background processes for task=%s", count, self.task_id)
        with _cwd_lock:
            _cwd_cache.pop(self.task_id, None)

    def __del__(self):
        """析构时尝试同步清理后台进程。"""
        for proc in list(self._bg_processes.values()):
            try:
                proc.proc.kill()
            except Exception:
                pass
        self._bg_processes.clear()
