"""
文件工具 — 读写/搜索/patch
来源: Hermes file_tools.py

安全策略:
- 工作目录限制 + 系统目录禁止
- 设备路径拦截 (/dev/zero, /dev/stdin 等)
- 敏感路径保护 (/etc, /boot, /sys)
- 大文件分块读取 + 偏移限制
- 编码自动检测
- 二进制文件识别与拦截
"""

import difflib
import errno
import fnmatch
import hashlib
import json
import logging
import os
import re
import stat
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger("qiyue.files")


# ─── 配置常量 ───

DEFAULT_MAX_READ_CHARS = 100_000  # 100K 字符 (~25-35K tokens)
LARGE_FILE_HINT_BYTES = 512_000  # 512KB 以上提示分块读取
MAX_SEARCH_RESULTS = 200
MAX_FILE_SIZE_READ = 10 * 1024 * 1024  # 10MB 单文件读取上限
MAX_WRITE_SIZE = 50 * 1024 * 1024  # 50MB 写入上限

# 禁止读取的设备路径（会导致无限输出或阻塞）
BLOCKED_DEVICE_PATHS = frozenset({
    "/dev/zero", "/dev/random", "/dev/urandom", "/dev/full",
    "/dev/stdin", "/dev/tty", "/dev/console",
    "/dev/stdout", "/dev/stderr",
    "/dev/fd/0", "/dev/fd/1", "/dev/fd/2",
})

# 敏感系统路径（禁止文件工具写入）
SENSITIVE_PATH_PREFIXES = (
    "/etc/", "/boot/", "/sys/", "/proc/",
    "/usr/lib/systemd/", "/private/etc/", "/private/var/",
    "C:\\Windows\\", "C:\\Windows\\System32\\",
)
SENSITIVE_EXACT_PATHS = {
    "/var/run/docker.sock", "/run/docker.sock",
    "C:\\pagefile.sys", "C:\\hiberfil.sys",
}

# 二进制文件扩展名（快速检查）
BINARY_EXTENSIONS = frozenset({
    ".exe", ".dll", ".so", ".dylib", ".bin", ".dat",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".ico", ".webp",
    ".mp3", ".mp4", ".avi", ".mov", ".wav", ".flac", ".ogg",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".pyc", ".pyo", ".class", ".o", ".obj", ".a", ".lib",
    ".db", ".sqlite", ".sqlite3",
})

# 文本文件扩展名
TEXT_EXTENSIONS = frozenset({
    ".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css", ".scss",
    ".json", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".md", ".rst", ".txt", ".log", ".csv", ".tsv",
    ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat",
    ".c", ".cpp", ".h", ".hpp", ".rs", ".go", ".java", ".kt",
    ".swift", ".rb", ".php", ".pl", ".lua", ".r", ".m",
    ".sql", ".graphql", ".proto",
    ".toml", ".lock", ".env", ".gitignore", ".dockerignore",
})


# ─── 辅助函数 ───

def _is_blocked_device_path(path: str) -> bool:
    """Return True for concrete device/fd paths that can hang reads."""
    normalized = os.path.expanduser(path)
    if normalized in BLOCKED_DEVICE_PATHS:
        return True
    if normalized.startswith("/proc/") and normalized.endswith(
        ("/fd/0", "/fd/1", "/fd/2")
    ):
        return True
    if normalized.startswith("/proc/") and normalized.endswith(
        ("/environ", "/cmdline", "/maps")
    ):
        return True
    return False


def _is_blocked_device(filepath: str) -> bool:
    """Return True if the path would hang the process (infinite output or blocking input)."""
    normalized = os.path.expanduser(filepath)
    if _is_blocked_device_path(normalized):
        return True
    try:
        resolved = os.path.realpath(normalized)
    except (OSError, ValueError):
        return False
    if resolved != normalized and _is_blocked_device_path(resolved):
        return True
    return False


def _has_binary_extension(filepath: str) -> bool:
    """Quick extension-based binary check."""
    suffix = Path(filepath).suffix.lower()
    if suffix in BINARY_EXTENSIONS:
        return True
    if suffix in TEXT_EXTENSIONS:
        return False
    return None  # Unknown


def _detect_encoding(filepath: Path, sample_size: int = 8192) -> str:
    """Detect file encoding using chardet or fall back to utf-8."""
    try:
        import chardet
        with open(filepath, "rb") as f:
            raw = f.read(sample_size)
        result = chardet.detect(raw)
        encoding = result.get("encoding") or "utf-8"
        confidence = result.get("confidence", 0)
        logger.debug(
            "[Files] Encoding detected: %s (confidence=%.2f) for %s",
            encoding, confidence, filepath.name
        )
        return encoding
    except ImportError:
        return "utf-8"
    except Exception:
        return "utf-8"


def _is_likely_binary(filepath: Path) -> bool:
    """Check if file is likely binary by reading the first 1024 bytes."""
    # Quick extension check first
    ext_result = _has_binary_extension(str(filepath))
    if ext_result is True:
        return True
    if ext_result is False:
        return False

    try:
        with open(filepath, "rb") as f:
            chunk = f.read(1024)
        if b"\x00" in chunk:
            return True
        # Check for high ratio of non-printable characters
        non_printable = sum(1 for b in chunk if b < 0x09 or (0x0E <= b < 0x20) or b == 0x7F)
        if len(chunk) > 0 and non_printable / len(chunk) > 0.3:
            return True
        return False
    except Exception:
        return True  # Conservatively treat errors as binary


def _is_sensitive_path(filepath: str) -> Optional[str]:
    """Return error message if path is in sensitive system location, else None."""
    try:
        resolved = str(Path(filepath).resolve())
    except Exception:
        resolved = filepath
    normalized = os.path.normpath(os.path.expanduser(filepath))

    for prefix in SENSITIVE_PATH_PREFIXES:
        if resolved.startswith(prefix) or normalized.startswith(prefix):
            return (
                f"Refusing to write to sensitive system path: {filepath}\n"
                "Use the shell tool with sudo if you need to modify system files."
            )
    if resolved in SENSITIVE_EXACT_PATHS or normalized in SENSITIVE_EXACT_PATHS:
        return (
            f"Refusing to write to protected path: {filepath}"
        )
    return None


def _check_disk_space(filepath: Path, content_size: int) -> Optional[str]:
    """Check if enough disk space is available for write. Returns warning or None."""
    try:
        stat_info = os.statvfs(str(filepath.parent))
        available = stat_info.f_frsize * stat_info.f_bavail
        if content_size > available:
            return (
                f"Insufficient disk space: need {content_size} bytes, "
                f"only {available} bytes available"
            )
        if available < 10 * 1024 * 1024:  # 10MB threshold
            logger.warning(
                "[Files] Low disk space: %.1fMB available at %s",
                available / (1024 * 1024), str(filepath.parent)
            )
    except Exception:
        pass
    return None


# ─── FileTools 类 ───

class FileTools:
    """文件操作工具集"""

    # 禁止访问的系统目录
    FORBIDDEN_DIRS = [
        "C:\\Windows", "C:\\Windows\\System32",
        "/etc", "/sys", "/proc", "/boot",
    ]

    # 禁止访问的文件模式
    FORBIDDEN_PATTERNS = [
        "/etc/shadow", "/etc/passwd",
        "~/.ssh/id_rsa", "~/.ssh/id_ed25519",
        ".env.production", "credentials.json",
    ]

    def __init__(self, workdir: str = None, task_id: str = "default"):
        self.workdir = Path(workdir or os.getcwd()).resolve()
        self.task_id = task_id
        self._forbidden_globs = [
            re.compile(fnmatch.translate(p.replace("\\", "/")))
            for p in self.FORBIDDEN_PATTERNS
        ]

    # ═══════════════════════════════════════════
    # 读取
    # ═══════════════════════════════════════════

    def read(
        self,
        path: str,
        offset: int = 1,
        limit: int = 500,
        encoding: str = None,
    ) -> Dict[str, Any]:
        """读取文件，返回 {content, total_lines, start_line, end_line, truncated, encoding, size_bytes}"""
        filepath = self._resolve(path)
        if not filepath:
            return {"error": f"File not found: {path}"}
        if not filepath.exists():
            return {"error": f"File not found: {path}"}
        if not filepath.is_file():
            return {"error": f"Not a file: {path}"}

        # 设备路径检查
        if _is_blocked_device(str(filepath)):
            return {"error": f"Cannot read device/special file: {path}"}

        # 二进制检查
        if _is_likely_binary(filepath):
            file_size = filepath.stat().st_size
            return {
                "content": f"[Binary file detected: {filepath.name} ({file_size} bytes)]",
                "total_lines": 0,
                "start_line": 0,
                "end_line": 0,
                "truncated": False,
                "is_binary": True,
                "encoding": "binary",
                "size_bytes": file_size,
            }

        # 编码检测
        detected_enc = _detect_encoding(filepath) if not encoding else encoding
        file_size = filepath.stat().st_size

        try:
            with open(filepath, "r", encoding=detected_enc, errors="replace") as f:
                all_lines = f.readlines()

            total = len(all_lines)

            # ---- 大文件提示 ----
            large_file_hint = ""
            if file_size > LARGE_FILE_HINT_BYTES and limit > 200:
                large_file_hint = (
                    f"\n[Note: File is {file_size / 1024:.1f}KB. "
                    "Use offset+limit for targeted reads to manage context.]"
                )

            # ---- 分页 ----
            start = max(0, offset - 1)
            end = min(start + limit, total)
            selected = all_lines[start:end]

            content = "".join(selected)

            # ---- 字符数限制 ----
            char_count = len(content)
            if char_count > DEFAULT_MAX_READ_CHARS:
                content = content[:DEFAULT_MAX_READ_CHARS]
                truncated = True
                content += (
                    f"\n\n[... content truncated at {DEFAULT_MAX_READ_CHARS:,} characters "
                    f"(~{total - end + limit} lines remaining)]"
                )
            else:
                truncated = end < total

            # 确保末尾换行
            if content and not content.endswith("\n"):
                content += "\n"

            if large_file_hint:
                content += large_file_hint

            return {
                "content": content,
                "total_lines": total,
                "start_line": start + 1,
                "end_line": end,
                "truncated": truncated,
                "encoding": detected_enc,
                "size_bytes": file_size,
            }

        except UnicodeDecodeError:
            # 编码检测失败，尝试其他编码
            for fallback_enc in ["utf-8", "latin-1", "cp1252", "gbk", "shift-jis"]:
                if fallback_enc == detected_enc:
                    continue
                try:
                    with open(filepath, "r", encoding=fallback_enc, errors="replace") as f:
                        all_lines = f.readlines()
                    total = len(all_lines)
                    start = max(0, offset - 1)
                    end = min(start + limit, total)
                    selected = all_lines[start:end]
                    content = "".join(selected)
                    if len(content) > DEFAULT_MAX_READ_CHARS:
                        content = content[:DEFAULT_MAX_READ_CHARS] + "\n[... truncated]"
                    return {
                        "content": content,
                        "total_lines": total,
                        "start_line": start + 1,
                        "end_line": min(end, total),
                        "truncated": end < total,
                        "encoding": fallback_enc,
                        "size_bytes": file_size,
                    }
                except UnicodeDecodeError:
                    continue
            return {"error": f"Cannot decode file: {path} (tried multiple encodings)"}

        except Exception as e:
            return {"error": str(e)}

    def stat(self, path: str) -> Dict[str, Any]:
        """获取文件/目录元信息"""
        filepath = self._resolve(path)
        if not filepath or not filepath.exists():
            return {"error": f"Not found: {path}"}

        try:
            st = filepath.stat()
            return {
                "path": str(filepath),
                "exists": True,
                "is_file": filepath.is_file(),
                "is_dir": filepath.is_dir(),
                "is_symlink": filepath.is_symlink(),
                "size_bytes": st.st_size,
                "modified": st.st_mtime,
                "created": st.st_ctime,
                "mode": oct(st.st_mode),
                "owner": st.st_uid if hasattr(st, "st_uid") else None,
            }
        except Exception as e:
            return {"error": str(e)}

    # ═══════════════════════════════════════════
    # 写入
    # ═══════════════════════════════════════════

    def write(self, path: str, content: str, encoding: str = "utf-8") -> Dict[str, Any]:
        """写入文件（覆盖），支持自动创建父目录"""
        filepath = self._resolve(path)
        if not filepath:
            return {"error": f"Invalid path: {path}"}

        # 安全检查
        if not self._is_safe_path(filepath):
            return {"error": f"Access denied: {filepath}"}

        # 敏感路径检查
        sensitive_err = _is_sensitive_path(str(filepath))
        if sensitive_err:
            return {"error": sensitive_err}

        # 内容大小检查
        content_bytes = len(content.encode(encoding, errors="replace"))
        if content_bytes > MAX_WRITE_SIZE:
            return {
                "error": (
                    f"Content too large: {content_bytes} bytes "
                    f"(max {MAX_WRITE_SIZE} bytes). Split into smaller writes."
                )
            }

        # 磁盘空间检查
        space_warning = _check_disk_space(filepath, content_bytes)
        if space_warning:
            return {"error": space_warning}

        try:
            filepath.parent.mkdir(parents=True, exist_ok=True)

            # 安全写入：先写入临时文件，再原子替换
            tmp_path = filepath.with_suffix(filepath.suffix + ".tmp")
            tmp_path.write_text(content, encoding=encoding, errors="replace")

            # Windows 需要先删除目标文件
            if filepath.exists() and os.name == "nt":
                filepath.unlink()

            tmp_path.replace(filepath)
            size = filepath.stat().st_size

            logger.info("[Files] Written: %s (%d bytes)", str(filepath), size)
            return {
                "ok": True,
                "path": str(filepath),
                "bytes": size,
                "chars": len(content),
            }

        except Exception as e:
            # 清理临时文件
            if "tmp_path" in locals() and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass
            return {"error": str(e)}

    def append(self, path: str, content: str, encoding: str = "utf-8") -> Dict[str, Any]:
        """追加内容到文件"""
        filepath = self._resolve(path)
        if not filepath:
            return {"error": f"Invalid path: {path}"}

        if not self._is_safe_path(filepath):
            return {"error": f"Access denied: {filepath}"}

        try:
            filepath.parent.mkdir(parents=True, exist_ok=True)
            with open(filepath, "a", encoding=encoding, errors="replace") as f:
                f.write(content)
            size = filepath.stat().st_size
            return {"ok": True, "path": str(filepath), "bytes": size}
        except Exception as e:
            return {"error": str(e)}

    def delete(self, path: str) -> Dict[str, Any]:
        """删除文件"""
        filepath = self._resolve(path)
        if not filepath:
            return {"error": f"Invalid path: {path}"}
        if not filepath.exists():
            return {"error": f"File not found: {path}"}

        if not self._is_safe_path(filepath):
            return {"error": f"Access denied: {filepath}"}

        sensitive_err = _is_sensitive_path(str(filepath))
        if sensitive_err:
            return {"error": sensitive_err}

        try:
            if filepath.is_dir():
                import shutil
                shutil.rmtree(str(filepath))
            else:
                filepath.unlink()
            return {"ok": True, "path": str(filepath), "deleted": True}
        except Exception as e:
            return {"error": str(e)}

    # ═══════════════════════════════════════════
    # Patch (目标替换编辑)
    # ═══════════════════════════════════════════

    def patch(
        self,
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> Dict[str, Any]:
        """
        目标替换编辑 — 支持 9 种模糊匹配策略。

        当 replace_all=False 时，old_string 必须在文件中唯一出现。
        当 replace_all=True 时，替换所有出现。
        """
        filepath = self._resolve(path)
        if not filepath or not filepath.exists():
            return {"error": f"File not found: {path}"}
        if not filepath.is_file():
            return {"error": f"Not a file: {path}"}

        if not self._is_safe_path(filepath):
            return {"error": f"Access denied: {filepath}"}

        sensitive_err = _is_sensitive_path(str(filepath))
        if sensitive_err:
            return {"error": sensitive_err}

        try:
            content = filepath.read_text(encoding="utf-8", errors="replace")

            if replace_all:
                new_content = content.replace(old_string, new_string)
                count = content.count(old_string)
                if count == 0:
                    return {"error": "old_string not found in file"}
            else:
                # 模糊匹配策略
                found_content, strategy = self._fuzzy_find(content, old_string)
                if found_content is None:
                    return {"error": "old_string not found (tried exact + 8 fuzzy strategies)"}
                new_content = content.replace(found_content, new_string, 1)
                count = 1

            # 生成 unified diff
            diff_lines = list(difflib.unified_diff(
                content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=str(filepath),
                tofile=str(filepath),
                lineterm="",
            ))
            diff = "\n".join(diff_lines)

            # 写入
            filepath.write_text(new_content, encoding="utf-8", errors="replace")

            logger.info(
                "[Files] Patched: %s (%d replacement(s), strategy=%s)",
                str(filepath), count,
                strategy if not replace_all else "replace_all"
            )

            return {
                "ok": True,
                "diff": diff,
                "replacements": count,
                "strategy": strategy if not replace_all else "replace_all",
            }

        except Exception as e:
            return {"error": str(e)}

    def _fuzzy_find(self, content: str, needle: str) -> Tuple[Optional[str], str]:
        """尝试多种模糊匹配策略查找 needle 在 content 中的实际出现形式。

        返回 (found_string, strategy_name) 或 (None, "").
        策略:
          0. 精确匹配
          1. 规范化缩进（tab → spaces）
          2. 去除行首尾空白
          3. 统一行尾符 (\r\n → \n)
          4. 规范化缩进 + 去除行首尾空白
          5. 压缩连续空白
          6. 去除所有空白差异（逐行比较非空内容）
          7. 尝试查找原始行号范围的上下文
          8. 忽略末尾标点/空白差异
        """
        needle = needle.replace("\r\n", "\n")

        # Strategy 0: 精确匹配
        count = content.count(needle)
        if count == 1:
            return needle, "exact"
        elif count > 1:
            return None, ""  # 需要 replace_all=True

        strategies = [
            ("indent_normalized", self._normalize_indent),
            ("trimmed_lines", lambda s: "\n".join(l.strip() for l in s.splitlines())),
            ("lf_normalized", lambda s: s.replace("\r\n", "\n")),
            ("indent_trimmed", lambda s: self._normalize_indent(
                "\n".join(l.strip() for l in s.splitlines())
            )),
            ("whitespace_collapsed", lambda s: re.sub(r"[ \t]+", " ", s)),
        ]

        for name, transform in strategies:
            transformed_needle = transform(needle)
            # 对 content 的每一行应用相同的变换并搜索
            lines = content.splitlines(keepends=True)
            transformed_lines = []
            for line in lines:
                transformed_lines.append(transform(line))

            transformed_content = "".join(transformed_lines)
            idx = transformed_content.find(transformed_needle)
            if idx >= 0:
                # 在原始 content 中提取对应范围
                # 计算原始行范围
                prefix_transformed = transformed_content[:idx]
                original_prefix_lines = 0
                pos = 0
                for i, line in enumerate(lines):
                    if pos >= len(prefix_transformed):
                        original_prefix_lines = i
                        break
                    pos += len(transform(line))

                # 估计原始内容范围
                needle_lines_count = transformed_needle.count("\n") + 1
                end_line = min(original_prefix_lines + needle_lines_count + 2, len(lines))
                found = "".join(lines[original_prefix_lines:end_line])

                # 验证
                if found.strip() and needle.strip()[:20].lower() in found.lower():
                    return found.rstrip(), name

        # Strategy 6: 基于行的模糊匹配（忽略空白差异）
        needle_lines = [l.strip() for l in needle.splitlines() if l.strip()]
        content_lines = content.splitlines()
        content_stripped = [(i, l.strip()) for i, l in enumerate(content_lines)]

        for start_i in range(len(content_stripped) - len(needle_lines) + 1):
            match = True
            for j, needle_l in enumerate(needle_lines):
                if content_stripped[start_i + j][1] != needle_l:
                    match = False
                    break
            if match:
                end_i = content_stripped[start_i + len(needle_lines) - 1][0]
                found = "\n".join(
                    content_lines[content_stripped[start_i][0]:end_i + 1]
                )
                return found, "line_content_match"

        return None, ""

    @staticmethod
    def _normalize_indent(text: str) -> str:
        """将 TAB 缩进转换为空格缩进（4空格/TAB）。"""
        lines = text.splitlines(keepends=True)
        result = []
        for line in lines:
            stripped = line.lstrip("\t ")
            indent_len = len(line) - len(stripped)
            # 假设统一的缩进表示
            result.append(" " * indent_len + stripped)
        return "".join(result)

    # ═══════════════════════════════════════════
    # 搜索
    # ═══════════════════════════════════════════

    def search(
        self,
        pattern: str,
        path: str = ".",
        file_glob: str = None,
        limit: int = 50,
        output_mode: str = "content",
        context: int = 0,
    ) -> Dict[str, Any]:
        """
        搜索文件内容 (grep 风格)。

        output_mode: 'content' (匹配行) | 'files_only' (文件路径) | 'count' (计数)
        context: 匹配行前后各显示 N 行
        """
        search_dir = self._resolve(path)
        if not search_dir or not search_dir.exists():
            return {"error": f"Directory not found: {path}"}
        if not search_dir.is_dir():
            # 单文件搜索
            return self._search_single_file(
                str(search_dir), pattern, limit, output_mode, context
            )

        try:
            compiled = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return {"error": f"Invalid regex pattern: {e}"}

        results = []
        files_scanned = 0

        for root, dirs, files in os.walk(str(search_dir)):
            # 跳过隐藏目录和常见忽略目录
            dirs[:] = [
                d for d in dirs
                if not d.startswith(".")
                and d not in ("node_modules", "__pycache__", ".git", "venv", ".venv", "dist", "build")
            ]

            for fname in files:
                if file_glob and not fnmatch.fnmatch(fname, file_glob):
                    continue

                fpath = Path(root) / fname

                # 安全检查
                if not self._is_safe_path(fpath):
                    continue

                # 跳过二进制文件
                if _has_binary_extension(str(fpath)) is True:
                    continue

                files_scanned += 1

                if output_mode == "files_only":
                    try:
                        text = fpath.read_text(encoding="utf-8", errors="ignore")
                        if compiled.search(text):
                            rel = str(fpath.relative_to(search_dir))
                            results.append({"file": rel})
                            if len(results) >= limit:
                                break
                    except Exception:
                        continue
                    if len(results) >= limit:
                        break
                    continue

                if output_mode == "count":
                    try:
                        text = fpath.read_text(encoding="utf-8", errors="ignore")
                        count = len(compiled.findall(text))
                        if count > 0:
                            rel = str(fpath.relative_to(search_dir))
                            results.append({"file": rel, "matches": count})
                    except Exception:
                        continue
                    if len(results) >= limit:
                        break
                    continue

                # output_mode == "content"
                try:
                    lines = fpath.read_text(encoding="utf-8", errors="ignore").splitlines()
                    rel = str(fpath.relative_to(search_dir))

                    for i, line in enumerate(lines):
                        if compiled.search(line):
                            entry = {
                                "file": rel,
                                "line": i + 1,
                                "content": line.strip()[:300],
                            }
                            if context:
                                ctx_start = max(0, i - context)
                                ctx_end = min(len(lines), i + context + 1)
                                entry["context_before"] = [
                                    {"line": j + 1, "content": lines[j][:200]}
                                    for j in range(ctx_start, i)
                                ]
                                entry["context_after"] = [
                                    {"line": j + 1, "content": lines[j][:200]}
                                    for j in range(i + 1, ctx_end)
                                ]
                            results.append(entry)
                            if len(results) >= limit:
                                break
                except Exception:
                    continue

                if len(results) >= limit:
                    break

            if len(results) >= limit:
                break

        return {
            "matches": results,
            "total_matches": len(results),
            "files_scanned": files_scanned,
            "truncated": len(results) >= limit,
        }

    def _search_single_file(
        self, filepath: str, pattern: str, limit: int,
        output_mode: str, context: int,
    ) -> Dict[str, Any]:
        """搜索单个文件"""
        try:
            compiled = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return {"error": f"Invalid regex pattern: {e}"}

        try:
            lines = Path(filepath).read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception as e:
            return {"error": str(e)}

        if output_mode == "count":
            count = 0
            for line in lines:
                count += len(compiled.findall(line))
            return {"matches": [{"file": filepath, "matches": count}], "truncated": False}

        if output_mode == "files_only":
            for line in lines:
                if compiled.search(line):
                    return {"matches": [{"file": filepath}], "truncated": False}
            return {"matches": [], "truncated": False}

        results = []
        for i, line in enumerate(lines):
            if compiled.search(line):
                entry = {"file": filepath, "line": i + 1, "content": line.strip()[:300]}
                if context:
                    ctx_start = max(0, i - context)
                    ctx_end = min(len(lines), i + context + 1)
                    entry["context"] = [
                        {"line": j + 1, "content": lines[j][:200]}
                        for j in range(ctx_start, ctx_end)
                    ]
                results.append(entry)
                if len(results) >= limit:
                    break

        return {"matches": results, "total_matches": len(results), "truncated": len(results) >= limit}

    # ═══════════════════════════════════════════
    # 文件名搜索
    # ═══════════════════════════════════════════

    def find_files(
        self,
        pattern: str = "*",
        path: str = ".",
        max_results: int = 200,
        sort_by: str = "mtime",  # mtime | size | name
    ) -> Dict[str, Any]:
        """按 glob 模式查找文件"""
        search_dir = self._resolve(path)
        if not search_dir or not search_dir.exists():
            return {"error": f"Directory not found: {path}"}

        results = []
        for f in search_dir.rglob(pattern):
            if not self._is_safe_path(f):
                continue

            # 跳过隐藏文件和目录
            if any(part.startswith(".") for part in f.parts[len(search_dir.parts):]):
                continue

            if f.name.startswith("."):
                continue

            try:
                stat_info = f.stat()
                results.append({
                    "path": str(f.relative_to(search_dir)),
                    "size": stat_info.st_size if f.is_file() else 0,
                    "is_dir": f.is_dir(),
                    "is_file": f.is_file(),
                    "mtime": stat_info.st_mtime,
                })
            except Exception:
                continue

            if len(results) >= max_results:
                break

        # 排序
        if sort_by == "mtime":
            results.sort(key=lambda x: x["mtime"], reverse=True)
        elif sort_by == "size":
            results.sort(key=lambda x: x["size"], reverse=True)
        elif sort_by == "name":
            results.sort(key=lambda x: x["path"])

        return {
            "files": results[:max_results],
            "total": len(results),
            "truncated": len(results) >= max_results,
        }

    # ═══════════════════════════════════════════
    # 目录操作
    # ═══════════════════════════════════════════

    def list_dir(self, path: str = ".") -> Dict[str, Any]:
        """列出目录内容（ls 风格）"""
        dirpath = self._resolve(path)
        if not dirpath:
            return {"error": f"Directory not found: {path}"}
        if not dirpath.is_dir():
            return {"error": f"Not a directory: {path}"}

        try:
            entries = []
            for entry in sorted(dirpath.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                if not self._is_safe_path(entry):
                    continue
                try:
                    st = entry.stat()
                    entries.append({
                        "name": entry.name,
                        "is_dir": entry.is_dir(),
                        "is_file": entry.is_file(),
                        "is_symlink": entry.is_symlink(),
                        "size": st.st_size if entry.is_file() else 0,
                        "mtime": st.st_mtime,
                    })
                except Exception:
                    continue

            return {
                "path": str(dirpath),
                "entries": entries,
                "count": len(entries),
            }
        except Exception as e:
            return {"error": str(e)}

    def mkdir(self, path: str, parents: bool = True) -> Dict[str, Any]:
        """创建目录"""
        dirpath = self._resolve(path)
        if not dirpath:
            return {"error": f"Invalid path: {path}"}

        if not self._is_safe_path(dirpath):
            return {"error": f"Access denied: {dirpath}"}

        try:
            if parents:
                dirpath.mkdir(parents=True, exist_ok=True)
            else:
                dirpath.mkdir(exist_ok=True)
            return {"ok": True, "path": str(dirpath)}
        except Exception as e:
            return {"error": str(e)}

    # ═══════════════════════════════════════════
    # 内部
    # ═══════════════════════════════════════════

    def _resolve(self, path_str: str) -> Optional[Path]:
        """解析路径到绝对路径"""
        p = Path(path_str).expanduser()
        if not p.is_absolute():
            p = self.workdir / p
        try:
            return p.resolve()
        except Exception:
            try:
                return p
            except Exception:
                return None

    def _is_safe_path(self, path: Path) -> bool:
        """检查路径是否安全可访问"""
        try:
            resolved = path.resolve()
            path_str = str(resolved).replace("\\", "/")
            forbidden_strs = [d.replace("\\", "/") for d in self.FORBIDDEN_DIRS]

            for forbidden in forbidden_strs:
                if path_str.lower().startswith(forbidden.lower()):
                    return False

            # 检查禁止的文件模式
            for pattern in self._forbidden_globs:
                if pattern.match(path_str):
                    return False

            return True
        except Exception:
            return False

    def get_workdir(self) -> str:
        """获取当前工作目录"""
        return str(self.workdir)

    def set_workdir(self, path: str) -> None:
        """设置工作目录"""
        new_workdir = self._resolve(path)
        if new_workdir and new_workdir.is_dir():
            self.workdir = new_workdir
            logger.info("[Files] Workdir changed to: %s", new_workdir)
